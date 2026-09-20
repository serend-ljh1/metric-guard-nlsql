"""安全加固回归 —— 把"审计不可匿名读、不可静默丢、不可删"钉进测试。

背景（评审实测确认的缺口，本文件逐条守）：
  1. `GET /api/audit` 完全无鉴权 → 匿名可拉走 user_input / generated_sql；
  2. `audit_logs` 可被应用账号 DELETE（留痕只是君子协定）；
  3. `append_audit` 用 `except Exception: pass` 吞掉写失败 → 审计能悄悄丢；
  4. `/api/analyze*` 解析了角色却不做权限校验、也不写审计；
  5. `check_access` / `mask_result` 遇到 `SELECT *` fail-open（明文返回敏感列）。
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sqlpa.business import storage
from sqlpa.business.audit import AuditRecord, append_audit, audit_write_failures
from sqlpa.business.metric_config import load_config
from sqlpa.business.permissions import check_access, mask_result

CFG = load_config()


# ---------------------------------------------------------------- 审计端点鉴权

@pytest.fixture
def client_with_tokens(monkeypatch):
    import api
    monkeypatch.setenv("SQLPA_API_TOKENS", "tok-admin:admin,tok-analyst:analyst")
    return TestClient(api.app)


@pytest.fixture
def client_no_tokens(monkeypatch):
    import api
    monkeypatch.setenv("SQLPA_API_TOKENS", "")
    monkeypatch.setenv("SQLPA_DEFAULT_ROLE", "analyst")
    return TestClient(api.app)


def test_audit_requires_token_when_configured(client_with_tokens):
    assert client_with_tokens.get("/api/audit").status_code == 401
    assert client_with_tokens.get("/api/datasources").status_code == 401
    ok = client_with_tokens.get("/api/audit", headers={"X-API-Token": "tok-analyst"})
    assert ok.status_code == 200


def test_audit_redacted_for_non_admin(monkeypatch, tmpdir_clean):
    """非 admin 看得到"发生过什么"，看不到提问原文与 SQL。"""
    import api
    log = tmpdir_clean / "audit.jsonl"
    log.write_text(json.dumps({
        "query_id": "q1", "username": "alice", "user_role": "analyst",
        "user_input": "某客户手机号 11987654321 相关订单", "generated_sql": "SELECT 1",
        "matched_metric": "gmv", "is_success": True, "mode": "metric",
        "certified": True, "create_time": "2026-01-01 00:00:00",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(api, "_audit_log_path", lambda: log, raising=False)
    # 直接测脱敏函数（端点读的是固定路径，这里避免污染真实审计文件）
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    out = api._redact_audit(rec)
    assert out["matched_metric"] == "gmv"
    assert "11987654321" not in json.dumps(out, ensure_ascii=False)
    assert out["user_input"] == "（已脱敏）"


# ---------------------------------------------------------------- 审计不可删/不可静默丢

def test_audit_table_is_append_only(monkeypatch, tmpdir_clean):
    db = tmpdir_clean / "app.db"
    monkeypatch.setattr(storage, "_DB_PATH", db)
    conn = storage._conn()
    conn.execute("INSERT INTO audit_logs(query_id, username, created_at) VALUES(?,?,?)",
                 ("q1", "alice", "2026-01-01"))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_logs")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_logs SET username='bob'")
    assert conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 1
    conn.close()


def test_audit_write_failure_is_visible(monkeypatch, tmpdir_clean):
    """SQLite 写失败必须留下痕迹，而不是被 except 吞掉。"""
    log = tmpdir_clean / "a.jsonl"
    before = len(audit_write_failures())

    def boom(_payload):
        raise RuntimeError("db is locked")

    monkeypatch.setattr(storage, "insert_audit", boom)
    ok = append_audit(AuditRecord(query_id="q-fail", username="u", user_input="x"), path=log)
    assert ok is False, "写失败必须返回 False"
    fails = audit_write_failures()
    assert len(fails) == before + 1 and fails[-1]["query_id"] == "q-fail"
    # JSONL 仍然写出（单 sink 失败不等于整条丢失）
    assert log.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------- SELECT * fail-closed

def test_wildcard_projection_is_denied_for_restricted_role():
    perms = CFG.permissions
    schema = {"tables": [{"name": "customers",
                          "columns": [{"name": c} for c in
                                      ("customer_id", "customer_phone",
                                       "customer_zip_code_prefix", "customer_state")]}]}
    assert check_access("analyst", perms, "SELECT * FROM customers c", schema=schema), \
        "SELECT * 必须被拒（无法逐列核对权限）"
    assert check_access("analyst", perms, "SELECT c.* FROM customers c", schema=schema)
    # 显式列且在白名单内 → 放行；显式敏感列 → 拦截
    assert not check_access("analyst", perms,
                            "SELECT c.customer_state FROM customers c", schema=schema)
    assert check_access("analyst", perms,
                        "SELECT c.customer_phone FROM customers c", schema=schema)


def test_wildcard_masking_is_fail_closed():
    """通配符结果集不能明文返回敏感列。"""
    perms = CFG.permissions
    schema = {"tables": [{"name": "customers",
                          "columns": [{"name": c} for c in
                                      ("customer_id", "customer_phone",
                                       "customer_zip_code_prefix", "customer_state")]}]}
    rows = [("c1", "11987654321", "01151", "SP")]
    out = mask_result(["customer_id", "customer_phone",
                       "customer_zip_code_prefix", "customer_state"],
                      rows, perms.get("sensitive_columns"), sql="SELECT * FROM customers c",
                      perms=perms, schema=schema)
    assert out[0][0] == "c1"
    assert "11987654321" not in str(out), f"敏感列被明文返回: {out}"
    assert "01151" not in str(out)


# ---------------------------------------------------------------- 分析链的权限与审计

def test_analysis_path_writes_audit(monkeypatch, tmpdir_clean, sample_db):
    """分析链同样要留痕（此前 role 是死参数、审计一条不写）。"""
    from sqlpa.analysis.orchestrator import run_analysis
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

    db = tmpdir_clean / "app.db"
    monkeypatch.setattr(storage, "_DB_PATH", db)
    cfg = load_config()
    sb = SqlSandbox(sample_db, ExecConfig(max_rows=2000))
    out = run_analysis("2018-06 的 GMV 是多少", cfg, sb, sample_db, None,
                       role="analyst", username="tester", dims_override=[])
    assert out.get("ok") is True, out.get("reject")
    conn = storage._conn()
    n = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE username='tester'").fetchone()[0]
    conn.close()
    assert n >= 1, "分析路径未写审计"


def test_analysis_path_enforces_permissions(monkeypatch, tmpdir_clean, sample_db):
    """分析链不能成为绕过表列权限的第二入口。"""
    from dataclasses import replace

    from sqlpa.analysis.orchestrator import run_analysis
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

    monkeypatch.setattr(storage, "_DB_PATH", tmpdir_clean / "app2.db")
    cfg = load_config()
    # 收紧 analyst：customers 只允许 customer_id（于是 c.customer_state 越权）
    perms = dict(cfg.permissions)
    perms["roles"] = dict(perms["roles"])
    perms["roles"]["analyst"] = dict(perms["roles"]["analyst"])
    perms["roles"]["analyst"]["allowed_columns"] = {"customers": ["customer_id"]}
    cfg2 = replace(cfg, permissions=perms)

    sb = SqlSandbox(sample_db, ExecConfig(max_rows=2000))
    out = run_analysis("各州的 GMV", cfg2, sb, sample_db, None,
                       role="analyst", username="tester", dims_override=["state"])
    assert out.get("ok") is False
    assert "权限" in str(out.get("reject", "")), out.get("reject")


# ---------------------------------------------------------------- SSE 失败不再假超时

def test_stream_emits_error_frame_instead_of_hanging(monkeypatch):
    """节点异常必须变成 error 终帧，而不是让客户端干等 300 秒后收到"分析超时"。"""
    import api
    from sqlpa.analysis import orchestrator

    def boom(*_a, **_k):
        raise RuntimeError("节点炸了")

    # 隔离对真实数据的依赖：本用例只验流式层的异常兜底
    monkeypatch.setattr(orchestrator, "run_analysis", boom)
    monkeypatch.setattr(api, "_cfg", lambda: None)
    monkeypatch.setattr(api, "_sandbox", lambda: None)
    monkeypatch.setattr(api, "_db_path", lambda: "unused.db")
    client = TestClient(api.app)
    with client.stream("POST", "/api/analyze/stream", json={"question": "x"}) as resp:
        assert resp.status_code == 200
        raw = "".join(resp.iter_text())
    types = [json.loads(ln[6:])["type"] for ln in raw.splitlines() if ln.startswith("data: ")]
    assert "error" in types and "done" in types
    assert "节点炸了" in raw
    assert "分析超时" not in raw


# ---------------------------------------------------------------- 会话记忆归属与上限

def test_session_memory_is_scoped_per_caller():
    import api
    a = api._session_memory("s1", "callerA")
    a["last_drill"] = {"metric": "gmv"}
    b = api._session_memory("s1", "callerB")
    assert b == {}, "不同调用者用同一 session_id 不能读到别人的记忆"
    assert api._session_memory("s1", "callerA")["last_drill"]["metric"] == "gmv"
    assert api._session_memory(None, "callerA") == {}


def test_session_memory_is_bounded(monkeypatch):
    import api
    monkeypatch.setattr(api, "_SESSION_MAX", 5)
    api._SESSION_MEMORY.clear()
    api._SESSION_SEEN.clear()
    for i in range(20):
        api._session_memory(f"s{i}", "callerA")
    assert len(api._SESSION_MEMORY) <= 5, "会话记忆必须有过期/容量上限"


# ---------------------------------------------------------------- 外部连接器护栏

def test_mysql_connector_forces_readonly_txn_and_timeout(monkeypatch):
    """MySQL 只读不能依赖驱动默认 autocommit，且必须有查询级超时。

    用假驱动断言**语句序列**：关 autocommit → 会话只读 → 显式只读事务 → 执行超时。
    """
    import sys
    import types

    executed = []

    class FakeCursor:
        def execute(self, stmt, *a):
            executed.append(stmt if isinstance(stmt, str) else str(stmt))

    class FakeConn:
        def __init__(self):
            self.autocommit_calls = []
        def cursor(self):
            return FakeCursor()
        def autocommit(self, v):
            self.autocommit_calls.append(v)
        def close(self):
            pass

    fake_conn = FakeConn()
    fake_mod = types.SimpleNamespace(connect=lambda **kw: fake_conn)
    monkeypatch.setitem(sys.modules, "pymysql", fake_mod)

    from sqlpa.sandbox.dialects import MySQLConnector
    c = MySQLConnector(host="h", user="u", password="p", database="d", timeout=7.0)
    c._open()

    assert fake_conn.autocommit_calls == [False], "必须显式关闭 autocommit"
    joined = " | ".join(executed)
    assert "SET SESSION TRANSACTION READ ONLY" in joined
    assert "START TRANSACTION READ ONLY" in joined, "必须显式开启只读事务"
    assert any("MAX_EXECUTION_TIME" in s or "max_statement_time" in s for s in executed), \
        "必须设置查询级超时（read_timeout 只管网络读）"


def test_postgres_connector_sets_statement_timeout(monkeypatch):
    """PostgreSQL 连接参数必须带 statement_timeout 与服务端只读。"""
    import sys
    import types

    captured = {}

    class FakeConn:
        def set_session(self, **kw):
            captured["set_session"] = kw
        def cursor(self):
            return types.SimpleNamespace(execute=lambda *a, **k: None)
        def close(self):
            pass

    def fake_connect(**kw):
        captured["connect"] = kw
        return FakeConn()

    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=fake_connect))
    from sqlpa.sandbox.dialects import PostgresConnector
    PostgresConnector(host="h", user="u", password="p", database="d", timeout=7.0)._open()

    opts = captured["connect"].get("options", "")
    assert "statement_timeout=7000" in opts, opts
    assert "default_transaction_read_only=on" in opts, opts
    assert captured["set_session"].get("readonly") is True
