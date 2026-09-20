"""
sqlpa.business.storage
======================
统一持久化层：SQLite 单文件 (data/app.db)，承载用户、审计、HITL、反馈、收藏。

替代原先 audit.jsonl / hitl.jsonl 的文件存储，支持按条件查询、分页、统计，
为"查询历史""反馈闭环""真实登录"提供数据底座。
"""
from __future__ import annotations

import json
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
    llm_calls     INTEGER DEFAULT 0,   -- 可观测性：本次查询的 LLM 调用次数
    total_tokens  INTEGER DEFAULT 0,
    cost          REAL DEFAULT 0,
    latency_ms    REAL DEFAULT 0,
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

-- 审计表 append-only：应用账号不得删改审计痕迹。
-- 此前 audit_logs 可直接 DELETE（实测能删掉行），"留痕"就成了君子协定。
-- 用触发器把不变性放到数据库层：即使有人拿到连接，也改不了历史记录。
CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete
BEFORE DELETE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs 为 append-only：禁止删除审计记录');
END;

CREATE TRIGGER IF NOT EXISTS audit_logs_no_update
BEFORE UPDATE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs 为 append-only：禁止修改审计记录');
END;

-- 指标口径版本历史（append-only）：谁在什么时候把哪个指标从什么改成了什么。
-- 语义层的核心承诺是"可追溯"，此前只有 YAML 里一个手写的 version 字符串，
-- 改公式既没有 diff 也没有回滚，报告里引用的旧口径无法复现。
CREATE TABLE IF NOT EXISTS metric_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_key   TEXT NOT NULL,
    version_seq  INTEGER NOT NULL,
    action       TEXT NOT NULL,        -- create / update / rollback
    before_json  TEXT,
    after_json   TEXT,
    content_hash TEXT,
    actor        TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_versions_key ON metric_versions(metric_key);

CREATE TRIGGER IF NOT EXISTS metric_versions_no_delete
BEFORE DELETE ON metric_versions
BEGIN
    SELECT RAISE(ABORT, 'metric_versions 为 append-only：禁止删除口径历史');
END;

CREATE TRIGGER IF NOT EXISTS metric_versions_no_update
BEFORE UPDATE ON metric_versions
BEGIN
    SELECT RAISE(ABORT, 'metric_versions 为 append-only：禁止修改口径历史');
