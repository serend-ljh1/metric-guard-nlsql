"""L1/L2 控制面评测线（evaluation/eval_planning.py）——把评测线的核心断言锁进 CI。

为什么单独锁：`run_all.py` 只**报告**数字、不**断言**数字，评测脚本自身退化了也不会失败。
这条测试用注入式真因小库 + 脚本化 mock LLM 重跑 `evaluate()`，对**关键不变量**硬断言：
  1. L1 规划循环：不越界（硬上限）/ 长脚本被截断 / 合法脚本忠实执行 / 非法脚本全拦 / 无 LLM 全剪枝 / schema 校验 100%；
  2. L2 假设验证环：真因必被 verified 且排第一 / 误否 0 / 结构不成立假设全被否 / **判官零 LLM**。

全部离线（脚本化 mock LLM），不碰真实 API、不碰真实 Olist 库。
"""
from __future__ import annotations

from evaluation.eval_planning import evaluate


def test_planning_battery_invariants(tmpdir_clean):
    r = evaluate(tmp_dir=tmpdir_clean)
    p = r["planning"]

    # 有界性：绝不越界，长脚本被截到硬上限
    assert p["bounded_violations"] == 0
    assert p["cap_enforced"] is True
    assert p["max_steps"] >= 1

    # 合法脚本忠实执行 / 非法脚本全拦 / schema 校验满分 / 无 LLM 全剪枝
    assert p["plan_step_accuracy"] == 1.0
    assert p["adversarial_block_rate"] == 1.0
    assert p["validator_accuracy"] == 1.0
    assert p["no_llm_prune_ok"] is True


def test_hypothesis_battery_invariants(tmpdir_clean):
    r = evaluate(tmp_dir=tmpdir_clean)
    h = r["hypothesis"]

    # 真因必被 verified 且证据排序第一，误否 0
    assert h["truth_verified_rate"] == 1.0
    assert h["false_reject_rate"] == 0.0
    assert h["truth_ranked_first_rate"] == 1.0
    # 结构不成立的假设全被否
    assert h["invalid_rejected_rate"] == 1.0
    # 判官全程不调 LLM
    assert h["judge_llm_calls"] == 0
