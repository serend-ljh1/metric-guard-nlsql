"""README ↔ 评测报告一致性校验
=============================
README 里那张「评测指标一览（7 条评测线）」的手填数字，与 `evaluation/reports/*.json`
的**实际产物**之间没有任何机器校验 —— 报告重跑后数字会漂移，README 却可能停留在旧值，
于是"文档数字"与"可复现产物"悄悄脱钩。这个脚本把两者对齐：

    python evaluation/check_readme_consistency.py      # 打印逐行比对结果，退出码 0/1

做法（不依赖 README 排版细节）：
  1. 从 README 找出 7 行评测表格，取**结果列**；
  2. 按各线报告 JSON 计算应出现的**百分比序列**（顺序与 README 一致）；
  3. 逐项比对（允许 ≤0.15 个百分点的小数舍入差）；
  4. 另有若干**结构性断言**（如 row1 的"100% × 5"、row3 的"判定题 N"、row7 的"**0**"）。

CI 通过 pytest（tests/test_readme_consistency.py）自动执行本校验。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
REPORT_DIR = Path(__file__).resolve().parent / "reports"

_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_TOL = 0.15          # 允许的舍入误差（百分点）


def _load(name: str) -> dict | None:
    p = REPORT_DIR / f"{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _readme_rows(text: str) -> dict:
    """从 README 抽取 7 行评测表格的『结果列』（第 4 列）。"""
    rows: dict = {}
    for line in text.splitlines():
        s = line.strip()
        for n in range(1, 8):
            if n in rows:
                continue
            if s.startswith(f"| {n} |"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                if len(cells) >= 4:
                    rows[n] = cells[3]
                break
    return rows


def _pcts(cell: str) -> list:
    return [float(x) for x in _PCT.findall(cell or "")]


def _expectations() -> dict:
    """各方应出现的百分比序列（顺序与 README 表格一致）。缺产物 → 值为 None。"""
    exp: dict = {}

    b = _load("business")
    exp[1] = None if b is None else [b[k] * 100 for k in (
        "in_scope_hit_rate", "guard_block_rate", "permission_block_rate",
        "pii_mask_rate", "audit_coverage")]

    p = _load("product")
    exp[2] = None if p is None else [p[k] * 100 for k in (
        "semantic_hit_rate", "in_scope_ok_rate", "metric_accuracy",
        "dims_accuracy", "certified_rate")]

    d = _load("decisions")
    exp[3] = None if d is None else [d["decision_accuracy"] * 100]

    a = _load("attribution")
    exp[4] = None if a is None else [
        a["attribution"]["hit_rate"] * 100,
        a["attribution"]["topk_hit_rate"]["3"] * 100,
        a["drill"]["hit_rate"] * 100,
        a["factorize"]["hit_rate"] * 100,
    ]

    aq = _load("alert_quality")
    if aq is None:
        exp[5] = None
    else:
        alerts = aq.get("n_threshold_alerts") or 0
        cal = aq.get("n_calendar_driven_alerts") or 0
        lb = aq.get("labeled") or {}
        fp_threshold = (lb.get("threshold_only") or {}).get("false_positive_rate") or 0.0
        gated = lb.get("gated") or {}
        fp_gated = gated.get("false_positive_rate") or 0.0
        recall_gated = gated.get("recall") or 0.0
        exp[5] = [aq["alert_rate_threshold_only"] * 100,
                  aq["suppression_rate"] * 100,
                  (cal / alerts * 100 if alerts else 0.0),
                  fp_threshold * 100, fp_gated * 100, recall_gated * 100]

    se = _load("sandbox_external")
    if se is None:
        exp[6] = None
    else:
        fv = se.get("filter_value_injection") or []
        fv_ok = sum(1 for x in fv if x.get("value_is_literal"))
        escape = (fv_ok / len(fv) * 100 if fv else 0.0)
        exp[6] = [se["dangerous_block_rate"] * 100,
                  se["readonly_false_positive_rate"] * 100, escape]

    e = _load("external_bank_world_1")
    exp[7] = None if e is None else [e["summary"][k] * 100 for k in (
        "coverage_rate", "accuracy_on_covered", "refusal_rate")]

    return exp


def _structural_checks(rows: dict) -> list:
    """与百分比序列无关的结构性断言（格式特殊、或非百分数的关键数字）。"""
    problems: list = []

    # row1：五项护栏全 100%，README 用「100% × 5」这种压缩写法表达
    b = _load("business")
    if b is not None:
        rates = [b[k] for k in ("in_scope_hit_rate", "guard_block_rate",
                                "permission_block_rate", "pii_mask_rate",
                                "audit_coverage")]
        if all(abs(r - 1.0) < 1e-9 for r in rates) and "100% × 5" not in (rows.get(1) or ""):
            problems.append("row1：五项护栏均为 100%，但 README 未写「100% × 5」")

    # row2：语义层命中率的分式 (hits/total) 应与报告一致
    p = _load("product")
    if p is not None:
        n = p.get("n_cases") or 0
        hits = round(p["semantic_hit_rate"] * n) if n else 0
        token = f"({hits}/{n})"
        if n and token not in (rows.get(2) or ""):
            problems.append(f"row2：README 未出现分式 {token}（报告 {hits}/{n}）")

    # row3：判定题数应等于报告的 judgment_cases
    d = _load("decisions")
    if d is not None:
        jc = d.get("judgment_cases")
        if jc is not None and f"判定题 {jc}" not in (rows.get(3) or ""):
            problems.append(f"row3：README 未写「判定题 {jc}」（报告 judgment_cases={jc}）")

    # row7：「敢答且答错」为 0 时，README 应体现 **0**
    e = _load("external_bank_world_1")
    if e is not None and e["summary"].get("wrong") == 0 and "**0**" not in (rows.get(7) or ""):
        problems.append("row7：报告『敢答且答错=0』，但 README 未标注 **0**")

    return problems


def check(readme_path: Path | None = None, report_dir: Path | None = None) -> dict:
    """执行校验，返回 {'ok': bool, 'problems': [...], 'rows': {...}}。"""
    global README, REPORT_DIR
    if readme_path is not None:
        README = Path(readme_path)
    if report_dir is not None:
        REPORT_DIR = Path(report_dir)

    text = README.read_text(encoding="utf-8")
    rows = _readme_rows(text)
    problems: list = []
    for n in range(1, 8):
        if n not in rows:
            problems.append(f"README 缺少第 {n} 行评测表格（或结果列不可解析）")
    exp = _expectations()
    # row1 用「100% × 5」这种压缩写法（一个数字代表五项），无法按位置逐项比对，
    # 改由 _structural_checks 专门断言"五项全 100% 且写了 × 5"。
    compact_rows = {1}
    for n in range(1, 8):
        e = exp.get(n)
        cell = rows.get(n, "")
        if e is None:
            problems.append(f"row{n}：对应报告缺失，无法校验（README 结果列：{cell!r}）")
            continue
        if n in compact_rows:
            continue
        got = _pcts(cell)
        if len(got) != len(e):
            problems.append(f"row{n}：README 结果列解析出 {len(got)} 个百分数"
                            f"（{got}），报告期望 {len(e)} 个（{[round(x, 2) for x in e]}）")
            continue
        for i, (g, want) in enumerate(zip(got, e)):
            if abs(g - want) > _TOL:
                problems.append(f"row{n} 第 {i + 1} 项：README 写 {g}%，报告为 "
                                f"{want:.1f}%（差 {g - want:+.2f}pt）")
    problems += _structural_checks(rows)
    return {"ok": not problems, "problems": problems, "rows": rows}


def main() -> int:
    res = check()
    if res["ok"]:
        print("README 评测表格与 evaluation/reports/*.json 一致 ✓")
        return 0
    print("README 评测表格与报告产物**不一致**，逐条如下：")
    for p in res["problems"]:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
