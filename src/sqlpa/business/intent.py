"""
sqlpa.business.intent
=====================
意图词表的**单一来源**：把散落在 Router / Attribution / service 里的
"追问语气词 / 定向维度疑问词"收敛成常量 + 函数。

为什么必须收敛：同一份词表此前在 4 处各写一遍（orchestrator._route、
orchestrator._attribution_chain、attribution.decide_drill、service._infer_drill_path），
语义强相关却互不引用 —— 任何一处增删词都会让"什么算追问"在不同 Agent 间不一致，
进而出现"路由认为是追问、归因认为不是"的分裂行为。集中一处、统一维护。
"""
from __future__ import annotations

# 追问语气词：出现其一即视为"用户在追问上一轮的结论"（而非在看数/做对比）。
FOLLOWUP_TONE_WORDS = (
    "那", "呢", "为什么", "为何", "继续", "下钻", "再看", "拆", "再",
)


def has_followup_tone(question: str) -> bool:
    """问题里是否含追问语气词（确定性，不依赖 LLM）。"""
    q = str(question or "")
    return any(k in q for k in FOLLOWUP_TONE_WORDS)
