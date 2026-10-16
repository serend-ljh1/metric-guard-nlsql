"""P0-2：LLM 成本 / 延迟面板（明细落库 + 单次调用延迟分位 + 查询入口）。

要点：
  - 「单次调用延迟分位（p95）」只能由**逐次明细**累出，故 `llm_calls` 表按"一次调用一行"
    落库（审计表只记"每次查询的合计"，算不出分位）；
  - 分位口径用线性插值（确定性、可复现），内存 `latency_stats` 与落库汇总一致；
  - 失败的调用也记一行（ok=0）：超时同样是延迟的一部分，不该被"只记成功"抹掉；
  - `/api/llm-usage` 仅 admin 可见：这是跨用户的成本数据。
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from sqlpa.business import storage
from sqlpa.llm.openai_compat import OpenAICompatLLM


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


def _llm(pool=("m1",)):
    return OpenAICompatLLM(api_key="test-key", base_url="http://llm.test/v1",
                           model_pool=list(pool), max_retries=0, timeout=1.0)


def test_percentile_linear_interpolation():
    from sqlpa.business.storage import _percentile
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _percentile(vals, 0.5) == 3.0
    assert _percentile(vals, 0.95) == pytest.approx(4.8)
    assert _percentile([], 0.95) == 0.0
    assert _percentile([7.0], 0.95) == 7.0


def test_real_call_records_token_and_latency_detail():
    llm = _llm()
    with patch("urllib.request.urlopen", lambda req, timeout=None: _Resp(_ok())):
        llm._chat([{"role": "user", "content": "hi"}])
    st = llm.stats()
    assert st["usage"]["total_tokens"] == 15
    assert st["latency"]["n"] == 1
    assert st["latency"]["p95_ms"] >= 0.0
    # 明细落库：面板能按模型聚合
    s = storage.llm_usage_summary(days=1)
    assert s["calls"] == 1
    assert s["prompt_tokens"] == 10 and s["completion_tokens"] == 5
    assert "m1" in s["by_model"]
    assert s["by_model"]["m1"]["p95_ms"] >= 0.0


def test_failed_call_recorded_with_ok_zero():
    llm = _llm(("m1",))

    def boom(req, timeout=None):
        raise RuntimeError("404 model_not_found")

    with patch("urllib.request.urlopen", boom), \
         patch("sqlpa.llm.openai_compat.time.sleep"):
        with pytest.raises(RuntimeError):
            llm._chat([{"role": "user", "content": "hi"}])
    s = storage.llm_usage_summary(days=1)
    assert s["calls"] == 1 and s["failed_calls"] == 1
    assert llm.stats()["latency"]["n"] == 1


def test_reset_stats_clears_latency_log():
    llm = _llm()
    llm._record_call("m1", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                     12.5)
    assert llm.stats()["latency"]["n"] == 1
    llm.reset_stats()
    assert llm.stats()["latency"]["n"] == 0


def test_usage_summary_synthesizes_percentiles():
    for ms in (10, 20, 30, 40, 100):
        storage.insert_llm_call({"model": "mX", "prompt_tokens": 100,
                                 "completion_tokens": 50, "total_tokens": 150,
                                 "cost": 0.001, "latency_ms": ms, "ok": 1})
    s = storage.llm_usage_summary(days=1)
    assert s["calls"] == 5 and s["total_tokens"] == 750
    assert s["latency_ms"]["p50"] == 30.0
    assert s["latency_ms"]["p95"] == pytest.approx(88.0)
    assert s["latency_ms"]["max"] == 100.0
    assert s["by_model"]["mX"]["calls"] == 5
    assert s["by_model"]["mX"]["p95_ms"] == pytest.approx(88.0)


def _client():
    import api
    from fastapi.testclient import TestClient
    return TestClient(api.app)


def test_llm_usage_endpoint_requires_admin(monkeypatch):
    import api  # noqa: F401  先导入，确保 app 已组装
    monkeypatch.setenv("SQLPA_API_TOKENS", "admintok:admin,usertok:analyst")
    c = _client()
    assert c.get("/api/llm-usage", headers={"X-API-Token": "usertok"}).status_code == 403
    r = c.get("/api/llm-usage", headers={"X-API-Token": "admintok"})
    assert r.status_code == 200
    body = r.json()
    assert "latency_ms" in body and "by_model" in body
    # 未带凭据（且已配置 token 表）→ 鉴权失败
    assert c.get("/api/llm-usage").status_code == 401
