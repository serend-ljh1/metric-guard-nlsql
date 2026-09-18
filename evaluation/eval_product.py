"""评测 7：产品指标（语义层命中率 / 降级率 / 口径认证率）。

这回答的是**产品成熟度**，而不是 SQL 做题能力：
  - 语义层命中率：业务问题有多少能落到已治理的口径上（主路径覆盖率）——北极星指标
  - 降级率：多少落到多 Agent 自由生成（未认证，需人工判断）
  - 拒绝率：多少被明确拒绝（维度不支持等）——拒绝要给出可操作原因
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.eval.console import ensure_utf8_console  # noqa: E402
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


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "olist_sample" / "sample.db"))
    ap.add_argument("--report-dir", default=str(ROOT / "evaluation" / "reports"))
    args = ap.parse_args()

    cfg = load_config()
    sb = SqlSandbox(args.db, ExecConfig.from_settings(max_rows=200))
    cases = _load_cases()

    print("=" * 66)
    print("评测：产品指标（语义层命中率 / 降级率 / 认证率）")
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
    n_fallback_ok = 0          # 期望降级且实际降级

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
            n_fallback += 1
        else:
            n_reject += 1
        n_ok += int(row["ok"])
        n_certified += int(row["certified"])

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
        else:  # out_of_scope：语义层无此指标 → 线上会降级到多 Agent（需 LLM）
            # 离线模式无 LLM，故这里只能验证"未走语义层"这一必要条件
            oos = path != "semantic"
            n_fallback_ok += int(oos)
            row["out_of_scope_metric_matched"] = bool(row["matched"])
            flag = "✓" if oos else "✗"

        print(f"{flag} {c['q'][:32]:34s} {path:9s} {row['metric'][:24]:26s} "
              f"{'ok' if row['ok'] else ('拒绝' if row['reject'] else '未通过')}")

    total = len(cases)
    n_out_of_scope = sum(1 for c in cases if c["expect"] == "out_of_scope")
    report = {
        "n_cases": total,
        "engine": "semantic-first (compiler)",
        "offline_note": (
            "本评测离线运行（llm=None），因此**降级路径未被实际触发**：口径外问题在离线时"
            "会被明确拒绝而不是交给多 Agent 生成。线上有 LLM 时这部分会走 fallback。"
            "故这里以 'semantic_hit_rate'(落到已治理口径的比例) 为主指标，"
            "降级率仅给出上界（口径外题数占比）。"
        ),
        "semantic_layer": {
            "metrics": len(cfg.metrics), "derived": len(cfg.derived_metrics),
            "dimensions": len(cfg.dimensions),
        },
        "paths": {"semantic": n_semantic, "fallback": n_fallback, "reject": n_reject},
        # 北极星：业务问题落到已治理口径的比例
        "semantic_hit_rate": _pct(n_semantic, total),
        "out_of_scope_rate": _pct(n_out_of_scope, total),
        "reject_rate": _pct(n_reject, total),
        "certified_rate": _pct(n_certified, n_ok),
        "in_scope_ok_rate": _pct(n_hit_rate_ok, n_expect_semantic),
        "metric_accuracy": _pct(n_metric_ok, n_expect_semantic),
        "dims_accuracy": _pct(n_dims_ok, n_expect_semantic),
        "reject_accuracy": _pct(n_reject_ok, sum(1 for c in cases if c["expect"] == "reject")),
        "out_of_scope_detected_rate": _pct(n_fallback_ok, n_out_of_scope),
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
    print(f"    口径外占比        : {report['out_of_scope_rate']:.1%} "
          f"({n_out_of_scope}/{total})  ← 线上这部分走多 Agent 降级")
    print(f"    明确拒绝率        : {report['reject_rate']:.1%} ({n_reject}/{total})")
    print(f"    口径内执行成功率  : {report['in_scope_ok_rate']:.1%}")
    print(f"    指标识别准确率    : {report['metric_accuracy']:.1%}")
    print(f"    维度识别准确率    : {report['dims_accuracy']:.1%}")
    print(f"    拒绝判定准确率    : {report['reject_accuracy']:.1%}")
    print(f"    口径外识别率      : {report['out_of_scope_detected_rate']:.1%}")
    print(f"    口径认证率(成功中): {report['certified_rate']:.1%}")
    print("=" * 66)
    print("注：本评测离线运行，降级路径需 LLM 才会实际触发（见报告 offline_note）。")
    print(f"\n报告已保存: {out_dir / 'product.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
