"""
evaluation/eval_decisions.py
============================
多 Agent 决策质量评测：归因 Agent"下一步该往哪拆"的命中率（验收点 B）。

对一组"带专家真凶"的归因追问用例，用（真实或 mock）LLM 跑 `decide_drill`，
对比其 action 与专家标注，产出 `decision_accuracy` —— 把"Agent 做了决策"
变成"决策对不对"的可量化指标。

用法：
    - 离线基线（llm=None，纯规则兜底）：
        python -m evaluation.eval_decisions
    - 接真实 LLM（把 llm 换成 DeepSeek/OpenAI 兼容客户端）：
        参考 tests/conftest 的 MockLLM 形式实现 __call__/complete 后传入 run(llm=...)
    - 作为 pytest 用例被 tests/test_attribution_decisions.py 引用，保证不改坏。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from sqlpa.business.attribution import decide_drill

# 标注用例：att 为（可简化的）归因结果，expert_action 为专家期望的"下一步"。
# 三人规则基线应为 2/3，接 LLM 后可达 3/3 —— 从而量化"Agent 决策"带来了多少提升。
ANNOTATED: List[Dict] = [
    {"name": "gmv-单量因清晰追问", "metric": "gmv", "question": "那为什么单量少了？",
     "att": {"top_contributors": [
         {"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00", "pct_of_change": 0.9}]},
     "expert_action": "drill"},
    {"name": "gmv-贡献分散需换维度", "metric": "gmv", "question": "那为什么呢？",
     "att": {"top_contributors": [
         {"desc": "品类「a」变化-30", "pct_of_change": 0.30},
         {"desc": "品类「b」变化-28", "pct_of_change": 0.28}]},
     "expert_action": "switch_dim"},
    {"name": "gmv-应做单量×客单价因子分解", "metric": "gmv", "question": "是单量还是客单价的问题？",
     "att": {"top_contributors": [
         {"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00", "pct_of_change": 0.9}]},
     "expert_action": "factorize"},
]


def run(annotated: Optional[List[Dict]] = None, llm=None) -> Dict:
    """跑一遍注解集，返回决策命中率汇总与逐条明细。"""
    ann = annotated if annotated is not None else ANNOTATED
    cases: List[Dict] = []
    correct = 0
    for c in ann:
        dec = decide_drill(c["att"], c["question"], llm)
        hit = dec.get("action") == c.get("expert_action")
        correct += int(hit)
        cases.append({"name": c.get("name"), "action": dec.get("action"),
                      "expert": c.get("expert_action"), "source": dec.get("decision_source"),
                      "hit": hit, "reason": dec.get("reason")})
    return {"total": len(ann), "correct": correct,
            "accuracy": round(correct / len(ann), 4) if ann else 1.0,
            "llm_used": llm is not None, "cases": cases}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))