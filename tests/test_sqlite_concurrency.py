"""SQLite 多 worker 就绪回归：WAL 日志模式 + busy_timeout。

背景：`storage._conn()` 此前用默认 rollback journal + 无 busy_timeout —— 一个连接在写
就锁住整库，并发写**立刻**抛 `database is locked`。这意味着 `--workers > 1`（或"多进程
共享同一 data/app.db"）一并发就出 500。这里回归：日志模式为 WAL、busy_timeout 生效、
多线程并发写不报错。
"""
from __future__ import annotations

import threading

from sqlpa.business import storage


def _pragma(conn, name: str):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_journal_mode_is_wal(tmpdir_clean):
    with storage._conn() as c:
        assert str(_pragma(c, "journal_mode")).lower() == "wal"


def test_busy_timeout_is_configured(tmpdir_clean):
    with storage._conn() as c:
        assert int(_pragma(c, "busy_timeout")) == storage._busy_timeout_ms() > 0


def test_env_overrides_are_respected(tmpdir_clean, monkeypatch):
    monkeypatch.setenv("SQLPA_SQLITE_JOURNAL_MODE", "DELETE")
    monkeypatch.setenv("SQLPA_SQLITE_BUSY_TIMEOUT_MS", "1234")
    with storage._conn() as c:
        assert str(_pragma(c, "journal_mode")).lower() == "delete"
        assert int(_pragma(c, "busy_timeout")) == 1234


def test_concurrent_writes_do_not_raise_locked(tmpdir_clean):
    """8 线程各写 15 条审计：WAL + busy_timeout 下不应出现 'database is locked'。"""
    errors: list = []

    def worker(tid: int):
        try:
            for i in range(15):
                storage.insert_audit({"username": f"u{tid}", "user_input": f"q{i}"})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"并发写出现异常：{errors}"
    assert storage.count_audit() == 8 * 15
