"""P1-2：存储后端可配置（仅 SQLite 实测）+ 每进程一次建表 + 部署事实可查。

要点：
  - 只实现 SQLite；配置成未实现的后端必须**立即报错**（fail fast），不静默降级 ——
    静默降级会把"部署配错了"掩盖成"看起来能跑"；
  - `_conn()` 的建表 + 迁移由"每条连接一次"改为"每进程每库一次"：一个请求会开多条
    连接（会话/限速/审计/记忆各一），重复建表既是纯浪费、也在并下发反复抢写锁；
  - `/api/storage-info` 仅 admin，如实回报 backend/journal/busy_timeout，不虚构 Postgres。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sqlpa.business import storage


def test_resolve_backend_defaults_to_sqlite(monkeypatch):
    monkeypatch.delenv("SQLPA_STORAGE_BACKEND", raising=False)
    assert storage.resolve_backend() == "sqlite"
    assert storage.resolve_backend("") == "sqlite"
    assert storage.resolve_backend("SQLite") == "sqlite"   # 大小写不敏感


def test_unsupported_backend_fails_fast(monkeypatch):
    with pytest.raises(ValueError):
        storage.resolve_backend("postgres")
    monkeypatch.setenv("SQLPA_STORAGE_BACKEND", "postgres")
    with pytest.raises(ValueError):
        storage._conn()          # 构建连接时就失败，不会静默按 SQLite 跑


def test_backend_info_reports_sqlite_deployment(tmpdir_clean):
    info = storage.backend_info()
    assert info["backend"] == "sqlite"
    assert info["supported_backends"] == ["sqlite"]
    assert info["journal_mode"] == "WAL" and info["wal"] is True
    assert info["busy_timeout_ms"] > 0
    assert "SQLite" in info["multiworker_note"]


def test_schema_initialized_once_per_db_path(tmpdir_clean, monkeypatch):
    """建表/迁移只在**该库路径**首次连接时执行一次（并发下不再每请求抢写锁）。"""
    monkeypatch.setattr(storage, "_DB_PATH", tmpdir_clean / "once.db")
    calls = {"n": 0}
    real = storage._migrate

    def counting(conn):
        calls["n"] += 1
        return real(conn)

    monkeypatch.setattr(storage, "_migrate", counting)
    for _ in range(3):
        storage._conn().close()
    assert calls["n"] == 1


def _client():
    import api
    return TestClient(api.app)


def test_storage_info_endpoint_requires_admin(monkeypatch):
    import api  # noqa: F401  先导入，确保 app 已组装
    monkeypatch.setenv("SQLPA_API_TOKENS", "admintok:admin,usertok:analyst")
    c = _client()
    assert c.get("/api/storage-info", headers={"X-API-Token": "usertok"}).status_code == 403
    r = c.get("/api/storage-info", headers={"X-API-Token": "admintok"})
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "sqlite" and "journal_mode" in body
    assert c.get("/api/storage-info").status_code == 401
