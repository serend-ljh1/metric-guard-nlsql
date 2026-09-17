"""
sqlpa.llm.base
==============
LLM 抽象层：定义各 Agents 需要的最小接口，便于"真实 API / 离线 Mock / 其他后端"可插拔。

原则（对齐"推理归 LLM、计算归代码"）：
  - 本层只负责"语义/规划/纠错"类调用；SQL 执行与安全校验永远走 sandbox 确定性代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class LLMProvider(ABC):
    """所有 LLM 后端的统一接口。"""

    @abstractmethod
    def generate_sql(self, question: str, schema_text: str, plan: str = "",
                     previous_try: Optional[Dict] = None,
                     metric_constraint: str = "",
                     dialect_hint: str = "") -> str:
        """根据问题 + schema(+规划) 生成一条 SQL。

        metric_constraint: 业务模式注入的"指标公式硬约束"，要求 Writer 在 SELECT 中
        必须原样使用该计算表达式（防止大模型篡改 GMV/退货率公式）。
        dialect_hint: 目标数据库方言提示（如 MySQL/PostgreSQL），默认 SQLite。
        """

    @abstractmethod
    def diagnose_error(self, sql: str, error: str, schema_text: str) -> str:
        """对执行错误做诊断，返回根因与修复建议。"""

    @abstractmethod
    def validate_semantics(self, question: str, sql: str,
                           exec_result: Dict) -> Dict:
        """校验 SQL 执行结果与问题语义是否一致，返回 {"valid": bool, "reason": str}。"""

    @abstractmethod
    def difficulty_judge(self, question: str, schema_text: str) -> str:
        """LLM 层面的难度二次判断（用于兜底路由边界情形），返回 simple/complex。"""

    @abstractmethod
    def complete(self, prompt: str) -> str:
        """通用补全：业务语义层(MetricMatcher)等用它做自由度较高的文本理解。"""

    @abstractmethod
    def review_sql(self, question: str, sql: str, schema_text: str,
                   exec_result: Optional[Dict] = None) -> Dict:
        """评审 Agent：用独立标准审核 SQL，返回 {"pass":bool,"issues":[...],"feedback":str}。"""

    # ---- 用量/成本统计（可选能力，默认零成本实现）----
    # 真实客户端（OpenAICompatLLM）会覆盖这两个方法，累计 token 与估算成本；
    # Mock/离线后端沿用默认值，保证评测代码无需特判。
    def stats(self) -> Dict:
        """返回累计用量与成本：{"usage": {...}, "cost": float}。"""
        return {"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "cost": 0.0}

    def reset_stats(self) -> None:
        """重置累计用量（评测按题统计时逐题调用）。"""
        return None

    def last_model(self) -> str:
        """最近一次实际调用的模型名（默认空串：离线/Mock 后端无模型概念）。

        存在的意义：模型池会静默切换模型，评测产物若不记录实际模型，
        一旦中途切换，"绝对指标"就会变成多个模型的混合结果且无人察觉。
        """
        return ""


def build_schema_text(schema: Dict) -> str:
    """把 schema 结构转成分层提示词文本，供 LLM 使用。"""
    lines = []
    for t in schema.get("tables", []):
        cols = []
        for c in t.get("columns", []):
            ref = ""
            if c.get("references_table"):
                ref = f" -> {c['references_table']}.{c.get('references_col')}"
            pk = " PK" if c.get("primary_key") else ""
            cols.append(f"  - {c['name']} {c.get('dtype', 'TEXT')}{pk}{ref}")
        lines.append(f"表 {t['name']}:")
        lines.extend(cols)
    return "\n".join(lines)
