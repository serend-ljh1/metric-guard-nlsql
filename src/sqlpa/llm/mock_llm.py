"""
sqlpa.llm.mock_llm
==================
离线 Mock LLM：不调用任何外部 API。

用途：在没有 API Key 的环境下，让依赖 LLM 的判断/表述环节（意图识别兜底、结论生成、
归因点评、续问改写）**统一返回空结果**，调用方据此走确定性降级路径，从而保证
离线测试与无 Key 演示完全可复现。

注意：本实现不代表任何真实模型能力，**不用于产出"真实准确率"**——只有接真实 LLM
（openai_compat）跑评测才算数。
"""
from __future__ import annotations

from .base import LLMProvider


class MockLLM(LLMProvider):
    """恒返回空串的离线实现：所有 LLM 触点均降级为确定性行为。"""

    def complete(self, prompt: str) -> str:
        return ""