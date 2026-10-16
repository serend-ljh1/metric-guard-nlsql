"""
sqlpa.llm.base
==============
LLM 抽象层：定义各 Agents 需要的最小接口，便于"真实 API / 离线 Mock / 其他后端"可插拔。

原则（对齐"推理归 LLM、计算归代码"）：
  - 本层只负责**文本理解与表述**类调用（意图识别兜底、结论/归因讲解、续问改写）；
  - **不提供 SQL 生成接口**：SQL 由 sqlpa.business.compiler 从指标配置确定性编译，
    系统不做开放式 SQL 生成；SQL 执行与安全校验永远走 sandbox 确定性代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict


class LLMProvider(ABC):
    """所有 LLM 后端的统一接口。"""

    @abstractmethod
    def complete(self, prompt: str) -> str:
        """通用补全：业务语义层(MetricMatcher)等用它做自由度较高的文本理解。"""

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