END;
"""


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


# 轻量迁移：老库缺列时补齐（SQLite 的 ADD COLUMN 是廉价的元数据操作）
_MIGRATIONS = [
    ("audit_logs", "llm_calls", "INTEGER DEFAULT 0"),
    ("audit_logs", "total_tokens", "INTEGER DEFAULT 0"),
    ("audit_logs", "cost", "REAL DEFAULT 0"),
    ("audit_logs", "latency_ms", "REAL DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, decl in _MIGRATIONS:
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                conn.commit()
            except sqlite3.Error:
                pass


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ================= 用户 =================
# 口令哈希：PBKDF2-HMAC-SHA256 + **每用户随机盐** + 常量时间比较。
# 旧实现是 `sha256(全局固定盐 + 口令)`：同一口令所有人哈希相同（撞库/彩虹表直接命中），
# 且 `==` 比较泄漏时序。格式 `pbkdf2_sha256$<iterations>$<salt>$<hash>`；
# 旧的 64 位 hex 仍可校验（向后兼容），验证成功后由调用方决定是否升级重存。
_PBKDF2_ITERATIONS = 200_000
_LEGACY_GLOBAL_SALT = "sqlpa_salt_2026"      # 仅用于校验历史遗留哈希


def _hash_pwd(pwd: str) -> str:
    """生成新格式口令哈希（随机盐）。"""
    import base64
    import hashlib
    import secrets
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return (f"pbkdf2_sha256${_PBKDF2_ITERATIONS}$"
            f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}")


def _legacy_hash(pwd: str, salt: str = _LEGACY_GLOBAL_SALT) -> str:
    import hashlib
    return hashlib.sha256((salt + pwd).encode()).hexdigest()


def verify_password(pwd: str, stored: str) -> bool:
    """校验口令（常量时间）。兼容旧格式，便于平滑升级。"""
    import base64
    import hashlib
    import hmac
    stored = stored or ""
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _algo, iters, salt_b64, hash_b64 = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"),
                                     base64.b64decode(salt_b64), int(iters))
            return hmac.compare_digest(base64.b64encode(dk).decode(), hash_b64)
        except Exception:  # noqa: BLE001
            return False
    # 旧格式：全局固定盐的 sha256
    return hmac.compare_digest(_legacy_hash(pwd), stored)


def needs_rehash(stored: str) -> bool:
    """是否为旧格式（应升级为随机盐 PBKDF2）。"""
    return not (stored or "").startswith("pbkdf2_sha256$")


def create_user(username: str, password: str, role: str = "analyst") -> bool:
    try:
        with _conn() as c:
            c.execute("INSERT INTO users(username,password,role,created_at) VALUES(?,?,?,?)",
                      (username, _hash_pwd(password), role, _now()))
        return True
    except sqlite3.IntegrityError:
        return False


def set_password(username: str, password: str) -> bool:
    """重设口令（也用于把旧格式哈希就地升级为随机盐 PBKDF2）。"""
    with _conn() as c:
        cur = c.execute("UPDATE users SET password=? WHERE username=?",
                        (_hash_pwd(password), username))
        return cur.rowcount > 0


def verify_user(username: str, password: str) -> Optional[str]:
    """返回角色名或 None（校验通过且哈希是旧格式时顺带升级存储）。"""
    with _conn() as c:
        row = c.execute("SELECT role,password FROM users WHERE username=?", (username,)).fetchone()
    if not row or not verify_password(password, row["password"]):
        return None
    if needs_rehash(row["password"]):
        set_password(username, password)      # 透明升级：用户无感
    return row["role"]


def list_users() -> list:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT username,role,created_at FROM users").fetchall()]


def ensure_default_users() -> None:
    """**不再预置任何弱口令账号。**

    旧实现会创建 `admin/admin123` 与 `analyst/analyst123` —— 一旦忘记删除就是公开后门。
    现在只有显式配置 `SQLPA_BOOTSTRAP_ADMIN_PASSWORD`（可配 `SQLPA_BOOTSTRAP_ADMIN_USER`）
    才会创建首个管理员；未配置时什么都不建，并打印提示。
    """
    import os
    pwd = (os.environ.get("SQLPA_BOOTSTRAP_ADMIN_PASSWORD") or "").strip()
    if not pwd:
        print("[安全提示] 未配置 SQLPA_BOOTSTRAP_ADMIN_PASSWORD，未创建任何账号"
              "（不再预置 admin/admin123）。需要账号请显式设置该环境变量或用 create_user()。")
        return
    user = (os.environ.get("SQLPA_BOOTSTRAP_ADMIN_USER") or "admin").strip() or "admin"
    if create_user(user, pwd, role="admin"):
        print(f"[安全提示] 已创建管理员账号 {user}（口令来自环境变量）。")


# ================= 审计 =================
def insert_audit(rec: dict) -> None:
    rec.setdefault("created_at", _now())
    rec.setdefault("query_id", uuid.uuid4().hex[:12])
    with _conn() as c:
        c.execute("""INSERT INTO audit_logs
            (query_id,username,user_role,user_input,matched_metric,generated_sql,
             is_success,execute_cost_ms,result_rows,reject_reason,mode,certified,
             llm_calls,total_tokens,cost,latency_ms,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.get("query_id"), rec.get("username", ""), rec.get("user_role", ""),
             rec.get("user_input", ""), rec.get("matched_metric", ""),
             rec.get("generated_sql", ""), int(rec.get("is_success", 0)),
             rec.get("execute_cost_ms", 0.0), rec.get("result_rows", 0),
             rec.get("reject_reason", ""), rec.get("mode", "metric"),
             int(rec.get("certified", 0)),
             int(rec.get("llm_calls", 0) or 0), int(rec.get("total_tokens", 0) or 0),
             float(rec.get("cost", 0.0) or 0.0), float(rec.get("latency_ms", 0.0) or 0.0),
             rec["created_at"]))


def list_audit(username: Optional[str] = None, limit: int = 100, offset: int = 0) -> list:
    """按时间倒序列审计（分页）。审计的**权威读取入口**：/api/audit 走这里。"""
    sql = "SELECT * FROM audit_logs"
    args: list = []
    if username:
        sql += " WHERE username=?"
        args.append(username)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args.extend([max(1, int(limit)), max(0, int(offset))])
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def count_audit(username: Optional[str] = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM audit_logs"
    args: list = []
    if username:
        sql += " WHERE username=?"
        args.append(username)
    with _conn() as c:
        return int(c.execute(sql, args).fetchone()["n"])


# ================= 指标口径版本 =================
def insert_metric_version(metric_key: str, action: str, before: Optional[dict],
                          after: Optional[dict], content_hash: str = "",
                          actor: str = "") -> int:
    """追加一条指标口径版本记录（append-only），返回 version_seq。"""
    key = (metric_key or "").strip()
    with _conn() as c:
        row = c.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM metric_versions "
                        "WHERE metric_key=?", (key,)).fetchone()
        seq = int(row["n"]) + 1
        c.execute("""INSERT INTO metric_versions
            (metric_key,version_seq,action,before_json,after_json,content_hash,actor,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (key, seq, action,
             json.dumps(before, ensure_ascii=False) if before else None,
             json.dumps(after, ensure_ascii=False) if after else None,
             content_hash, actor, _now()))
        return seq


def list_metric_versions(metric_key: Optional[str] = None, limit: int = 50) -> list:
    sql = "SELECT * FROM metric_versions"
    args: list = []
    if metric_key:
        sql += " WHERE metric_key=?"
        args.append(metric_key)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def get_metric_version(metric_key: str, version_seq: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM metric_versions WHERE metric_key=? AND version_seq=?",
                        (metric_key, int(version_seq))).fetchone()
    return dict(row) if row else None


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
