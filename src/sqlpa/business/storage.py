"""
sqlpa.business.storage
======================
统一持久化层：SQLite 单文件 (data/app.db)，承载用户、审计、HITL、反馈、收藏。

替代原先 audit.jsonl / hitl.jsonl 的文件存储，支持按条件查询、分页、统计，
为"查询历史""反馈闭环""真实登录"提供数据底座。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import List, Optional

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
    owner         TEXT,
    generated_sql TEXT,
    reject_reason TEXT,
    time_spec     TEXT,                -- 异常所属时间窗（重跑验证用）
    dims_json     TEXT,                -- 异常归因维度（重跑验证用）
    status        TEXT DEFAULT 'pending',  -- pending/in_progress/fixed/verified/dismissed/reopened
    decided_by    TEXT,
    decided_at    TEXT,
    created_at    TEXT NOT NULL
);

-- HITL 状态流转历史（append-only）：每个工单"谁在什么时候把它从什么状态改成了什么"
CREATE TABLE IF NOT EXISTS hitl_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id   TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hitl_history ON hitl_history(record_id);

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

-- 口径确认（HITL 前置门）：LLM 猜出来的口径必须先由人确认才允许拉数/归因。
-- 旧实现把"确认"放在前端内存里、且只看一个客户端自报的 confirm_metric 字符串 ——
-- 既不落库（刷新即失、别人看不到、无审计），又能被一个真值参数绕过门。
-- 现在：确认是一条**记录**，执行时按记录里的 spec 逐字执行，且用掉即置 confirmed。
CREATE TABLE IF NOT EXISTS metric_confirmations (
    id           TEXT PRIMARY KEY,
    question     TEXT,
    metric       TEXT NOT NULL,
    metric_name  TEXT,
    dims_json    TEXT,
    filters_json TEXT,
    time_grain   TEXT,
    method       TEXT,                  -- 提出该口径的方式：llm（系统猜的）
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending / confirmed / dismissed
    actor        TEXT,
    created_at   TEXT NOT NULL,
    decided_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_confirm_status ON metric_confirmations(status);

-- 会话记忆持久化（跨重启存活 / 多 worker 共享同一 DB 文件时一致）。
-- 此前会话记忆是**纯进程内 dict**：重启即丢、多 worker 各自一份（下钻记忆分裂）。
CREATE TABLE IF NOT EXISTS session_memory (
    key        TEXT PRIMARY KEY,
    data_json  TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_memory_time ON session_memory(updated_at);

-- 限速计数持久化（同上：多 worker 共享同一 DB 文件时，限速才真正是"全局"的）
CREATE TABLE IF NOT EXISTS rate_limit (
    key        TEXT PRIMARY KEY,
    hits_json  TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_limit_time ON rate_limit(updated_at);

-- 登录会话（动态 token）：把"真实登录"做成可运营能力，
-- 取代仅靠静态 SQLPA_API_TOKENS（token 与角色绑死在环境变量里、无法登出）。
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    role       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);
"""


def _journal_mode() -> str:
    """SQLite journal 模式：`SQLPA_SQLITE_JOURNAL_MODE`（默认 WAL）。

    WAL（Write-Ahead Logging）让**读不阻塞写、写不阻塞读**：此前默认的 rollback
    journal 下，一个进程在写就会把整库锁住，多 worker 共享同一 `data/app.db` 时
    并发请求会直接 `database is locked`。WAL 是"多 worker 部署"的前提。
    个别文件系统（网络盘/某些容器卷）不支持 WAL，可用该环境变量改回 `DELETE`。
    """
    v = (os.environ.get("SQLPA_SQLITE_JOURNAL_MODE") or "WAL").strip().upper()
    return v or "WAL"


def _busy_timeout_ms() -> int:
    """写锁等待毫秒数：`SQLPA_SQLITE_BUSY_TIMEOUT_MS`（默认 5000）。"""
    try:
        v = int(os.environ.get("SQLPA_SQLITE_BUSY_TIMEOUT_MS") or 5000)
    except ValueError:
        v = 5000
    return v if v >= 0 else 5000


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=_busy_timeout_ms() / 1000.0)
    conn.row_factory = sqlite3.Row
    # 多 worker / 并发读写就绪：WAL + busy_timeout。
    # busy_timeout 让并发写**等待**而不是立刻抛 "database is locked"；
    # journal_mode 是**持久属性**（写进库文件头），每条连接设一次是幂等的。
    # pragmatic：独立 try，个别文件系统不支持 WAL 时不能因此让整库不可用。
    try:
        conn.execute(f"PRAGMA journal_mode={_journal_mode()}")
        conn.execute(f"PRAGMA busy_timeout={_busy_timeout_ms()}")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


