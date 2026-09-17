"""
sqlpa.llm.mock_llm
==================
离线 Mock LLM：不调用任何外部 API。

用途：
  - 在没有 API Key 的环境下**演示并验证多 Agent 编排链路真实可跑**（路由→Schema→生成→执行→诊断→修复→校验）。
  - 绝不用于产生"真实准确率"——只有接真实 LLM（openai_compat）跑 Spider/BIRD 才算数。

模式：
  - answer_key: {question: correct_sql}  -> 生成正确 SQL（用于 EX=1 的演示）。
  - fail_first_map: {question: wrong_sql} -> 首次返回错误 SQL（触发真实 sqlite 报错），
    后续返回 answer_key 里的正确 SQL（模拟"自愈修复成功"）。
"""
from __future__ import annotations

from typing import Dict, Optional
from .base import LLMProvider


class MockLLM(LLMProvider):
    def __init__(self, answer_key: Optional[Dict[str, str]] = None,
                 fail_first_map: Optional[Dict[str, str]] = None):
        self.answer_key = answer_key or {}
        self.fail_first_map = fail_first_map or {}
        self._attempt: Dict[str, int] = {}

    def generate_sql(self, question: str, schema_text: str, plan: str = "",
                     previous_try: Optional[Dict] = None,
                     metric_constraint: str = "", dialect_hint: str = "") -> str:
        q = question
        if q in self.fail_first_map:
            self._attempt[q] = self._attempt.get(q, 0) + 1
            if self._attempt[q] == 1:
                return self.fail_first_map[q]          # 首次故意给错，触发修复链路
            return self.answer_key.get(q, self.fail_first_map[q])
        return self.answer_key.get(q, "SELECT 1")

    def diagnose_error(self, sql: str, error: str, schema_text: str) -> str:
        # 防御：调用方可能传 None（历史上 pipeline/langgraph 都踩过这个坑）
        error = error or "result_mismatch"
        return f"[mock-diagnose] 疑似 {str(error)[:80]}; 建议修正列名/别名后重试"

    def validate_semantics(self, question: str, sql: str,
                           exec_result: Dict) -> Dict:
        return {"valid": True, "reason": "execution ok (mock)"}

    def difficulty_judge(self, question: str, schema_text: str) -> str:
        """Mock：永远认为"不复杂"，保证路由结果确定、测试可复现。

        真实实现见 openai_compat.OpenAICompatLLM.difficulty_judge。
        """
        return "simple"

    def complete(self, prompt: str) -> str:
        return ""

    def review_sql(self, question: str, sql: str, schema_text: str,
                   exec_result=None) -> dict:
        return {"pass": True, "issues": [], "feedback": ""}
