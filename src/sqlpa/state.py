"""
sqlpa.state
===========
多智能体协作的全局任务级状态（LangGraph State 的等价物）。

设计原则（对齐"状态与业务/计算分层"）：
  - 这是"短时工作记忆"，单次查询任务内共享、任务结束即销毁。
  - 字段聚合了评测所需的全部中间产物，便于可观测与断点续跑。
  - 数字可 JSON 序列化，配合 Checkpoint 做持久化。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class SqlAgentState:
    # ---- 输入 ----
    user_question: str = ""
    db_id: str = ""

    # ---- Schema / 计划 ----
    filtered_schema: Dict[str, Any] = field(default_factory=dict)   # 裁减后的 schema
    schema_prompt: str = ""                                        # 提供给 LLM 的 schema 文本
    query_plan: str = ""                                           # 结构化查询规划

    # ---- 生成与执行 ----
    candidate_sql_list: List[str] = field(default_factory=list)    # 并行生成的多候选
    elected_sql: str = ""                                          # 最终选中的 SQL
    current_sql: str = ""                                          # 当前待执行 SQL
    exec_result: Dict[str, Any] = field(default_factory=dict)      # 执行结果/报错

    # ---- 自愈 ----
    repair_retry_count: int = 0                                    # 纠错重试计数器（护栏）
    max_repair_round: int = 3
    error_diagnosis: str = ""                                      # 本轮诊断结论
    repair_history: List[str] = field(default_factory=list)        # 每次修复摘要

    # ---- 输出 ----
    final_sql: str = ""
    final_answer: str = ""
    final_valid: bool = False

    # ---- 路由 / 元信息 ----
    route_decision: str = ""      # simple | complex
    terminated: bool = False
    terminate_reason: str = ""    # ok | max_retry | security | no_valid_candidate
    latency_ms: float = 0.0
    token_usage: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def new(cls, question: str, db_id: str, max_repair_round: int = 3) -> "SqlAgentState":
        st = cls(user_question=question, db_id=db_id, max_repair_round=max_repair_round)
        return st
