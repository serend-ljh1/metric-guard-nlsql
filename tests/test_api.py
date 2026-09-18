"""
tests/test_api.py
=================
FastAPI 薄后端接口测试（离线、无需 API Key）：
  - /health
  - /api/query（口径内认证 / 口径外降级）
  - /api/metrics / /api/audit / /api/datasources
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(monkeypatch_module):
    import api   # 先导入（其 load_dotenv 可能从 .env 载入 Key）
    # 再强制离线：清掉 LLM Key，避免测试打到真实 API（保证确定性 + 零成本）
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch_module.delenv(k, raising=False)
    return TestClient(api.app)


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_query_in_scope(client):
    r = client.post("/api/query", json={"question": "各个品类的GMV", "role": "analyst"})
    assert r.status_code == 200
    d = r.json()
    assert d["matched"] and d["mode"] == "metric" and d["certified"]


def test_query_out_of_scope_offline(client):
    # 无 Key 时口径外问题被明确拒绝（离线不支持自由查询）
    r = client.post("/api/query", json={"question": "每个客服的响应时长是多少"})
    assert r.status_code == 200
    d = r.json()
    assert not d["ok"] and d["mode"] == "free"


def test_metrics_endpoint(client):
    r = client.get("/api/metrics")
    assert r.status_code == 200
    d = r.json()
    assert d["metrics"] and d["dimensions"]


def test_audit_endpoint(client):
    r = client.get("/api/audit?limit=5")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_datasources_endpoint(client):
    r = client.get("/api/datasources")
    assert r.status_code == 200
    assert any(d["id"] == "builtin_sqlite" for d in r.json())
