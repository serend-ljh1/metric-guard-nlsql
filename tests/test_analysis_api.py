"""api.py 新增的「多 Agent 分析」端点冒烟测试（离线、数据无关）。
  - /api/analyze           非流式聚合：事件数组 + 最终结论/依据/决策/可视化
  - /api/analyze/stream    SSE 流式：text/event-stream，逐 Agent 帧，含 done 终帧
断言只涉及**结构完整性**（不确定具体告警值，因真实 olist.db 的"本月"数据量不定）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(monkeypatch_module, real_olist_db):
    import api
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch_module.delenv(k, raising=False)
    monkeypatch_module.setenv("SQLPA_API_TOKENS", "")
    monkeypatch_module.setenv("SQLPA_DEFAULT_ROLE", "analyst")
    return TestClient(api.app)


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


def test_analyze_aggregate(client):
    r = client.post("/api/analyze",
                    json={"question": "本月 GMV 为什么跌？", "role": "analyst"})
    assert r.status_code == 200
    body = r.json()
    assert "events" in body and "final" in body
    f = body["final"]
    assert isinstance(f["conclusion"], str) and f["conclusion"], "应产出诊断结论"
    assert isinstance(f["evidence"], list)
    assert "actions" in f and "decision" in f
    assert set(f["chart"].keys()) >= {"waterfall", "share", "tree", "factors", "summary"}
    # 事件里应出现全 6 个 Agent 的 agent_done
    names = {e.get("name") for e in body["events"] if e.get("type") == "agent_done"}
    for expect in ("RouterAgent", "MetricMatcher", "ExecutorAgent",
                   "AttributionAgent", "ConclusionAgent", "DecisionAgent"):
        assert expect in names, f"缺少 {expect} 的完成事件"


def test_analyze_stream_sse(client):
    with client.stream("POST", "/api/analyze/stream",
                       json={"question": "本月 GMV 为什么跌？", "role": "analyst"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = "".join(resp.iter_text())
    frames = [ln for ln in raw.splitlines() if ln.startswith("data: ")]
    types = [json.loads(ln[6:])["type"] for ln in frames]
    assert "session_start" in types
    assert "done" in types
    assert types.count("agent_start") >= 4          # 至少有 Router/Metric/Executor/归因
    assert types.count("agent_done") >= 6           # 六个 Agent 全部完成


def test_analyze_unmatched(client):
    r = client.post("/api/analyze",
                    json={"question": "外星人数量是多少？", "role": "analyst"})
    body = r.json()
    f = body["final"]
    assert f["route"] == "unmatched"
    assert f["ok"] is False
    assert f["conclusion"], "口径外也应给出兜底结论"


def test_analyze_report(client):
    """报告端点输出 Markdown：含诊断结论 + 决策收口段。"""
    r = client.post("/api/analyze/report",
                    json={"question": "本月 GMV 为什么跌？", "role": "analyst"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    md = body["report"]
    assert "诊断结论" in md
    assert "决策收口" in md
    assert "复现 SQL" in md