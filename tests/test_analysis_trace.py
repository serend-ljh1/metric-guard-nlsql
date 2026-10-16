"""P0-3：编排 trace 结构化落库 + 可重放。

要点：
  - run_analysis 把推给 SSE 的**同一串事件**先记进内存流水，分析结束 best-effort 落库；
  - trace 存的是完整事件 + 最终载荷，因此"过程可看"升级为"过程可审计、可重放"；
  - 重放入口按 analysis_id 取回，analyst 只能放自己的、admin 可放全部。
"""
from __future__ import annotations


def test_trace_storage_roundtrip():
    from sqlpa.business import storage
    events = [{"type": "session_start", "analysis_id": "a1"},
              {"type": "done", "payload": {"ok": True}}]
    storage.insert_trace("a1", "GMV 为什么跌", events, {"ok": True, "analysis_id": "a1"},
                         username="alice")
    t = storage.get_trace("a1")
    assert t is not None
    assert t["question"] == "GMV 为什么跌"
    assert t["username"] == "alice"
    assert [e["type"] for e in t["events"]] == ["session_start", "done"]
    assert t["final"]["ok"] is True
    assert t["ok"] == 1
    # 列表只回摘要（不带事件流水，避免响应过大）
    rows = storage.list_traces(limit=5, username="alice")
    assert rows and "events" not in rows[0]
    assert rows[0]["analysis_id"] == "a1"


def test_run_analysis_persists_replayable_trace():
    """未命中口径的分析也留 trace：事件序列与 emit 收到的完全一致。"""
    from sqlpa.analysis.orchestrator import run_analysis
    from sqlpa.business import storage
    from sqlpa.business.metric_config import load_config
    seen: list = []
    final = run_analysis("今天天气怎么样", load_config(), None, "unused.db", None,
                         emit=seen.append)
    assert final["ok"] is False
    t = storage.get_trace(final["analysis_id"])
    assert t is not None
    # 落库的事件 = 推给 SSE 的事件（同一串）；首帧 session_start、末帧 done
    assert [e["type"] for e in t["events"]] == [e["type"] for e in seen]
    assert t["events"][0]["type"] == "session_start"
    assert any(e["type"] == "done" for e in t["events"])


def test_get_trace_returns_latest_for_same_id(monkeypatch):
    from sqlpa.business import storage
    storage.insert_trace("dup", "q1", [{"type": "session_start"}], {"ok": True})
    storage.insert_trace("dup", "q2", [{"type": "session_start"}], {"ok": True})
    t = storage.get_trace("dup")
    assert t["question"] == "q2"          # 取最近一次


def test_trace_endpoints_require_and_replay(monkeypatch):
    import api
    from fastapi.testclient import TestClient
    from sqlpa.business import storage
    monkeypatch.setenv("SQLPA_API_TOKENS", "admintok:admin,usertok:analyst,owner:analyst")
    storage.insert_trace("aid-replay", "q",
                         [{"type": "session_start"}, {"type": "done", "payload": {"ok": True}}],
                         {"ok": True, "analysis_id": "aid-replay"}, username="token:deadbeef")
    c = TestClient(api.app)
    r = c.get("/api/analyze/trace/aid-replay", headers={"X-API-Token": "admintok"})
    assert r.status_code == 200
    body = r.json()
    assert [e["type"] for e in body["events"]] == ["session_start", "done"]
    assert body["final"]["ok"] is True
    # 非本人（analyst）不可重放他人 trace
    assert c.get("/api/analyze/trace/aid-replay",
                 headers={"X-API-Token": "usertok"}).status_code == 403
    assert c.get("/api/analyze/trace/nope",
                 headers={"X-API-Token": "admintok"}).status_code == 404
    # 列表入口
    assert c.get("/api/analyze/traces", headers={"X-API-Token": "admintok"}).status_code == 200