# 轻量迁移：老库缺列时补齐（SQLite 的 ADD COLUMN 是廉价的元数据操作）
_MIGRATIONS = [
    ("audit_logs", "llm_calls", "INTEGER DEFAULT 0"),
    ("audit_logs", "total_tokens", "INTEGER DEFAULT 0"),
    ("audit_logs", "cost", "REAL DEFAULT 0"),
    ("audit_logs", "latency_ms", "REAL DEFAULT 0"),
    ("hitl_queue", "owner", "TEXT"),
    ("hitl_queue", "time_spec", "TEXT"),
    ("hitl_queue", "dims_json", "TEXT"),
    # HITL SLA：截止时间 / 超时升级 / 临期催办
    ("hitl_queue", "due_at", "REAL"),
    ("hitl_queue", "escalated", "INTEGER DEFAULT 0"),
    ("hitl_queue", "escalated_at", "TEXT"),
    ("hitl_queue", "reminded_at", "TEXT"),
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


# ================= 登录会话（动态 token） =================
# 静态 SQLPA_API_TOKENS 的问题：token 与角色写死在环境变量里 —— 改密码要改环境变量、
# 无法登出、无法知道"这个 token 是谁"。这里把登录做成**一条会话记录**：
# username → 由 users 表校验；token 是随机串（可撤销、可过期、可追溯归属人）。
_LOGIN_TTL_DEFAULT = 12 * 3600.0      # 会话默认有效期（12 小时）


def _login_ttl() -> float:
    try:
        v = float(os.environ.get("SQLPA_LOGIN_TTL") or _LOGIN_TTL_DEFAULT)
    except ValueError:
        v = _LOGIN_TTL_DEFAULT
    return v if v > 0 else _LOGIN_TTL_DEFAULT


def create_session(username: str, role: str, ttl_seconds: Optional[float] = None) -> dict:
    """为已通过口令校验的用户签发一条会话记录（含随机 token 与到期时间）。"""
    import secrets
    tok = secrets.token_urlsafe(32)
    now = time.time()
    ttl = float(_login_ttl() if ttl_seconds is None else ttl_seconds)
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))   # 顺手清过期
        c.execute("INSERT INTO sessions(token,username,role,created_at,expires_at) "
                  "VALUES(?,?,?,?,?)", (tok, username, role, _now(), now + ttl))
    return {"token": tok, "username": username, "role": role, "expires_at": now + ttl}


def get_session(token: Optional[str]) -> Optional[dict]:
    """按 token 取有效会话（过期/不存在 → None）。纯读，不在请求热路径上写库。"""
    if not token:
        return None
    with _conn() as c:
        row = c.execute("SELECT token,username,role,expires_at FROM sessions WHERE token=?",
                        (token,)).fetchone()
    if not row or float(row["expires_at"]) < time.time():
        return None
    return dict(row)


def delete_session(token: Optional[str]) -> bool:
    """登出：撤销该 token（此后它既不是会话、也不在任何静态映射里）。"""
    if not token:
        return False
    with _conn() as c:
        return c.execute("DELETE FROM sessions WHERE token=?", (token,)).rowcount > 0


# ================= 会话记忆 / 限速（持久化） =================
# 这两者此前都是**纯进程内**状态：重启即丢，多 worker 各持一份（下钻记忆分裂、限速形同虚设）。
# 落到 SQLite 后：单 worker 重启可续、多 worker 共享同一 DB 文件时限速与记忆都一致。
def session_load(key: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT data_json FROM session_memory WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        v = json.loads(row["data_json"])
    except Exception:  # noqa: BLE001
        return None
    return v if isinstance(v, dict) else None


def session_save(key: str, data: dict, ttl_seconds: float = 3600.0) -> None:
    """写回会话记忆（并按 TTL 顺带清理老记录，避免这张表无限增长）。"""
    now = time.time()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO session_memory(key,data_json,updated_at) VALUES(?,?,?)",
                  (key, json.dumps(data or {}, ensure_ascii=False), now))
        c.execute("DELETE FROM session_memory WHERE updated_at < ?",
                  (now - max(0.0, float(ttl_seconds)),))


def rate_load(key: str) -> List[float]:
    with _conn() as c:
        row = c.execute("SELECT hits_json FROM rate_limit WHERE key=?", (key,)).fetchone()
    if not row:
        return []
    try:
        v = json.loads(row["hits_json"])
    except Exception:  # noqa: BLE001
        return []
    return [float(x) for x in v] if isinstance(v, list) else []


def rate_save(key: str, hits: List[float], window_seconds: float = 60.0) -> None:
    now = time.time()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO rate_limit(key,hits_json,updated_at) VALUES(?,?,?)",
                  (key, json.dumps([float(h) for h in hits]), now))
        c.execute("DELETE FROM rate_limit WHERE updated_at < ?",
                  (now - max(0.0, float(window_seconds)),))


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


