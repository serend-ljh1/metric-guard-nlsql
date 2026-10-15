"""个人工作台端点回归：查询历史 / 收藏 / 反馈。

背景：`storage` 早有 audit_logs / favorites / feedback 三张表与读写函数，却**没有
任何端点**把它们暴露出来 —— "看历史 / 存收藏 / 提反馈"在系统里根本做不到。
这里回归新接上的 /api/history、/api/favorites、/api/feedback，并重点验证
**按调用者隔离**（别人的收藏删不掉）与**权限收敛**（反馈非 admin 只看自己的）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sqlpa.business import storage


@pytest.fixture
def client(monkeypatch):
    """无静态 token 的客户端：本地/演示默认身份为 anonymous。"""
    import api
    monkeypatch.delenv("SQLPA_API_TOKENS", raising=False)
    return TestClient(api.app)


def test_history_lists_only_current_caller(client):
    storage.insert_audit({"username": "anonymous", "user_input": "GMV 是多少",
                          "matched_metric": "gmv", "is_success": 1})
    storage.insert_audit({"username": "someone-else", "user_input": "别人的问题",
                          "matched_metric": "gmv"})
    r = client.get("/api/history")
    assert r.status_code == 200
    j = r.json()
    assert j["username"] == "anonymous" and j["total"] == 1
    assert [it["user_input"] for it in j["items"]] == ["GMV 是多少"]


def test_history_pagination_is_stable_and_disjoint(client):
    for i in range(5):
        storage.insert_audit({"username": "anonymous", "user_input": f"q{i}"})
    p1 = client.get("/api/history?limit=2&offset=0").json()
    p2 = client.get("/api/history?limit=2&offset=2").json()
    assert p1["total"] == 5 and len(p1["items"]) == 2 and len(p2["items"]) == 2
    a = {x["user_input"] for x in p1["items"]}
    b = {x["user_input"] for x in p2["items"]}
    assert a.isdisjoint(b)


def test_favorites_crud_and_user_isolation(client):
    assert client.post("/api/favorites", json={"title": "   "}).status_code == 400

    assert client.post("/api/favorites",
                       json={"title": "月度 GMV", "user_input": "GMV 多少"}).status_code == 200
    lst = client.get("/api/favorites").json()
    assert len(lst) == 1 and lst[0]["title"] == "月度 GMV"
    mine = lst[0]["id"]

    # 别人的收藏：既读不到、也删不掉
    storage.add_favorite("bob", "bob 的收藏", "", "")
    theirs = storage.list_favorites("bob")[0]["id"]
    assert client.delete(f"/api/favorites/{theirs}").status_code == 404
    assert storage.delete_favorite(theirs, "bob") is True  # 确实存在，只是不归我

    assert client.delete(f"/api/favorites/{mine}").status_code == 200
    assert client.get("/api/favorites").json() == []


def test_feedback_validates_then_scopes_by_role(client):
    assert client.post("/api/feedback", json={"rating": 0}).status_code == 400
    assert client.post("/api/feedback",
                       json={"rating": 1, "user_input": "赞"}).status_code == 200
    storage.insert_feedback("q2", "someone-else", "别人的问题", "", -1, "别人的差评")
    assert client.post("/api/feedback",
                       json={"rating": -1, "user_input": "答非所问",
                             "comment": "口径不对"}).status_code == 200

    all_fb = client.get("/api/feedback").json()
    # 非 admin 只看自己的：别人那条不可见
    assert all_fb["count"] == 2
    assert all(it["username"] == "anonymous" for it in all_fb["items"])

    bad = client.get("/api/feedback?rating=-1").json()
    assert bad["count"] == 1 and bad["items"][0]["comment"] == "口径不对"


def test_history_scoped_to_logged_in_user(monkeypatch, tmpdir_clean):
    """登录用户的历史按真实用户名隔离：A 看不到 B 问过什么。"""
    import api
    monkeypatch.delenv("SQLPA_API_TOKENS", raising=False)
    c = TestClient(api.app)
    storage.create_user("alice", "pw", role="analyst")
    storage.create_user("bob", "pw", role="analyst")
    storage.insert_audit({"username": "alice", "user_input": "alice 的问题"})
    storage.insert_audit({"username": "bob", "user_input": "bob 的问题"})

    tok = c.post("/api/auth/login", json={"username": "alice", "password": "pw"}).json()["token"]
    j = c.get("/api/history", headers={"X-API-Token": tok}).json()
    assert j["username"] == "alice"
    texts = [it["user_input"] for it in j["items"]]
    assert "alice 的问题" in texts and "bob 的问题" not in texts
