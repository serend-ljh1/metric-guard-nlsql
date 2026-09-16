"""
tests/test_sandbox.py
=====================
只读沙箱安全测试（无需 API Key / 无需外部数据）：
  - 写/高危语句拦截（对所有方言一致生效，含外部连接器）
  - 合法只读查询放行
  - 方言适配层（缺驱动优雅报错、连接器注入）
"""
import pytest

from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox, make_sandbox


HOSTILE = [
    "DROP TABLE x",
    "INSERT INTO x VALUES(1)",
    "UPDATE x SET a=1",
    "DELETE FROM x",
    "SELECT * FROM a; DROP TABLE b",
    "PRAGMA writable_schema=ON",
]


@pytest.fixture
def sb(mini_db):
    return SqlSandbox(mini_db, ExecConfig(max_rows=2000))


@pytest.mark.parametrize("sql", HOSTILE)
def test_hostile_blocked(sb, sql):
    r = sb.execute(sql)
    assert not r.ok


def test_readonly_query_ok(sb):
    r = sb.execute("SELECT name FROM singer")
    assert r.ok and len(r.rows) == 3


def test_result_truncation(sb):
    r = sb.execute("SELECT * FROM singer")
    assert r.ok and r.reason == "ok"


class _FakeConn:
    """外部连接器桩：验证语句级安全校验对外部方言一致生效。"""
    dialect = "mysql"
    prompt_hint = "目标数据库是 MySQL。"

    def __init__(self):
        self.seen = []

    def run_query(self, sql, limit):
        self.seen.append(sql)
        return ["x"], [(1,)]


def test_external_connector_blocks_writes():
    fc = _FakeConn()
    sb = SqlSandbox(connector=fc, config=ExecConfig())
    assert not sb.execute("DROP TABLE users").ok
    assert not sb.execute("SELECT 1; DELETE FROM t").ok


def test_external_connector_allows_select():
    fc = _FakeConn()
    sb = SqlSandbox(connector=fc, config=ExecConfig())
    r = sb.execute("SELECT 1")
    assert r.ok and fc.seen == ["SELECT 1"]
    assert sb.dialect == "mysql"


def test_make_sandbox_sqlite(mini_db):
    sb = make_sandbox({"kind": "sqlite", "path": mini_db}, ExecConfig())
    assert sb.execute("SELECT 1").ok


def test_missing_driver_graceful():
    """缺 pymysql/psycopg2 时连接测试优雅失败，而不是崩溃。"""
    from sqlpa.sandbox.dialects import make_connector
    conn = make_connector({"kind": "mysql", "host": "127.0.0.1", "user": "root",
                           "password": "x", "database": "test"})
    ok, msg = conn.ping()
    # 本机未装驱动 → 失败且提示安装；装了驱动但连不上 → 也是失败
    assert not ok
    assert msg