# ================= 口径确认（HITL 前置门） =================
def insert_confirmation(question: str, metric: str, metric_name: str = "",
                        dims: Optional[list] = None, filters: Optional[list] = None,
                        time_grain: Optional[str] = None, method: str = "llm",
                        actor: str = "") -> str:
    """登记一条待确认口径，返回 its id（客户端凭 id 确认，不再自报指标名）。"""
    cid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("""INSERT INTO metric_confirmations
            (id,question,metric,metric_name,dims_json,filters_json,time_grain,method,
             status,actor,created_at)
            VALUES(?,?,?,?,?,?,?,?, 'pending', ?,?)""",
            (cid, question, metric, metric_name,
             json.dumps(list(dims or []), ensure_ascii=False),
             json.dumps([[t, v] for t, v in (filters or [])], ensure_ascii=False),
             time_grain or "", method, actor, _now()))
    return cid


def get_confirmation(cid: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM metric_confirmations WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    rec = dict(row)
    try:
        rec["dims"] = json.loads(rec.get("dims_json") or "[]")
        rec["filters"] = [tuple(x) for x in json.loads(rec.get("filters_json") or "[]")]
    except Exception:  # noqa: BLE001
        rec["dims"], rec["filters"] = [], []
    return rec


def list_confirmations(status: Optional[str] = None, limit: int = 50) -> list:
    sql = "SELECT * FROM metric_confirmations"
    args: list = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    for r in rows:
        try:
            r["dims"] = json.loads(r.get("dims_json") or "[]")
            r["filters"] = [tuple(x) for x in json.loads(r.get("filters_json") or "[]")]
        except Exception:  # noqa: BLE001
            r["dims"], r["filters"] = [], []
    return rows


def decide_confirmation(cid: str, status: str, actor: str = "") -> bool:
    """置 confirmed / dismissed（原子更新；已处理过的不再改）。"""
    if status not in ("confirmed", "dismissed", "pending"):
        return False
    with _conn() as c:
        cur = c.execute("UPDATE metric_confirmations SET status=?, actor=?, decided_at=? "
                        "WHERE id=? AND status='pending'", (status, actor, _now(), cid))
        return cur.rowcount > 0


# ================= HITL =================
# 工单 SLA：登记即带截止时间（due_at），超期由 sweep 置 escalated 并升级。
_OPEN_HITL_STATUSES = ("pending", "in_progress", "fixed", "reopened")


def sla_hours() -> float:
    """工单 SLA 时长（小时），`SQLPA_HITL_SLA_HOURS`（默认 24）。"""
    try:
        v = float(os.environ.get("SQLPA_HITL_SLA_HOURS") or 24)
    except ValueError:
        v = 24.0
    return v if v > 0 else 24.0


def insert_hitl(rec: dict) -> None:
    rec.setdefault("created_at", _now())
    # 登记即确定截止时间：没有 due_at，"超时升级"就无从判定
    due_at = rec.get("due_at")
    if due_at is None:
        due_at = time.time() + sla_hours() * 3600
    with _conn() as c:
        c.execute("""INSERT OR IGNORE INTO hitl_queue
            (record_id,username,user_input,matched_metric,owner,generated_sql,
             reject_reason,time_spec,dims_json,status,due_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.get("record_id"), rec.get("username", ""), rec.get("user_input", ""),
             rec.get("matched_metric", ""), rec.get("owner", ""),
             rec.get("generated_sql", ""), rec.get("reject_reason", ""),
             rec.get("time_spec", ""), json.dumps(rec.get("dims") or [], ensure_ascii=False),
             rec.get("status", "pending"), float(due_at), rec["created_at"]))


def hitl_get(record_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM hitl_queue WHERE record_id=?", (record_id,)).fetchone()
    if not row:
        return None
    rec = dict(row)
    try:
        rec["dims"] = json.loads(rec.get("dims_json") or "[]")
    except Exception:  # noqa: BLE001
        rec["dims"] = []
    return rec


def hitl_find_open(matched_metric: str, time_spec: str = "") -> Optional[dict]:
    """幂等键：同一(指标, 时间窗)若已有未关闭工单，则返回它而不是再开一张。

    旧实现每次分析都新 uuid → 同一异常反复告警，人工队列被刷屏。
    """
    open_status = ("pending", "in_progress", "fixed", "reopened")
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM hitl_queue WHERE matched_metric=? AND "
            f"COALESCE(time_spec,'')=? AND status IN {open_status} "
            "ORDER BY id DESC LIMIT 1", (matched_metric, time_spec or "")).fetchone()
    return dict(row) if row else None


def hitl_set_status(record_id: str, status: str, actor: str = "", note: str = "") -> bool:
    """状态流转（写历史 + 原子更新；工单不存在返回 False）。"""
    with _conn() as c:
        row = c.execute("SELECT status FROM hitl_queue WHERE record_id=?",
                        (record_id,)).fetchone()
        if not row:
            return False
        old = row["status"]
        c.execute("UPDATE hitl_queue SET status=?, decided_by=?, decided_at=? WHERE record_id=?",
                  (status, actor, _now(), record_id))
        c.execute("""INSERT INTO hitl_history(record_id,from_status,to_status,actor,note,created_at)
                     VALUES(?,?,?,?,?,?)""", (record_id, old, status, actor, note, _now()))
        return True


def hitl_history(record_id: str) -> list:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM hitl_history WHERE record_id=? ORDER BY id", (record_id,)).fetchall()]


def list_hitl(status: str = "pending", limit: int = 50) -> list:
    sql = "SELECT * FROM hitl_queue"
    args: list = []
    if status and status != "all":
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    for r in rows:
        try:
            r["dims"] = json.loads(r.get("dims_json") or "[]")
        except Exception:  # noqa: BLE001
            r["dims"] = []
    return rows


def _hitl_rows_with_dims(rows) -> list:
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["dims"] = json.loads(d.get("dims_json") or "[]")
        except Exception:  # noqa: BLE001
            d["dims"] = []
        out.append(d)
    return out


def hitl_due(now_ts: Optional[float] = None,
             statuses: tuple = _OPEN_HITL_STATUSES) -> list:
    """已超 SLA 但**尚未升级**的未闭环工单（sweep 的升级候选）。"""
    ts = time.time() if now_ts is None else float(now_ts)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM hitl_queue WHERE status IN {tuple(statuses)} "
            "AND due_at IS NOT NULL AND due_at < ? AND COALESCE(escalated,0)=0 "
            "ORDER BY due_at", (ts,)).fetchall()
    return _hitl_rows_with_dims(rows)


def hitl_soon_due(within_seconds: float, now_ts: Optional[float] = None,
                  statuses: tuple = _OPEN_HITL_STATUSES) -> list:
    """临期工单（due_at 落在 [now, now+window) 内且未催办过）——催办候选。"""
    ts = time.time() if now_ts is None else float(now_ts)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM hitl_queue WHERE status IN {tuple(statuses)} "
            "AND due_at IS NOT NULL AND due_at >= ? AND due_at < ? "
            "AND COALESCE(escalated,0)=0 AND reminded_at IS NULL ORDER BY due_at",
            (ts, ts + float(within_seconds))).fetchall()
    return _hitl_rows_with_dims(rows)


def hitl_mark_escalated(record_id: str, actor: str = "system:sla", note: str = "") -> bool:
    """置为"已升级"并写一条流转历史（from=to=当前状态，表示"状态未变但已升级"）。"""
    with _conn() as c:
        row = c.execute("SELECT status FROM hitl_queue WHERE record_id=?",
                        (record_id,)).fetchone()
        if not row:
            return False
        now = _now()
        c.execute("UPDATE hitl_queue SET escalated=1, escalated_at=? WHERE record_id=?",
                  (now, record_id))
        c.execute("""INSERT INTO hitl_history(record_id,from_status,to_status,actor,note,created_at)
                     VALUES(?,?,?,?,?,?)""",
                  (record_id, row["status"], row["status"], actor,
                   note or "超时升级：已超过 SLA 截止时间仍未闭环", now))
        return True


def hitl_mark_reminded(record_id: str, actor: str = "system:sla", note: str = "") -> bool:
    """置为"已催办"并写历史（幂等：同一工单只催办一次）。"""
    with _conn() as c:
        row = c.execute("SELECT status FROM hitl_queue WHERE record_id=?",
                        (record_id,)).fetchone()
        if not row:
            return False
        now = _now()
        c.execute("UPDATE hitl_queue SET reminded_at=? WHERE record_id=?", (now, record_id))
        c.execute("""INSERT INTO hitl_history(record_id,from_status,to_status,actor,note,created_at)
                     VALUES(?,?,?,?,?,?)""",
                  (record_id, row["status"], row["status"], actor,
                   note or "临期催办：接近 SLA 截止时间，请尽快处理", now))
        return True


def list_hitl_escalated(limit: int = 50) -> list:
    """已升级且仍未闭环的工单（`/api/hitl/escalations` 的数据来源）。"""
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM hitl_queue WHERE COALESCE(escalated,0)=1 "
            f"AND status IN {_OPEN_HITL_STATUSES} "
            "ORDER BY escalated_at DESC, id DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
    return _hitl_rows_with_dims(rows)


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
