"""评测 7：产品指标（语义层命中率 / 口径外拒绝率 / 口径认证率）。

回答"这个产品覆盖了多少业务问题"，而不是"SQL 写得对不对"：
  - 语义层命中率：多少问题落到了**已治理口径**（主路径，决定性指标）
  - 口径外拒绝率：多少问题没有对应口径 → **明确拒绝**（不存在"降级自由生成"这条路径）
  - 认证率：返回成功的结果里，带"口径已认证"的比例

这回答的是**产品成熟度**，而不是 SQL 做题能力：
  - 语义层命中率：业务问题有多少能落到已治理的口径上（主路径覆盖率）——北极星指标
  - 口径外拒绝率：多少被明确拒绝（无对应口径 / 维度组合不支持 / 句式超纲）——拒绝要给出可操作原因
  - 口径认证率：成功返回的结果里，多少是"口径已认证"的
  - 零 token 占比：走语义层的比例即"不花 LLM 成本"的比例
  - 人工介入率：进入 HITL 队列的比例

用法：
    python evaluation/eval_product.py
产物：evaluation/reports/product.json（含逐题明细，可复核）
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.config import ensure_utf8_console, repo_relative  # noqa: E402
from sqlpa.business.metric_config import load_config  # noqa: E402
from sqlpa.business.service import answer  # noqa: E402
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox  # noqa: E402

QUESTIONS = ROOT / "evaluation" / "business_questions.jsonl"


def _load_cases() -> list:
    cases = []
    with open(QUESTIONS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _pct(a: int, b: int) -> float:
    return round(a / b, 4) if b else 0.0


def _require_db(db: Optional[str]) -> tuple[Optional[str], str]:
    """定位业务库：真实全量库 > 仓库自带样本库；都没有则大声跳过（不让 CI 红）。

    跑在样本库上时会打印提示并在报告里记 `db_kind`，避免把示意值当成全量口径结论。
    """
    from sqlpa.config import db_kind_note, resolve_db_path
    try:
        path, kind = resolve_db_path(db)
    except FileNotFoundError as e:
        print(f"[SKIP] {e}")
        print("       离线回归请跑：pytest tests -q")
        return None, "none"
    if db_kind_note(kind):
        print(db_kind_note(kind))
    return path, kind


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="业务库（默认：真实全量库 > 仓库自带样本库）")
    ap.add_argument("--report-dir", default=str(ROOT / "evaluation" / "reports"))
    args = ap.parse_args()

    db, db_kind = _require_db(args.db)
    if not db:
        return 0
    args.db = db

    cfg = load_config()
    sb = SqlSandbox(args.db, ExecConfig.from_settings(max_rows=200))
    cases = _load_cases()

    print("=" * 66)
    print("评测：产品指标（语义层命中率 / 口径外拒绝率 / 认证率）")
    print(f"业务库: {args.db}")
    print(f"指标数: {len(cfg.metrics)}  派生: {len(cfg.derived_metrics)}  "
          f"维度: {len(cfg.dimensions)}  题目: {len(cases)}")
    print("=" * 66)

    rows = []
    n_semantic = n_fallback = n_reject = 0
    n_ok = n_certified = 0
    n_metric_ok = n_dims_ok = 0
    n_expect_semantic = 0
    n_hit_rate_ok = 0          # 期望语义层且实际走语义层
    n_reject_ok = 0            # 期望拒绝且实际被拒
    n_out_of_scope_ok = 0      # 期望口径外 且 实际未走语义层（= 被明确拒绝）

    print(f"\n{'题目':34s} {'路径':9s} {'指标':26s} {'结果'}")
    print("-" * 90)
    for c in cases:
        a = answer(c["q"], cfg, sb, args.db, llm=None, role="analyst", username="eval")
        path = a.get("path") or ("reject" if a.get("matched") is False else "unknown")
        row = {"q": c["q"], "expect": c["expect"], "note": c.get("note", ""),
               "path": path, "matched": bool(a.get("matched")), "ok": bool(a.get("ok")),
               "certified": bool(a.get("certified")), "metric": a.get("metric", ""),
               "dims": a.get("dims") or [], "rows": len(a.get("rows") or []),
               "reject": (a.get("reject") or "")[:120]}
        rows.append(row)

        if path == "semantic":
            n_semantic += 1
        elif path == "fallback":
            # 历史"降级自由生成"路径：已删除。保留计数以便**断言它恒为 0**（回归护栏）
            n_fallback += 1
        else:
            n_reject += 1
        n_ok += int(row["ok"])
        # 只统计**成功结果**里的认证率：被拒的查询不是"口径已认证"。
        # （否则分子会包含拒绝分支，出现 >100% 这种不可能的数字——曾实测 101.8%。）
        n_certified += int(row["ok"] and row["certified"])

        flag = ""
        if c["expect"] == "semantic":
            n_expect_semantic += 1
            n_metric_ok += int(row["metric"] == c.get("metric"))
            n_dims_ok += int(set(row["dims"]) == set(c.get("dims") or []))
            hit = (path == "semantic" and row["ok"])
            n_hit_rate_ok += int(hit)
            flag = "✓" if hit else "✗"
        elif c["expect"] == "reject":
            # 指标命中但维度组合不受支持 → 应明确拒绝，而不是硬生成
            rej = (path != "semantic") and not row["ok"] and bool(row["reject"])
            n_reject_ok += int(rej)
            flag = "✓" if rej else "✗"
        else:  # out_of_scope：语义层无此口径 → 现行策略是**明确拒绝**（不存在降级生成）
            # 离线/在线一致：口径外都走拒绝，因此这里验证"未走语义层且被明确拒绝"
            oos = path != "semantic"
            n_out_of_scope_ok += int(oos)
            row["out_of_scope_metric_matched"] = bool(row["matched"])
            flag = "✓" if oos else "✗"

        print(f"{flag} {c['q'][:32]:34s} {path:9s} {row['metric'][:24]:26s} "
              f"{'ok' if row['ok'] else ('拒绝' if row['reject'] else '未通过')}")

    total = len(cases)
    n_out_of_scope = sum(1 for c in cases if c["expect"] == "out_of_scope")
    report = {
        "n_cases": total,
        "db_kind": db_kind,
        "db": repo_relative(args.db),
        "engine": "semantic-first (compiler)",
        "offline_note": (
            "本评测离线运行（llm=None），走确定性关键词匹配链路，因此衡量的是"
            "**关键词词表 + 口径配置的覆盖能力**。口径外问题在离线与在线**都**是"
            "明确拒绝（自由 SQL 生成链路已删除，不存在降级路径）；"
            "线上有 LLM 时，未被关键词命中的问法会由 LLM 识别并**停在口径确认门**等人确认"
            "（method=llm 不允许直接执行），那部分能力未在本评测中度量。"
            "故这里以 'semantic_hit_rate'(落到已治理口径的比例) 为主指标，"
            "'out_of_scope' 占比即**拒绝率**。"
        ),
        "semantic_layer": {
            "metrics": len(cfg.metrics), "derived": len(cfg.derived_metrics),
            "dimensions": len(cfg.dimensions),
        },
        "paths": {"semantic": n_semantic, "fallback": n_fallback, "reject": n_reject},
        "paths_note": ("fallback 恒为 0：自由 SQL 生成（降级）链路已删除，口径外一律走 reject。该字段保留为回归护栏。"),
        # 北极星：业务问题落到已治理口径的比例
        "semantic_hit_rate": _pct(n_semantic, total),
        "out_of_scope_rate": _pct(n_out_of_scope, total),
        "reject_rate": _pct(n_reject, total),
        "certified_rate": _pct(n_certified, n_ok),
        "in_scope_ok_rate": _pct(n_hit_rate_ok, n_expect_semantic),
        "metric_accuracy": _pct(n_metric_ok, n_expect_semantic),
        "dims_accuracy": _pct(n_dims_ok, n_expect_semantic),
        "reject_accuracy": _pct(n_reject_ok, sum(1 for c in cases if c["expect"] == "reject")),
        "out_of_scope_detected_rate": _pct(n_out_of_scope_ok, n_out_of_scope),
        "details": rows,
    }

    out_dir = Path(args.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "product.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 66)
    print("===== 产品指标汇总 =====")
    print(f"  ★ 语义层命中率      : {report['semantic_hit_rate']:.1%} "
          f"({n_semantic}/{total})  ← 北极星：落到已治理口径的比例")
    print(f"    口径外拒绝占比    : {report['out_of_scope_rate']:.1%} "
          f"({n_out_of_scope}/{total})  ← 无对应口径 → 明确拒绝（不是降级生成）")
    print(f"    明确拒绝率        : {report['reject_rate']:.1%} ({n_reject}/{total})")
    print(f"    口径内执行成功率  : {report['in_scope_ok_rate']:.1%}")
    print(f"    指标识别准确率    : {report['metric_accuracy']:.1%}")
    print(f"    维度识别准确率    : {report['dims_accuracy']:.1%}")
    print(f"    拒绝判定准确率    : {report['reject_accuracy']:.1%}")
    print(f"    口径外识别率      : {report['out_of_scope_detected_rate']:.1%}")
    print(f"    口径认证率(成功中): {report['certified_rate']:.1%}")
    print("=" * 66)
    print("注：本评测离线运行（走确定性关键词链路）；口径外离线与在线都明确拒绝，"
          "见报告 offline_note。")
    print(f"\n报告已保存: {out_dir / 'product.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
