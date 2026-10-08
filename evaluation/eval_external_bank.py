#!/usr/bin/env python
"""外部公开基准评测（Spider dev）—— 覆盖率 / 覆盖内执行正确率 / 拒答率。

**为什么需要**：项目此前所有"口径命中率、执行正确率"都来自**自编**问题集
（Olist 域、问题与标签都由本项目定义）。自编题能证明"机制在自家数据上跑得通"，
但证明不了"换个没人参与定义的域还能跑通"。本脚本把语义层接到**第三方定义**的
语料上：问题与 gold SQL 都来自 Spider 数据集（耶鲁 LILY，公开学术基准），
答案对不对由 gold SQL 的执行结果裁定 —— 不是本项目说了算。

三个数，各自诚实：
  1. **覆盖率**        命中口径并成功编译/执行 / 全部问题。
     ⚠ 口径：`world_1` 属于 dev 集，建层时看过题面 → 这是**开发集覆盖率**，
     不是 held-out 泛化指标。报告里显式标注，不冒充泛化。
  2. **覆盖内执行正确率** 在"敢答"的那部分里，结果行集与 gold 行集**完全一致**的比例。
     这一项与上面相反：**它不因看过题面而失真** —— 答案对不对由 gold 判定。
  3. **拒答率与拒答归因** 未覆盖问题一律显式拒答（不猜、不降级生成），
     并按原因分类（无对应指标 / 并列多指标 / 句式超纲 / 过滤取值或关系 / 编译失败）。

比"准确率"更重要的是**错答数**：一个可信系统的失败模式应当是"拒答"而不是"错答"。
本报告把 `wrong`（敢答且答错）单独计数并逐条列出 —— 这是最有价值的一栏。

跑法：
    python evaluation/eval_external_bank.py                      # world_1 / dev 全量
    python evaluation/eval_external_bank.py --db_id world_1 --limit 20
    python evaluation/eval_external_bank.py --spider-root "D:/ds harness/spider"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.business.metric_config import load_config  # noqa: E402
from sqlpa.business.service import answer  # noqa: E402
from sqlpa.config import repo_relative  # noqa: E402
from sqlpa.sandbox.sql_executor import SqlSandbox  # noqa: E402

_DEFAULT_SPIDER = ROOT.parent / "spider"
_DEFAULT_CONFIG = ROOT / "config" / "semantic_world.yaml"


def _default_spider_root() -> Path:
    """Spider 根目录：环境变量优先（`.env.example` 里已声明 SQLPA_SPIDER_ROOT）。

    没有它，`.env` 里那个变量就是"文档写了但没人读"的假开关；
    且换了机器只能靠命令行参数，容易复现失败。
    """
    env = (os.environ.get("SQLPA_SPIDER_ROOT") or "").strip()
    return Path(env) if env else _DEFAULT_SPIDER

# 拒答原因 → 分类（按 answer() 返回的 reject 文案前缀映射）
_REFUSAL_RULES = [
    ("未识别到业务指标", "no_metric"),
    ("并列了多个指标", "multi_metric"),
    ("同一过滤条件出现多个取值", "filter_conflict"),
    ("不在已登记的过滤取值域内", "unknown_entity"),
    ("该问题包含", "unsupported_pattern"),
    ("不是同一个", "having_cross_metric"),
    ("不支持", "unsupported_combination"),
    ("语义层编译失败", "compile_error"),
    ("权限", "permission"),
    ("公式", "formula_guard"),
]


def _classify_refusal(reject: str) -> str:
    for prefix, tag in _REFUSAL_RULES:
        if prefix in (reject or ""):
            return tag
    return "other"


def _sqlite_value_provider(db_path: Path):
    """过滤取值域来自库自身的列（建层时取 DISTINCT，不手抄字典）。"""
    def provider(table: str, column: str):
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL').fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    return provider


def _norm_cell(v):
    """单元格归一化：数值按有效数字，字符串按小写去空白（与 Spider EX 的宽松口径一致）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int,)):
        return int(v)
    if isinstance(v, float):
        return float(f"{v:.6g}")
    s = str(v).strip()
    try:
        return float(f"{float(s):.6g}")
    except ValueError:
        return s.lower()


def _norm_rows(rows) -> Counter:
    return Counter(tuple(_norm_cell(c) for c in row) for row in rows)


