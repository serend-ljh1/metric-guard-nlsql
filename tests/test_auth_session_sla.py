"""登录会话 / 会话记忆与限速持久化 / HITL SLA 的回归测试。

这三处此前的共同形态是"演示能跑、运营会塌"：
  - 身份只有静态 `SQLPA_API_TOKENS`，审计记不出"这是谁问的"，也登不出来；
  - 会话记忆与限速是**纯进程内**状态，重启即丢、多 worker 各自分裂；
  - 工单进队列后没人推动（没有 SLA、没有超时升级），"闭环"只剩文档里的词。
"""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sqlpa.business import hitl, storage


@pytest.fixture
def client(monkeypatch):
    """无静态 token 的客户端（本地/演示默认：未配置凭据时按最小权限放行）。"""
    import api
    monkeypatch.delenv("SQLPA_API_TOKENS", raising=False)
    return TestClient(api.app)


@pytest.fixture
def admin_client(monkeypatch):
    """配置了静态 token 的客户端（登录/静态凭据两条路径都要能走通）。"""
    import api
    monkeypatch.setenv("SQLPA_API_TOKENS", "tok-admin:admin,tok-analyst:analyst")
    return TestClient(api.app)


_ADMIN = {"X-API-Token": "tok-admin"}
_ANALYST = {"X-API-Token": "tok-analyst"}


# ---------------------------------------------------------------- 登录 / 身份

def test_login_issues_session_then_me_and_logout_revokes(client):
    """登录签发会话 token → /api/auth/me 能说出"我是谁" → 登出后 token 立即失效。"""
    assert storage.create_user("alice", "s3cret", role="admin")
    r = client.post("/api/auth/login", json={"username": "alice", "password": "s3cret"})
    assert r.status_code == 200
    j = r.json()
    tok = j["token"]
    assert j["username"] == "alice" and j["role"] == "admin"

    me = client.get("/api/auth/me", headers={"X-API-Token": tok})
    assert me.status_code == 200
    assert me.json() == {"username": "alice", "role": "admin", "source": "session"}

    # 错误口令不签发会话，且留一条失败审计
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "wrong"}).status_code == 401
    assert any((x.get("reject_reason") or "") == "auth login_failed"
               for x in storage.list_audit(limit=20))

    # 登出即撤销：token 既不是会话，也不落入静态映射（无 token 配置 → 回落默认身份）
    assert client.post("/api/auth/logout", headers={"X-API-Token": tok}).json()["logged_out"] is True
    assert storage.get_session(tok) is None
    assert client.get("/api/auth/me", headers={"X-API-Token": tok}).json()["source"] == "default"


def test_session_token_survives_until_expiry_and_token_is_opaque():
    """会话带到期时间；token 是随机串（不可由用户名推导）。"""
    storage.create_user("bob", "pw", role="analyst")
    s = storage.create_session("bob", "analyst", ttl_seconds=3600)
    assert len(s["token"]) >= 32
    got = storage.get_session(s["token"])
    assert got and got["username"] == "bob" and got["role"] == "analyst"
    # 过期会话不再有效
    expired = storage.create_session("bob", "analyst", ttl_seconds=-1)
    assert storage.get_session(expired["token"]) is None
    assert storage.delete_session("no-such-token") is False


def test_static_token_still_works_and_is_named_by_fingerprint(admin_client):
    """静态 token 部署方式保持可用：角色取自 token，身份记为凭据指纹（不再是匿名）。"""
    r = admin_client.get("/api/auth/me", headers=_ADMIN)
    assert r.status_code == 200
    j = r.json()
    assert j["role"] == "admin" and j["source"] == "static"
    assert j["username"].startswith("token:")
    # 无凭据且配了 token → 401（不能匿名读治理端点）
    assert admin_client.get("/api/auth/me").status_code == 401


def test_audit_receives_logged_in_actor_and_role_is_not_from_body(monkeypatch, tmpdir_clean):
    """审计要能回答"这是谁问的"：登录用户发起取数 → 取数层收到真实 username。

    同时回归**越权**：会话角色是 analyst 时，请求体里自称 admin 不能提权。
    """
    import api
    from apiroutes import query as query_router
    captured: dict = {}

    def fake_answer(question, cfg, sandbox, db_path, llm, role="analyst",
                    username="", **kw):
        captured.update(question=question, role=role, username=username)
        return {"ok": True, "rows": [], "metric": "gmv"}

    monkeypatch.delenv("SQLPA_API_TOKENS", raising=False)
    monkeypatch.setattr(query_router, "answer_impl", fake_answer)
    monkeypatch.setattr(query_router, "_cfg", lambda: None)
    monkeypatch.setattr(query_router, "_sandbox", lambda: None)
    monkeypatch.setattr(query_router, "_db_path", lambda: "unused.db")

    storage.create_user("alice", "s3cret", role="analyst")
    c = TestClient(api.app)
    tok = c.post("/api/auth/login",
                 json={"username": "alice", "password": "s3cret"}).json()["token"]
    r = c.post("/api/query", json={"question": "2018-06 GMV 是多少", "role": "admin"},
               headers={"X-API-Token": tok})
    assert r.status_code == 200
    assert captured["username"] == "alice", "审计归属人必须是登录用户，而不是空串/角色"
    assert captured["role"] == "analyst", "请求体自称 admin 不得提权"
    assert r.json()["effective_role"] == "analyst"


# ---------------------------------------------------------------- 引导账号

