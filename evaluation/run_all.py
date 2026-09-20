"""批量评测入口：一次跑业务层 / 产品 / 决策 / 归因命中率四套评测，汇总成一份总报告。

用法：
    python evaluation/run_all.py
    python evaluation/run_all.py --report-dir evaluation/reports

默认全部**离线确定性**运行（不接 LLM），因此无需 API Key、可复现；_decisions 走
离线规则基线（判断题判不出），如需接真实 LLM 手动跑 `eval_decisions.py --llm`。

产物：
    evaluation/reports/business.json      # 业务语义层口径/治理命中率
    evaluation/reports/product.json       # 产品模式语义层命中率（北极星）
    evaluation/reports/decisions.json     # 归因决策质量（离线规则基线）
    evaluation/reports/attribution.json   # 归因三维定位命中率（注入式真因）
    evaluation/reports/overall.json       # 汇总总报告（本脚本产出）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = Path(__file__).resolve().parent / "reports"


def _run_eval(name: str, report_dir: Path, extra: list = None) -> int:
    """运行 evaluation/eval_<name>.py，并返回其退出码。"""
    script = Path(__file__).resolve().parent / f"eval_{name}.py"
    cmd = [sys.executable, str(script)]
    if name == "decisions":
        cmd += ["--out", str(report_dir / "decisions.json")]
    else:
        cmd += ["--report-dir", str(report_dir)]
    cmd += (extra or [])
    print(f"\n>>> 运行 {name} …")
    sys.stdout.flush()
    return subprocess.call(cmd, cwd=str(ROOT))


def _load(report_dir: Path, name: str) -> dict:
    return json.loads((report_dir / f"{name}.json").read_text(encoding="utf-8"))


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _fmt(v) -> str:
    """报告值可能是百分数或 dict（如 top-k= {k1: .., k2: ..}），统一成字符串。"""
    if isinstance(v, dict):
        return "  ".join(f"{k}={_pct(val)}" for k, val in v.items())
    return _pct(v)


def main(report_dir: Path) -> int:
    report_dir.mkdir(parents=True, exist_ok=True)
    rc = 0
    rc |= _run_eval("business", report_dir)
    rc |= _run_eval("product", report_dir)
    rc |= _run_eval("decisions", report_dir)
    rc |= _run_eval("attribution", report_dir, [
        "--periods", "2018-05", "2018-06", "2018-07",
        "--dims", "state", "category", "--inject-pct", "0.60",
        "--topk", "1", "2", "3", "5",
    ])

    overall = {
        "suite": "overall",
        "desc": "批量评测总报告（离线确定性，无需 API Key）",
        "report_dir": str(report_dir),
        "exit_code_ok": rc == 0,
        "suites": {},
    }

    # ---- 业务语义层 ----
    b = _load(report_dir, "business")
    overall["suites"]["business"] = {
        "口径内命中率": b["in_scope_hit_rate"],
        "公式防篡改拦截率": b["guard_block_rate"],
        "权限拦截率": b["permission_block_rate"],
        "PII 掩码覆盖率": b["pii_mask_rate"],
        "审计覆盖率": b["audit_coverage"],
    }

    # ---- 产品模式（北极星 = 语义层命中率） ----
    p = _load(report_dir, "product")
    overall["suites"]["product"] = {
        "语义层命中率(北极星)": p["semantic_hit_rate"],
        "口径内执行成功率": p["in_scope_ok_rate"],
        "指标识别准确率": p["metric_accuracy"],
        "维度识别准确率": p["dims_accuracy"],
        "口径认证率": p["certified_rate"],
    }

    # ---- 归因决策（离线规则基线） ----
    d = _load(report_dir, "decisions")
    overall["suites"]["decisions"] = {
        "决策准确率": d.get("decision_accuracy"),
        "判定题数": d.get("judgment_cases", 0),
    }

    # ---- 归因二维/下钻/因子分解命中率 ----
    a = _load(report_dir, "attribution")
    overall["suites"]["attribution"] = {
        "归因定位命中率(analyze)": a["attribution"]["hit_rate"],
        "归因 top-k 命中率": a["attribution"]["topk_hit_rate"],
        "下钻定位命中率(drill)": a["drill"]["hit_rate"],
        "量价因子分解命中率(factorize)": a["factorize"]["hit_rate"],
    }

    (report_dir / "overall.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 打印汇总 ----
    sep = "=" * 60
    print("\n\n" + "#" * 60)
    print("# 批量评测总报告")
    print("#" * 60)
    print(sep)
    print("● 业务语义层")
    for k, v in overall["suites"]["business"].items():
        print(f"    {k:<12}: {_pct(v)}")
    print(sep)
    print("● 产品模式")
    for k, v in overall["suites"]["product"].items():
        print(f"    {k:<16}: {_pct(v)}")
    print(sep)
    print("● 归因决策（离线规则基线）")
    print(f"    决策准确率    : {_pct(d.get('decision_accuracy', 0))}")
    print(f"    判定题数      : {d.get('judgment_cases', 0)}")
    print(sep)
    print("● 归因三维定位命中率（注入式真因，inject 60%）")
    for k, v in overall["suites"]["attribution"].items():
        print(f"    {k:<24}: {_fmt(v)}")
    print("#" * 60)
    print(f"总报告已保存: {report_dir / 'overall.json'}")
    print(f"退出码: {rc}（{'全部通过' if rc == 0 else '存在失败'}，单个脚本的输出见上方）")
    return rc


def _cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report-dir", default=str(REPORT_DIR),
                    help=f"报告输出目录（默认 {REPORT_DIR}）")
    args = ap.parse_args()
    return main(Path(args.report_dir))


if __name__ == "__main__":
    raise SystemExit(_cli())