def _gold_execute(db_path: Path, sql: str):
    """用**独立连接**（与被测沙箱无关）执行 gold SQL，得到参考结果。"""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.execute(sql)
        return [list(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _ex_match(mine, gold) -> bool:
    if mine is None or gold is None:
        return False
    if mine and gold and len(mine[0]) != len(gold[0]):
        return False          # 列数不同 → 不是同一个答案（"只答一半"由此暴露）
    return _norm_rows(mine) == _norm_rows(gold)


def _shape(sql: str) -> str:
    """gold SQL 的粗形状，用于分层报数（解释"覆盖率为什么是这个数"）。"""
    tags = []
    n_agg = len(re.findall(r"\b(count|sum|avg|max|min)\s*\(", sql, re.I))
    tags.append(f"agg{n_agg}")           # agg0 = 无聚合（行级列举）
    if re.search(r"\bJOIN\b", sql, re.I):
        tags.append("join")
    if re.search(r"\bGROUP\s+BY\b", sql, re.I):
        tags.append("groupby")
    if re.search(r"\bHAVING\b", sql, re.I):
        tags.append("having")
    if re.search(r"\bUNION\b|\bINTERSECT\b|\bEXCEPT\b", sql, re.I):
        tags.append("setop")
    if re.search(r"\bLIMIT\b", sql, re.I):
        tags.append("limit")
    if re.search(r"\bORDER\s+BY\b", sql, re.I):
        tags.append("orderby")
    if re.search(r"\bWHERE\b", sql, re.I):
        tags.append("where")
    return "+".join(tags)


def _group_of(shape: str) -> str:
    """把形状粗分成三档：单聚合可答 / 多聚合并列 / 行级列举。"""
    if "setop" in shape or "having" in shape:
        return "集合运算/HAVING（超出单指标口径）"
    aggs = int(re.search(r"agg(\d+)", shape).group(1))
    if aggs >= 2:
        return "多聚合并列（语义层一次一个度量）"
    if aggs == 1:
        return "单聚合（语义层目标形态）"
    return "无聚合（行级列举/取最大者）"


def main() -> int:
    ap = argparse.ArgumentParser(description="外部公开基准评测（Spider dev）：覆盖率/正确率/拒答率")
    ap.add_argument("--spider-root", default=str(_default_spider_root()),
                    help="Spider 数据集根目录（或设 SQLPA_SPIDER_ROOT）")
    ap.add_argument("--db_id", default="world_1", help="评测用库（默认 world_1）")
    ap.add_argument("--config", default=str(_DEFAULT_CONFIG), help="语义层配置 YAML")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全量）")
    ap.add_argument("--report-dir", default=str(ROOT / "evaluation" / "reports"))
    ap.add_argument("--show-wrong", type=int, default=10, help="打印多少条错答明细")
    args = ap.parse_args()

    root = Path(args.spider_root)
    dev_json = root / "dev.json"
    db_path = root / "database" / args.db_id / f"{args.db_id}.sqlite"
    if not dev_json.exists():
        print(f"❌ 找不到 {dev_json}（用 --spider-root 指定 Spider 根目录）")
        return 2
    if not db_path.exists():
        print(f"❌ 找不到 {db_path}")
        return 2

    cfg = load_config(args.config, value_provider=_sqlite_value_provider(db_path))
    fv = (cfg.matcher_keywords or {}).get("filter_values") or {}
    n_values = sum(len(v) for v in fv.values())
    print(f"语义层: {args.config}")
    print(f"  指标 {len(cfg.metrics)} 个 / 维度 {len(cfg.dimensions)} 个 / "
          f"过滤模板 {len(cfg.filter_templates)} 种")
    print(f"  过滤取值词表: {len(fv)} 类共 {n_values} 个取值（来自库自身的列）")

    dev = json.loads(dev_json.read_text(encoding="utf-8"))
    cases = [d for d in dev if d["db_id"] == args.db_id]
    if args.limit:
        cases = cases[:args.limit]
    print(f"语料: {dev_json.name} 中 db_id={args.db_id} 共 {len(cases)} 题 "
          f"(gold SQL 由数据源外部定义，非本项目自编)")

    sb = SqlSandbox(db_path)
    details = []
    for i, c in enumerate(cases, 1):
        gold_rows, gold_err = None, ""
        try:
            gold_rows = _gold_execute(db_path, c["query"])
        except Exception as e:                       # noqa: BLE001
            gold_err = f"{type(e).__name__}: {e}"
        r = answer(c["question"], cfg, sb, str(db_path), llm=None,
                   role="analyst", username="eval-external")
        rows = r.get("rows") if r.get("ok") else None
        if r.get("ok"):
            status = "correct" if _ex_match(rows, gold_rows) else "wrong"
            tag = ""
        else:
            status = "refused"
            tag = _classify_refusal(str(r.get("reject") or ""))
        details.append({
            "idx": i, "question": c["question"], "gold_sql": c["query"],
            "shape": _shape(c["query"]), "group": _group_of(_shape(c["query"])),
            "status": status, "refusal": tag,
            "metric": r.get("metric"), "match_method": r.get("match_method"),
            "sql": r.get("sql"), "rows": rows, "gold_rows": gold_rows,
            "gold_error": gold_err, "reject": r.get("reject"),
        })
        mark = {"correct": "✓", "wrong": "✗", "refused": "·"}[status]
        if status != "correct":
            print(f"  [{i:3d}] {mark} {status:7s} {tag:20s} {c['question'][:78]}")

    n = len(details)
    covered = [d for d in details if d["status"] in ("correct", "wrong")]
    correct = [d for d in details if d["status"] == "correct"]
    wrong = [d for d in details if d["status"] == "wrong"]
    refused = [d for d in details if d["status"] == "refused"]
    by_refusal = Counter(d["refusal"] for d in refused)
    by_group = {}
    for d in details:
        g = by_group.setdefault(d["group"], {"total": 0, "correct": 0, "wrong": 0, "refused": 0})
        g["total"] += 1
        g[d["status"]] += 1
    gold_errors = [d for d in details if d["gold_error"]]

    report = {
        "provenance": {
            "dataset": "Spider (Yale LILY) — dev split",
            "note": ("问题与 gold SQL 由第三方数据集定义；本文件的分母/分子由脚本计算，"
                     "可复跑核对。world_1 属 dev 集，建层时看过题面 → 覆盖率为**开发集**口径。"),
            "spider_root": repo_relative(root), "split": "dev", "db_id": args.db_id,
            "dev_json_sha256": hashlib.sha256(dev_json.read_bytes()).hexdigest(),
            "db_sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
            "config": repo_relative(args.config),
            "config_metrics": len(cfg.metrics),
            "filter_value_vocab": {k: len(v) for k, v in fv.items()},
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "llm": None,
        },
        "summary": {
            "total": n,
            "covered": len(covered),
            "coverage_rate": round(len(covered) / n, 4) if n else None,
            "correct": len(correct),
            "wrong": len(wrong),
            "accuracy_on_covered": round(len(correct) / len(covered), 4) if covered else None,
            "refused": len(refused),
            "refusal_rate": round(len(refused) / n, 4) if n else None,
            "refusal_breakdown": dict(by_refusal),
            "gold_exec_errors": len(gold_errors),
        },
        "by_gold_shape": by_group,
        "wrong_details": [{k: d[k] for k in ("idx", "question", "gold_sql", "metric", "sql",
                                             "rows", "gold_rows", "reject")} for d in wrong],
        "details": details,
    }
    out_dir = Path(args.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"external_bank_{args.db_id}.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    s = report["summary"]
    print("\n" + "=" * 72)
    print(f"外部基准 {args.db_id} (dev, N={s['total']})")
    print(f"  覆盖率（开发集口径）     : {s['covered']}/{s['total']} = "
          f"{(s['coverage_rate'] or 0):.1%}")
    print(f"  覆盖内执行正确率         : {s['correct']}/{s['covered']} = "
          f"{(s['accuracy_on_covered'] or 0):.1%}   ← 由 gold SQL 结果裁定，与是否看过题面无关")
    print(f"  ⚠ 敢答且答错 (wrong)     : {s['wrong']}"
          + ("   ← 必须为 0 或逐条有解释" if s["wrong"] else "   （无）"))
    print(f"  显式拒答                 : {s['refused']}/{s['total']} = {s['refusal_rate']:.1%}")
    for k, v in sorted(s["refusal_breakdown"].items(), key=lambda x: -x[1]):
        print(f"      - {k:22s} {v}")
    print("  按 gold 形状分层:")
    for g, v in sorted(by_group.items(), key=lambda x: -x[1]["total"]):
        print(f"      {g:32s} 共{v['total']:3d}  答对{v['correct']:3d}  "
              f"答错{v['wrong']:2d}  拒答{v['refused']:3d}")
    if gold_errors:
        print(f"  ⚠ gold SQL 执行失败 {len(gold_errors)} 条（这些题无法裁定，已列明细）")
    for d in wrong[:max(0, args.show_wrong)]:
        print(f"\n  ✗ [{d['idx']}] {d['question']}")
        print(f"      gold: {d['gold_sql'][:110]}")
        print(f"      mine: {(d['sql'] or '').replace(chr(10), ' ')[:110]}")
        print(f"      rows={d['rows']}  gold={d['gold_rows']}")
    print(f"\n产物 -> {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