def test_bootstrap_creates_admin_once(monkeypatch, tmpdir_clean):
    """设了引导口令 → 启动时**真的**建号（此前 ensure_default_users 只有测试会调）。"""
    import api
    monkeypatch.setenv("SQLPA_BOOTSTRAP_ADMIN_USER", "root")
    monkeypatch.setenv("SQLPA_BOOTSTRAP_ADMIN_PASSWORD", "p@ss")
    api._bootstrap_accounts()
    assert storage.verify_user("root", "p@ss") == "admin"
    assert storage.verify_user("root", "wrong") is None
    before = len(storage.list_users())
    api._bootstrap_accounts()          # 幂等：账号已存在则不重复创建
    assert len(storage.list_users()) == before


def test_bootstrap_is_noop_without_password(monkeypatch, tmpdir_clean):
    """没设引导口令 → 不预置任何账号（避免"默认口令"这类安全问题）。"""
    import api
    monkeypatch.delenv("SQLPA_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    api._bootstrap_accounts()
    assert storage.list_users() == []


# ---------------------------------------------------------------- 持久化（跨重启）

def test_session_memory_survives_cache_clear(tmpdir_clean):
    """会话记忆落库：清空进程内缓存（≈重启/换 worker）后仍读得回上一轮下钻上下文。"""
    import api
    with api._session_guard("s-1", "user:alice") as mem:
        mem["last_drill"] = {"metric": "gmv", "dim": "state"}
    api._SESSION_MEMORY.clear()
    api._SESSION_SEEN.clear()
    assert api._session_memory("s-1", "user:alice").get("last_drill") == \
        {"metric": "gmv", "dim": "state"}
    # 按调用者隔离依然成立：别人拿同一个 session_id 读不到
    assert api._session_memory("s-1", "user:bob") == {}


def test_rate_limit_window_survives_cache_clear(monkeypatch):
    """限速计数落库：清空进程内缓存后**继续计数**，不会清零放行。"""
    from apiroutes import deps as d
    monkeypatch.setattr(d, "_RATE_MAX", 3)
    monkeypatch.setattr(d, "_RATE_WINDOW", 60.0)
    d._RATE_HITS.clear()
    for _ in range(3):
        d._rate_limit("user:alice", "analyze")
    d._RATE_HITS.clear()          # 模拟进程重启 / 换 worker
    with pytest.raises(HTTPException) as ei:
        d._rate_limit("user:alice", "analyze")
    assert ei.value.status_code == 429


# ---------------------------------------------------------------- HITL SLA

def test_hitl_ticket_carries_sla_deadline(tmpdir_clean):
    """登记即带截止时间：没有 due_at，"超时升级"就无从判定。"""
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03")
    rec = hitl.get(rid)
    assert rec["due_at"] and float(rec["due_at"]) > time.time()
    assert not rec["escalated"] and not rec["reminded_at"]


def test_sweep_escalates_overdue_but_stays_idempotent(tmpdir_clean):
    """超期 → 升级并写流转历史；重复巡检不重复升级。"""
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03", owner="alice")
    due = float(hitl.get(rid)["due_at"])
    out = hitl.sweep(now_ts=due + 3600)
    assert out["escalated_count"] == 1 and out["reminded_count"] == 0
    assert out["escalated"][0]["record_id"] == rid
    rec = hitl.get(rid)
    assert int(rec["escalated"]) == 1 and rec["escalated_at"]
    assert any("超时升级" in (h.get("note") or "") for h in hitl.history(rid))
    # 幂等：再巡检一次不应重复升级
    assert hitl.sweep(now_ts=due + 7200)["escalated_count"] == 0
    assert [r["record_id"] for r in storage.list_hitl_escalated()] == [rid]


def test_sweep_reminds_soon_due_but_only_once(tmpdir_clean):
    """临期（距截止不足 1 小时）→ 催办，只催一次。"""
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03")
    due = float(hitl.get(rid)["due_at"])
    out = hitl.sweep(now_ts=due - 60)
    assert out["reminded_count"] == 1 and out["escalated_count"] == 0
    assert hitl.get(rid)["reminded_at"]
    assert hitl.sweep(now_ts=due - 30)["reminded_count"] == 0


def test_escalation_skips_closed_tickets(tmpdir_clean):
    """已闭环（verified/dismissed）的工单不该被升级 —— 它已经不是"待办"。"""
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03")
    hitl.resolve(rid, "dismissed", human_note="误报", actor="alice")
    due = float(hitl.get(rid)["due_at"])
    assert hitl.sweep(now_ts=due + 3600)["escalated_count"] == 0
    assert storage.list_hitl_escalated() == []


def test_sla_endpoints_require_auth_and_admin(admin_client):
    """SLA 巡检是 admin 动作；超期列表对 analyst 可读（看板要能展示）。"""
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03")
    assert admin_client.post("/api/hitl/sweep").status_code == 401
    assert admin_client.post("/api/hitl/sweep", headers=_ANALYST).status_code == 403
    ok = admin_client.post("/api/hitl/sweep", headers=_ADMIN)
    assert ok.status_code == 200 and "sla_hours" in ok.json()

    esc = admin_client.get("/api/hitl/escalations", headers=_ANALYST)
    assert esc.status_code == 200
    body = esc.json()
    assert set(body) == {"sla_hours", "escalated", "soon_due"}

    # 超期工单确实出现在升级列表中
    future = float(hitl.get(rid)["due_at"]) + 3600
    hitl.sweep(now_ts=future)
    listed = admin_client.get("/api/hitl/escalations", headers=_ANALYST).json()
    assert rid in [r["record_id"] for r in listed["escalated"]]
