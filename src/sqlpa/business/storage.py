"""
sqlpa.business.storage
======================
统一持久化层：SQLite 单文件 (data/app.db)，承载用户、审计、HITL、反馈、收藏。

替代原先 audit.jsonl / hitl.jsonl 的文件存储，支持按条件查询、分页、统计，
为"查询历史""反馈闭环""真实登录"提供数据底座。
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "app.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    password   TEXT NOT NULL,          -- 简单加盐哈希(非生产级，原型用)
    role       TEXT NOT NULL DEFAULT 'analyst',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id      TEXT,
    username      TEXT,
    user_role     TEXT,
    user_input    TEXT,
    matched_metric TEXT,
    generated_sql TEXT,
    is_success    INTEGER,
    execute_cost_ms REAL,
    result_rows   INTEGER,
    reject_reason TEXT,
    mode          TEXT,                -- metric / free
    certified     INTEGER,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(username);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(created_at);

CREATE TABLE IF NOT EXISTS hitl_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id     TEXT UNIQUE,
    username      TEXT,
    user_input    TEXT,
    matched_metric TEXT,
    generated_sql TEXT,
    reject_reason TEXT,
    status        TEXT DEFAULT 'pending',  -- pending / approved / rejected
    decided_by    TEXT,
    decided_at    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id    TEXT,
    username    TEXT,
    user_input  TEXT,
    generated_sql TEXT,
    rating      INTEGER,              -- 1=点赞 / -1=点踩
    comment     TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS favorites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT,
    title       TEXT,
    user_input  TEXT,
    generated_sql TEXT,
    created_at  TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ================= 用户 =================
def _hash_pwd(pwd: str, salt: str = "sqlpa_salt_2026") -> str:
    """简单加盐哈希（原型用，非生产安全）。"""
    import hashlib
    return hashlib.sha256((salt + pwd).encode()).hexdigest()


def create_user(username: str, password: str, role: str = "analyst") -> bool:
    try:
        with _conn() as c:
            c.execute("INSERT INTO users(username,password,role,created_at) VALUES(?,?,?,?)",
                      (username, _hash_pwd(password), role, _now()))
        return True
    except sqlite3.IntegrityError:
        return False


def verify_user(username: str, password: str) -> Optional[str]:
    """返回角色名或 None。"""
    with _conn() as c:
        row = c.execute("SELECT role,password FROM users WHERE username=?", (username,)).fetchone()
    if row and row["password"] == _hash_pwd(password):
        return row["role"]
    return None


def list_users() -> list:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT username,role,created_at FROM users").fetchall()]


def ensure_default_users() -> None:
    """首次启动创建两个演示账号。"""
    create_user("admin", "admin123", role="admin")
    create_user("analyst", "analyst123", role="analyst")


# ================= 审计 =================
def insert_audit(rec: dict) -> None:
    rec.setdefault("created_at", _now())
    rec.setdefault("query_id", uuid.uuid4().hex[:12])
    with _conn() as c:
        c.execute("""INSERT INTO audit_logs
            (query_id,username,user_role,user_input,matched_metric,generated_sql,
             is_success,execute_cost_ms,result_rows,reject_reason,mode,certified,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.get("query_id"), rec.get("username", ""), rec.get("user_role", ""),
             rec.get("user_input", ""), rec.get("matched_metric", ""),
             rec.get("generated_sql", ""), int(rec.get("is_success", 0)),
             rec.get("execute_cost_ms", 0.0), rec.get("result_rows", 0),
             rec.get("reject_reason", ""), rec.get("mode", "metric"),
             int(rec.get("certified", 0)), rec["created_at"]))


def list_audit(username: Optional[str] = None, limit: int = 100) -> list:
    sql = "SELECT * FROM audit_logs"
    args = []
    if username:
        sql += " WHERE username=?"
        args.append(username)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


# ================= HITL =================
def insert_hitl(rec: dict) -> None:
    rec.setdefault("created_at", _now())
    with _conn() as c:
        c.execute("""INSERT OR IGNORE INTO hitl_queue
            (record_id,username,user_input,matched_metric,generated_sql,reject_reason,status,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (rec.get("record_id"), rec.get("username", ""), rec.get("user_input", ""),
             rec.get("matched_metric", ""), rec.get("generated_sql", ""),
             rec.get("reject_reason", ""), rec.get("status", "pending"), rec["created_at"]))


def list_hitl(status: str = "pending", limit: int = 50) -> list:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM hitl_queue WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit)).fetchall()]


def decide_hitl(record_id: str, status: str, decided_by: str = "") -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE hitl_queue SET status=?, decided_by=?, decided_at=? WHERE record_id=?",
                        (status, decided_by, _now(), record_id))
        return cur.rowcount > 0


# ================= 反馈 =================
def insert_feedback(query_id: str, username: str, user_input: str,
                    generated_sql: str, rating: int, comment: str = "") -> None:
    with _conn() as c:
        c.execute("""INSERT INTO feedback
            (query_id,username,user_input,generated_sql,rating,comment,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (query_id, username, user_input, generated_sql, rating, comment, _now()))


def list_feedback(rating: Optional[int] = None, limit: int = 100) -> list:
    sql = "SELECT * FROM feedback"
    args = []
    if rating is not None:
        sql += " WHERE rating=?"
        args.append(rating)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def export_badcases(path: str) -> int:
    """把点踩的坏例导出为 JSONL，可作为评测集补充。"""
    bad = list_feedback(rating=-1)
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        import json
        for r in bad:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


# ================= 收藏 =================
def add_favorite(username: str, title: str, user_input: str, generated_sql: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO favorites(username,title,user_input,generated_sql,created_at) VALUES(?,?,?,?,?)",
                  (username, title, user_input, generated_sql, _now()))


def list_favorites(username: str) -> list:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM favorites WHERE username=? ORDER BY created_at DESC", (username,)).fetchall()]


def delete_favorite(fav_id: int, username: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM favorites WHERE id=? AND username=?", (fav_id, username))
        return cur.rowcount > 0
