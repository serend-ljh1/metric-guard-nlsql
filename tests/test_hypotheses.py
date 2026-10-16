"""L2 假设验证环 —— LLM 提假设、确定性判官裁决 测试。

锁定：
  1. 确定性候选按 analyze 结果生成；
  2. 判官对 dim_value 假设：维度不在候选 → 否；取值不存在 → 否；**分段之和≠总变化（不可加）→ 否**；
     可加且存在 → 成立；
  3. 判官对 factor 假设：因子不在分解里 → 否；有贡献 → 成立；
  4. propose_hypotheses 去重合并 LLM 假设与确定性候选；
  5. run_hypothesis_loop 正确分流 verified/rejected 并按证据排序。

全部离线（Mock LLM），判官零 LLM、零 token。
"""
from __future__ import annotations

from sqlpa.analysis import hypotheses
from sqlpa.business.metric_config import load_config

CFG = load_config()

# change=-70；state 维分段 {SP:-70, RJ:0} 合计 -70（可加）；bad 维合计 -30（不可加）
ATTR = {
    "is_abnormal": True, "change": -70.0, "change_pct": -0.4667,
    "dims": [
        {"dim": "state", "delta": {"SP": -70.0, "RJ": 0.0}},
        {"dim": "bad", "delta": {"x": -10.0, "y": -20.0}},
    ],
    "top_contributors": [
        {"dim": "state", "key": "SP", "delta": -70.0, "pct_of_change": 1.0,
         "desc": "州「SP」变化-70.00", "pct_reliable": True},
    ],
}
FZ = {"ok": True, "formula": "订单量 × 客单价", "factors": [
    {"factor": "order_count", "label": "订单量", "change": -1.0, "share": 0.8},
    {"factor": "aov", "label": "客单价", "change": -5.0, "share": 0.2}]}


# ---------------------------------------------------------------- 候选 / 判官

def test_deterministic_candidates_from_att():
    cands = hypotheses.deterministic_candidates(ATTR)
    assert len(cands) == 1 and cands[0]["kind"] == "dim_value"
    assert cands[0]["dim"] == "state" and cands[0]["value"] == "SP"
    assert cands[0]["source"] == "analyze"


def test_judge_dim_value_verified_and_reasons():
    ok = hypotheses.verify_hypothesis(
        {"kind": "dim_value", "dim": "state", "value": "SP"}, ATTR)
    assert ok["verified"] and ok["evidence"]["delta"] == -70.0

    bad_dim = hypotheses.verify_hypothesis(
        {"kind": "dim_value", "dim": "不存在", "value": "SP"}, ATTR)
    assert not bad_dim["verified"] and "候选拆解范围" in bad_dim["reason"]

    bad_val = hypotheses.verify_hypothesis(
        {"kind": "dim_value", "dim": "state", "value": "MG"}, ATTR)
    assert not bad_val["verified"] and "不存在" in bad_val["reason"]

    # 不可加维度：分段之和 -30 ≠ 总变化 -70 → 占比不可靠 → 否
    bad_add = hypotheses.verify_hypothesis(
        {"kind": "dim_value", "dim": "bad", "value": "x"}, ATTR)
    assert not bad_add["verified"] and "不可靠" in bad_add["reason"]


def test_judge_factor_verified_and_reasons():
    ok = hypotheses.verify_hypothesis({"kind": "factor", "factor": "订单量"}, ATTR,
                                      factor_split=FZ)
    assert ok["verified"] and ok["evidence"]["share"] == 0.8
    miss = hypotheses.verify_hypothesis({"kind": "factor", "factor": "运费"}, ATTR,
                                        factor_split=FZ)
    assert not miss["verified"] and "不在" in miss["reason"]
    none_fz = hypotheses.verify_hypothesis({"kind": "factor", "factor": "订单量"}, ATTR,
                                           factor_split={"ok": False})
    assert not none_fz["verified"] and "未定义" in none_fz["reason"]
    unknown = hypotheses.verify_hypothesis({"kind": "???", "x": 1}, ATTR)
    assert not unknown["verified"] and "未知假设类型" in unknown["reason"]


# ---------------------------------------------------------------- 提假设（Mock LLM）

class _LLM:
    def __init__(self, text): self.text, self.calls = text, 0
    def complete(self, prompt):
        self.calls += 1
        return self.text


def test_propose_merges_llm_and_base_with_dedup():
    llm = _LLM('[{"kind":"factor","factor":"订单量","claim":"单量少了"},'
               '{"kind":"dim_value","dim":"state","value":"SP","claim":"重复候选"}]')
    hyps = hypotheses.propose_hypotheses(ATTR, llm, n=3)
    # LLM 的 factor 假设 + base 的 state·SP（LLM 那条 dim_value 与 base 去重）
    kinds = {(h["kind"], h.get("dim"), h.get("value"), h.get("factor")) for h in hyps}
    assert ("factor", None, None, "订单量") in kinds
    assert ("dim_value", "state", "SP", None) in kinds
    assert len(hyps) == 2 and llm.calls == 1


def test_propose_falls_back_to_deterministic_on_bad_json():
    llm = _LLM("这不是 JSON")
    hyps = hypotheses.propose_hypotheses(ATTR, llm)
    assert len(hyps) == 1 and hyps[0]["source"] == "analyze"


# ---------------------------------------------------------------- 环：分流 + 排序

def test_run_hypothesis_loop_splits_and_sorts():
    llm = _LLM('[{"kind":"dim_value","dim":"state","value":"SP","claim":"主因 SP"},'
               '{"kind":"dim_value","dim":"state","value":"MG","claim":"主因 MG"},'
               '{"kind":"factor","factor":"订单量","claim":"单量少了"}]')
    res = hypotheses.run_hypothesis_loop(None, ATTR, llm, factor_split=FZ)
    assert res["n_verified"] == 2 and res["n_rejected"] == 1
    # 被否的是「取值不存在」那条
    assert "不存在" in res["rejected"][0]["reason"]
    assert res["verified"][0]["evidence"]["delta"] == -70.0        # |delta| 最大者排第一
    assert res["n"] == 3


def test_run_hypothesis_loop_no_llm_returns_deterministic():
    res = hypotheses.run_hypothesis_loop(None, ATTR, llm=None)
    assert res["n"] == 1 and res["n_verified"] == 1 and res["n_rejected"] == 0
