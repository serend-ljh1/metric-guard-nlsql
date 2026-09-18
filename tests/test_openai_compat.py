"""
tests/test_openai_compat.py
===========================
OpenAI 兼容真实客户端(OpenAICompatLLM)的**离线单元测试**。

这是全项目唯一"真实 LLM 调用层"的测试：不碰真实 API，通过 patch 打桩
`urllib.request.urlopen` 模拟 HTTP 响应，把【模型池切换 / 重试退避 / 空内容
fallback / JSON 解析兜底 / prompt 构造 / token 计价】这些纯逻辑分支钉住。

为什么必须离线：
  - 真实调用要 API Key、花钱、结果非确定，不适合进 pytest（会 flaky）；
  - 而这里测的是"客户端自身的确定性行为"，用打桩即可完全离线覆盖。
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from sqlpa.llm.openai_compat import (
    OpenAICompatLLM, _is_retryable, _should_switch,
)


class _Resp:
    """模拟 urlopen 返回值：支持 with 上下文 + read() 返回 JSON bytes。"""

    def __init__(self, payload: dict):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._p).encode("utf-8")


def _ok(content="SELECT 1", usage=None):
    return {"choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5,
                               "total_tokens": 15}}


def _make_llm(model_pool=("m1", "m2"), max_retries=1, **kw):
    return OpenAICompatLLM(api_key="test-key", base_url="http://llm.test/v1",
                           model_pool=list(model_pool), max_retries=max_retries,
                           timeout=1.0, **kw)


def _patch_urlopen(cases, calls=None):
    """按序返回 cases 中的响应或抛出其中的异常，并把每次请求记入 calls。"""
    cases = list(cases)

    def fake(req, timeout=None):
        if calls is not None:
            calls.append((req.full_url, json.loads(req.data.decode("utf-8"))))
        item = cases.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _Resp(item)

    return fake


# ---------- 错误分类（纯函数） ----------

def test_is_retryable_classifies_transient_errors():
    # 瞬态：应重试
    for msg in ("429 rate limit", "got timeout", "502 upstream", "connection reset", "500 internal"):
        assert _is_retryable(msg), f"应判为可重试: {msg}"
    # 致命：不重试
    for msg in ("404 model not found", "401 unauthorized", "insufficient_quota"):
        assert not _is_retryable(msg), f"不应判为可重试: {msg}"


def test_should_switch_classifies_fatal_errors():
    for msg in ("404 model not found", "403 insufficient_quota", "free quota",
                "401 invalid api key", "400 bad request",
                "context_length_exceeded", "model overloaded"):
        assert _should_switch(msg), f"应切换模型: {msg}"
    assert not _should_switch("429 rate limit")   # 瞬态 → 同模型重试，不切换
    assert not _should_switch("502 bad gateway")


# ---------- 构造器 ----------

def test_require_api_key(monkeypatch):
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError):
        OpenAICompatLLM(api_key=None, base_url="http://x")


def test_constructor_resolves_model_pool():
    llm = _make_llm()
    assert llm.last_model() == "m1"          # 默认从池首开始
    assert llm.model_pool == ["m1", "m2"]


# ---------- _chat：模型池调用 ----------

def test_chat_success_records_usage_and_model():
    llm = _make_llm(("m1",))
    with patch("urllib.request.urlopen", _patch_urlopen([_ok("SELECT 42")])), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        content = llm._chat([{"role": "user", "content": "hi"}])
    assert content == "SELECT 42"
    assert llm.last_model() == "m1"
    assert llm.stats()["usage"]["total_tokens"] == 15


def test_chat_retries_transient_error_on_same_model():
    """429 应同模型重试，不切换模型。"""
    calls: list = []
    cases = [RuntimeError("429 rate limit"), _ok()]
    with patch("urllib.request.urlopen", _patch_urlopen(cases, calls)), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        llm = _make_llm(("m1", "m2"), max_retries=1)
        llm._chat([{"role": "user", "content": "hi"}])
    models = [c[1]["model"] for c in calls]
    assert models == ["m1", "m1"]            # 两次都落在 m1 → 未误切换


def test_chat_switches_model_on_fatal_error():
    """404(不可重试) → 立即切到下一模型。"""
    calls: list = []
    cases = [RuntimeError("404 model not found"), _ok()]
    with patch("urllib.request.urlopen", _patch_urlopen(cases, calls)), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        llm = _make_llm(("m1", "m2"), max_retries=1)
        content = llm._chat([{"role": "user", "content": "hi"}])
    assert content == "SELECT 1"
    models = [c[1]["model"] for c in calls]
    assert models == ["m1", "m2"]            # 只各试一次


def test_chat_switches_model_on_empty_content():
    """模型返回空内容 → 视为失败，切下一模型。"""
    calls: list = []
    cases = [_ok(content="   "), _ok(content="SELECT 1")]
    with patch("urllib.request.urlopen", _patch_urlopen(cases, calls)), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        llm = _make_llm(("m1", "m2"), max_retries=1)
        assert llm._chat([{"role": "user", "content": "hi"}]) == "SELECT 1"
    assert [c[1]["model"] for c in calls] == ["m1", "m2"]


def test_chat_all_models_fail_raises():
    llm = _make_llm(("m1", "m2"), max_retries=0)
    cases = [RuntimeError("404 model_not_found")] * 2
    with patch("urllib.request.urlopen", _patch_urlopen(cases)), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        with pytest.raises(RuntimeError):
            llm._chat([{"role": "user", "content": "hi"}])


# ---------- prompt 构造 ----------

def test_generate_sql_injects_metric_constraint():
    """业务硬约束必须原样拼进 user context（防模型篡改公式）。"""
    calls: list = []
    with patch("urllib.request.urlopen", _patch_urlopen([_ok("SELECT ...")], calls)), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        llm = _make_llm()
        llm.generate_sql("总GMV是多少", "<schema>", metric_constraint="SUM(oi.price) AS gmv")
    ctx = calls[0][1]["messages"][1]["content"]
    assert "业务指标硬约束" in ctx
    assert "SUM(oi.price) AS gmv" in ctx


def test_generate_sql_injects_previous_error_for_repair():
    calls: list = []
    with patch("urllib.request.urlopen", _patch_urlopen([_ok("SELECT 2")], calls)), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        llm = _make_llm()
        llm.generate_sql("q", "<schema>",
                         previous_try={"sql": "SELECT 1",
                                       "error": "no such column: x"})
    ctx = calls[0][1]["messages"][1]["content"]
    assert "上一条 SQL" in ctx and "no such column: x" in ctx


# ---------- JSON 解析兜底 ----------

def test_review_sql_parses_strict_json():
    with patch("urllib.request.urlopen",
               _patch_urlopen([_ok('{"pass": false, "issues": ["a"], "feedback": "f"}')])), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        r = _make_llm().review_sql("q", "SQL", "<schema>")
    assert r == {"pass": False, "issues": ["a"], "feedback": "f"}


def test_review_sql_falls_back_on_bad_json():
    """LLM 返回非 JSON → 兜底 pass=True，不让评审阻塞流程。"""
    with patch("urllib.request.urlopen", _patch_urlopen([_ok("sorry i cant")])), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        r = _make_llm().review_sql("q", "SQL", "<schema>")
    assert r == {"pass": True, "issues": [], "feedback": ""}


def test_validate_semantics_parse_and_fallback():
    with patch("urllib.request.urlopen",
               _patch_urlopen([_ok('{"valid": false, "reason": "not matched"}')])), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        assert _make_llm().validate_semantics("q", "SQL", {"rows": []})["valid"] is False
    with patch("urllib.request.urlopen", _patch_urlopen([_ok("garbage")])), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        assert _make_llm().validate_semantics("q", "SQL", {"rows": []}) == \
            {"valid": True, "reason": "parse-fallback"}


# ---------- token 计价 ----------

def test_usage_pricing():
    llm = _make_llm()
    llm._acc_usage({"prompt_tokens": 1000, "completion_tokens": 2000,
                    "total_tokens": 3000})
    # 默认 0.14/1M 输入, 0.28/1M 输出
    expect = 1000 / 1e6 * 0.14 + 2000 / 1e6 * 0.28
    assert llm.stats()["cost"] == pytest.approx(expect, abs=1e-4)


def test_reset_stats_zeroes_usage():
    llm = _make_llm()
    llm._acc_usage({"prompt_tokens": 1000, "completion_tokens": 0, "total_tokens": 1000})
    llm.reset_stats()
    assert llm.stats()["usage"]["total_tokens"] == 0
    assert llm.stats()["cost"] == 0.0