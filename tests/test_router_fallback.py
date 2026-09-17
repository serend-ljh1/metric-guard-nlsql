"""路由 LLM 兜底测试（离线）。

背景：`difficulty_judge` 曾在 3 个 LLM 实现里都有定义却**从未被调用**，
`pipeline.route_llm_fallback` 配置也从未被读取——文档声称"隐式 JOIN 由轻量 LLM
二次判断兜底"，实际没有。本文件锁定该能力真实存在且行为可控。
"""
from __future__ import annotations

import pytest

from sqlpa.agents.router import route_decision, route_with_llm_fallback


class _JudgeLLM:
    """可控的假 LLM：difficulty_judge 返回预设值。"""

    def __init__(self, verdict: str = "complex", boom: bool = False):
        self.verdict = verdict
        self.boom = boom
        self.calls = 0

    def difficulty_judge(self, question: str, schema_text: str) -> str:
        self.calls += 1
        if self.boom:
            raise RuntimeError("judge 挂了")
        return self.verdict


SCHEMA = {"tables": [
    {"name": "orders", "columns": [{"name": "order_id"}, {"name": "customer_id"}]},
    {"name": "customers", "columns": [{"name": "customer_id"}, {"name": "customer_state"}]},
]}

# 不含表名、不含聚合/分组等逻辑信号 → 确定性路由判 simple
SIMPLE_Q = "列出所有客户的国家"


def test_deterministic_route_says_simple():
    """前提校验：这个词面问题确实被判为 simple，后续兜底测试才有意义。"""
    assert route_decision(SIMPLE_Q, SCHEMA)["decision"] == "simple"


def test_fallback_upgrades_simple_to_complex():
    llm = _JudgeLLM("complex")
    rt = route_with_llm_fallback(SIMPLE_Q, SCHEMA, llm=llm, enabled=True)
    assert rt["decision"] == "complex"
    assert rt["llm_fallback"] is True
    assert llm.calls == 1, "应只调用一次判定"


def test_fallback_disabled_does_not_call_llm():
    llm = _JudgeLLM("complex")
    rt = route_with_llm_fallback(SIMPLE_Q, SCHEMA, llm=llm, enabled=False)
    assert rt["decision"] == "simple"
    assert rt["llm_fallback"] is False
    assert llm.calls == 0, "兜底关闭时不得调用 LLM（否则白花 token）"


def test_fallback_verdict_simple_keeps_simple():
    llm = _JudgeLLM("simple")
    rt = route_with_llm_fallback(SIMPLE_Q, SCHEMA, llm=llm, enabled=True)
    assert rt["decision"] == "simple"
    assert rt["llm_fallback"] is False


def test_fallback_never_downgrades_complex():
    """已判 complex 的题不再调用 LLM（省一次调用），也不会被降级。"""
    q = "每个州的订单总额排名"          # 命中分组/聚合信号 → complex
    assert route_decision(q, SCHEMA)["decision"] == "complex"
    llm = _JudgeLLM("simple")
    rt = route_with_llm_fallback(q, SCHEMA, llm=llm, enabled=True)
    assert rt["decision"] == "complex"
    assert llm.calls == 0


def test_fallback_failure_is_silent():
    """兜底本身抛异常时不能影响主流程。"""
    llm = _JudgeLLM(boom=True)
    rt = route_with_llm_fallback(SIMPLE_Q, SCHEMA, llm=llm, enabled=True)
    assert rt["decision"] == "simple"
    assert rt["llm_fallback"] is False


def test_no_llm_means_no_fallback():
    rt = route_with_llm_fallback(SIMPLE_Q, SCHEMA, llm=None, enabled=True)
    assert rt["decision"] == "simple"
    assert rt["llm_fallback"] is False


def test_enabled_none_follows_settings_yaml():
    """enabled=None 时读 settings.yaml（本仓库为 true）→ 会调用 LLM。"""
    llm = _JudgeLLM("complex")
    rt = route_with_llm_fallback(SIMPLE_Q, SCHEMA, llm=llm, enabled=None)
    # settings.yaml: pipeline.route_llm_fallback = true
    assert llm.calls == 1
    assert rt["decision"] == "complex"
