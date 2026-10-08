"""HITL 闭环回归：工单能推得动、能重跑验证、同一异常不刷屏。

本轮评审发现的问题：写入是真、状态机是真，但 `hitl.resolve` 全仓零调用者、
`api.py` 没有任何 HITL 端点 —— "闭环"只是"写进队列"。而且在改造前，
存储是 jsonl 原地重写（会静默丢行）、工单没有幂等键（同一异常每次分析都新开一张）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sqlpa.business import hitl, storage
from sqlpa.business.metric_config import load_config

CFG = load_config()


# ---------------------------------------------------------------- 入队与幂等

def test_enqueue_is_idempotent_per_metric_and_period(tmpdir_clean):
    """同一 (指标, 时间窗) 重复告警 → 复用同一张工单，不刷屏。"""
    f = tmpdir_clean / "hitl.jsonl"
    rid1 = hitl.enqueue("GMV 异常", "gmv", "", "波动超阈值", time_spec="2018-03", path=f)
    rid2 = hitl.enqueue("GMV 异常", "gmv", "", "波动超阈值", time_spec="2018-03", path=f)
    assert rid1 == rid2, "同一异常不应重复开工单"
    # 不同时间窗 → 新工单
    rid3 = hitl.enqueue("GMV 异常", "gmv", "", "波动超阈值", time_spec="2018-04", path=f)
    assert rid3 != rid1
    assert len(storage.list_hitl("all", limit=50)) == 2


def test_enqueue_records_structured_context(tmpdir_clean):
    """时间窗与维度要**结构化**存下来 —— 重跑验证依赖它们，不能只写进自由文本。"""
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03",
                       dims=["state", "category"])
    rec = hitl.get(rid)
    assert rec["time_spec"] == "2018-03"
    assert rec["dims"] == ["state", "category"]


# ---------------------------------------------------------------- 状态流转

def test_resolve_transitions_are_recorded(tmpdir_clean):
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03")
    assert hitl.resolve(rid, "in_progress", human_note="已认领", actor="alice")
    assert hitl.resolve(rid, "fixed", human_note="已修数据", actor="alice")
    assert hitl.get(rid)["status"] == "fixed"
    hist = hitl.history(rid)
    assert [h["to_status"] for h in hist] == ["in_progress", "fixed"]
    assert hist[0]["from_status"] == "pending" and hist[0]["actor"] == "alice"
    assert hist[1]["note"] == "已修数据"
    # 非法状态拒绝、未知工单拒绝
    assert hitl.resolve(rid, "not_a_status") is False
    assert hitl.resolve("no-such-id", "fixed") is False


def test_resolve_writes_audit_trail(tmpdir_clean):
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03")
    hitl.resolve(rid, "dismissed", human_note="误报", actor="bob")
    rows = storage.list_audit(limit=20)
    assert any("HITL" in (r.get("reject_reason") or "") and r.get("mode") == "hitl"
               for r in rows), "人工处理动作必须进审计"


# ---------------------------------------------------------------- 重跑验证（闭环的关键）

class _FakeAnalyze:
    """可控的 analyze 替身：切换 abnormal 就能模拟"修好了"与"没修好"。"""

    def __init__(self, abnormal: bool):
        self.abnormal = abnormal
        self.calls = []

    def __call__(self, cfg, db, metric, current_spec=None, dims=None,
                 threshold_pct=0.05):
        self.calls.append({"metric": metric, "spec": current_spec, "dims": dims})
        return {"ok": True, "is_abnormal": self.abnormal, "metric": metric,
                "change_pct": -0.2 if self.abnormal else 0.01,
                "sql": "SELECT 1", "dims": []}


def test_verify_reopens_when_still_abnormal(monkeypatch, tmpdir_clean):
    """人工说"修好了"不算数：重跑仍异常 → reopened。"""
    from sqlpa.business import attribution
    fake = _FakeAnalyze(abnormal=True)
    monkeypatch.setattr(attribution, "analyze", fake)
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03", dims=["state"])
    hitl.resolve(rid, "fixed", human_note="我修好了", actor="alice")

    out = hitl.verify(rid, CFG, None, actor="api:admin")
    assert out["ok"] and out["status"] == "reopened"
    assert hitl.get(rid)["status"] == "reopened"
    assert fake.calls and fake.calls[0]["spec"] == "2018-03"
    assert fake.calls[0]["dims"] == ["state"], "重跑必须用工单里存的时间窗与维度"


def test_verify_closes_when_recovered(monkeypatch, tmpdir_clean):
    """数据恢复 → verified，且工单退出待办队列。"""
    from sqlpa.business import attribution
    monkeypatch.setattr(attribution, "analyze", _FakeAnalyze(abnormal=False))
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03")
    hitl.resolve(rid, "fixed", actor="alice")

    out = hitl.verify(rid, CFG, None)
    assert out["ok"] and out["status"] == "verified"
    assert storage.list_hitl("verified", limit=10)[0]["record_id"] == rid
    assert all(r["record_id"] != rid for r in hitl.queue(status="pending"))


def test_verify_refuses_without_time_window(tmpdir_clean):
    """缺时间窗 → 明确拒绝重跑（不能拿"没跑"当"已验证"）。"""
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动")       # 不传 time_spec
    out = hitl.verify(rid, CFG, None)
    assert out["ok"] is False and "时间窗" in out["reason"]
    assert hitl.get(rid)["status"] == "pending"


# ---------------------------------------------------------------- API 端点

@pytest.fixture
def client(monkeypatch):
    import api
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox
    monkeypatch.setenv("SQLPA_API_TOKENS", "tok-admin:admin,tok-analyst:analyst")
    # 避免依赖真实业务库：verify 用到 _db_path/_cfg
    monkeypatch.setattr(api, "_db_path", lambda: "unused.db")
    monkeypatch.setattr(api, "_sandbox", lambda: SqlSandbox(
        "", ExecConfig()) if False else None)
    monkeypatch.setattr(api, "_cfg", lambda: CFG)
    return TestClient(api.app)


_ADMIN = {"X-API-Token": "tok-admin"}
_ANALYST = {"X-API-Token": "tok-analyst"}


def test_hitl_endpoints_require_auth_and_admin(client):
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03")
    assert client.get("/api/hitl").status_code == 401
    assert client.get("/api/hitl", headers=_ANALYST).status_code == 200
    assert client.get(f"/api/hitl/{rid}/history", headers=_ANALYST).status_code == 200
    # 推进只有 admin 能做
    assert client.post(f"/api/hitl/{rid}/decide", json={"status": "in_progress"},
                       headers=_ANALYST).status_code == 403
    # 非法状态
    assert client.post(f"/api/hitl/{rid}/decide", json={"status": "nope"},
                       headers=_ADMIN).status_code == 400
    ok = client.post(f"/api/hitl/{rid}/decide",
                     json={"status": "in_progress", "note": "认领"}, headers=_ADMIN)
    assert ok.status_code == 200 and ok.json()["status"] == "in_progress"
    # 未知工单
    assert client.post("/api/hitl/nope/decide", json={"status": "fixed"},
                       headers=_ADMIN).status_code == 404
    assert client.get("/api/hitl/nope/history", headers=_ANALYST).status_code == 404


def test_confirmations_endpoints(client, monkeypatch):
    """待确认口径可查、可驳回、可改选（改选后旧记录作废）。"""
    from sqlpa.business import confirm as _confirm
    spec = {"question": "各州卖得怎么样", "metric": "gmv", "metric_name": "GMV",
            "dims": ["state"], "filters": [("time_range", "本月")],
            "time_grain": None, "method": "llm"}
    cid = _confirm.persist(spec)
    listed = client.get("/api/confirmations", headers=_ANALYST).json()
    assert any(c["id"] == cid for c in listed)

    # 改选：换指标 → 新 id，旧记录 dismissed
    r = client.post(f"/api/confirmations/{cid}/override?metric=order_count",
                    headers=_ANALYST)
    assert r.status_code == 200
    new_id = r.json()["confirm_id"]
    assert new_id != cid
    assert storage.get_confirmation(cid)["status"] == "dismissed"
    assert storage.get_confirmation(new_id)["metric"] == "order_count"
    # 改选非法指标 → 400
    assert client.post(f"/api/confirmations/{new_id}/override?metric=bogus",
                       headers=_ANALYST).status_code == 400

    # 驳回需要 admin
    cid2 = _confirm.persist(spec)
    assert client.post(f"/api/confirmations/{cid2}/dismiss",
                       headers=_ANALYST).status_code == 403
    assert client.post(f"/api/confirmations/{cid2}/dismiss",
                       headers=_ADMIN).status_code == 200
    assert storage.get_confirmation(cid2)["status"] == "dismissed"
