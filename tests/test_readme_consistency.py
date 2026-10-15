"""README 评测表格 ↔ evaluation/reports 产物 一致性回归。

背景：README 里那张评测表的数字是**手填**的，报告 JSON 才是**产物**。二者之间
此前没有任何机器校验 —— 重跑评测后数字漂移，README 却可能停在旧值，"文档数字"
与"可复现结论"悄悄脱钩。此用例把校验固化进 CI（校验逻辑见
evaluation/check_readme_consistency.py）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_matches_evaluation_reports():
    sys.path.insert(0, str(ROOT / "evaluation"))
    import check_readme_consistency as chk

    res = chk.check()
    assert res["ok"], "README 评测表格与报告产物不一致：" + "；".join(res["problems"])
