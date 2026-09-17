"""
sqlpa.sandbox.sql_executor
==========================
确定性、只读、加固的 SQLite 沙箱执行器。

设计目标（对齐"确定性代码 vs LLM 推理"解耦原则）：
  - LLM 只负责生成 SQL 文本；本模块用纯净代码保证"安全 + 可执行 + 可度量"。
  - 只读隔离：打开 SQLite 为只读连接，物理上杜绝任何写库。
  - 语句级拦截：拒绝 DDL / 改写 / 高危语句、多语句、危险 PRAGMA / ATTACH。
  - 资源护栏：强制查询超时、结果行数上限、禁止无 LIMIT 的无限扫表。
  - 与数据集/评测无关，可独立单元测试。
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional


class SqlSecurityError(Exception):
    """命中安全护栏时抛出。"""

    def __init__(self, message: str, reason: str = "security"):
        super().__init__(message)
        self.reason = reason


# 写/高危关键字（对 SELECT 之外的负面白名单做拦截）
_BLOCKED_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "REPLACE", "MERGE", "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX",
    "GRANT", "REVOKE", "ANALYZE", "CALL", "EXEC", "EXECUTE",
}
# 可能被用于绕过只读的语句
_BLOCKED_PATTERNS = [
    re.compile(r"\b(count|sum|avg|max|min)\s*\(\s*['\"]?", re.I),  # 不是高危,排除
]
# 危险 Load 扩展 / shell
_BLOCKED_FUNCTIONS = {"load_extension", "print"}

# 多语句分隔（单引号/双引号内不算）
_QUOTE_CHARS = ("'", '"')


@dataclass
class ExecConfig:
    """沙箱资源护栏参数。"""
    timeout_seconds: float = 5.0
    max_rows: int = 500           # 结果集行数上限（防无 LIMIT 爆表）
    allow_multi_statement: bool = False
    extra_blocked_keywords: frozenset = frozenset()

    @classmethod
    def from_settings(cls, max_rows: Optional[int] = None) -> "ExecConfig":
        """按 config/settings.yaml 构造（改规则不改代码）。

        读取 security.timeout_seconds / security.allow_multi_statement /
        security.forbid_keywords 与 eval.max_rows。
        修复前这些键**从未被读取**，属"文档声称可配置、实际是死配置"。
        """
        try:
            from sqlpa.config import get as cfg_get
        except Exception:  # noqa: BLE001
            return cls()
        kws = cfg_get("security.forbid_keywords", []) or []
        return cls(
            timeout_seconds=float(cfg_get("security.timeout_seconds", 5.0) or 5.0),
            max_rows=int(max_rows if max_rows is not None
                         else (cfg_get("eval.max_rows", 500) or 500)),
            allow_multi_statement=bool(cfg_get("security.allow_multi_statement", False)),
            extra_blocked_keywords=frozenset(str(k).upper() for k in kws),
        )


@dataclass
class ExecResult:
    ok: bool
    rows: List[tuple] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    rowcount: int = 0
    error: Optional[str] = None
    reason: str = "ok"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "columns": self.columns, "rows": self.rows,
            "rowcount": self.rowcount, "error": self.error, "reason": self.reason,
        }


def _strip_sql_comments(sql: str) -> str:
    """去掉 -- 与 /* */ 注释，避免注释里藏关键字绕过。"""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _sanitize(sql: str, cfg: ExecConfig) -> str:
    """语句级安全校验与规整。

    1. 只能包含一个语句；2. 首关键字必须是 SELECT/WITH；3. 负面关键字/函数拦截。
    """
    cleaned = _strip_sql_comments(sql).strip()
    if not cleaned:
        raise SqlSecurityError("空语句", "empty")

    if cfg.allow_multi_statement:
        # 仍要防止分号后跟写语句
        if len(_split_statements(cleaned)) > 1:
            raise SqlSecurityError("仅允许单条语句", "multi_statement")
    else:
        if len(_split_statements(cleaned)) > 1:
            raise SqlSecurityError("仅允许单条语句", "multi_statement")

    # 首关键字必须是查询类（WITH 用于 CTE，其后仍应是 SELECT）
    head = re.match(r"\s*(select|with)\b", cleaned, re.I)
    if not head:
        raise SqlSecurityError("仅允许 SELECT/WITH 查询", "not_select")

    up = cleaned.upper()
    for kw in _BLOCKED_KEYWORDS | cfg.extra_blocked_keywords:
        # 用词边界匹配，严防子串误伤（如 SELECT 里的 col 名）
        if re.search(r"\b" + re.escape(kw) + r"\b", up):
            raise SqlSecurityError(f"拦截高危关键字: {kw}", "blocked_keyword")

    for fn in _BLOCKED_FUNCTIONS:
        if re.search(r"\b" + re.escape(fn) + r"\s*\(", up):
            raise SqlSecurityError(f"拦截危险函数: {fn}", "blocked_function")

    return cleaned


def _split_statements(sql: str) -> List[str]:
    """按分号拆分,但跳过引号内的分号,返回非空语句列表。"""
    parts, cur, quote = [], [], None
    for ch in sql:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in _QUOTE_CHARS:
            quote = ch
            cur.append(ch)
            continue
        if ch == ";":
            stmt = "".join(cur).strip()
            if stmt:
                parts.append(stmt)
            cur = []
            continue
        cur.append(ch)
    stmt = "".join(cur).strip()
    if stmt:
        parts.append(stmt)
    return parts


class SqlSandbox:
    """只读、受控执行的沙箱。

    默认对 SQLite 数据库文件工作（向后兼容）；也可注入外部连接器
    （MySQL/PostgreSQL，见 sqlpa.sandbox.dialects），语句级安全校验
    （_sanitize）与资源护栏对所有方言一致生效。
    """

    def __init__(self, db_path: str | Path | None = None,
                 config: Optional[ExecConfig] = None, connector=None):
        self.connector = connector
        if connector is None:
            if db_path is None:
                raise ValueError("需要 db_path 或 connector 之一")
            self.db_path = Path(db_path)
            if not self.db_path.exists():
                raise FileNotFoundError(f"数据库不存在: {self.db_path}")
        else:
            self.db_path = None
        self.config = config or ExecConfig()

    @property
    def dialect(self) -> str:
        return self.connector.dialect if self.connector is not None else "sqlite"

    def _connect_ro(self) -> sqlite3.Connection:
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=self.config.timeout_seconds)
        conn.execute("PRAGMA query_only = ON")   # 双保险：只读
        if self.config.timeout_seconds:
            conn.execute(f"PRAGMA busy_timeout = {int(self.config.timeout_seconds * 1000)}")
            # 真正的"查询超时"：SQLite 的 busy_timeout 只管等锁，不管长查询。
            # 这里用 progress_handler 每 N 个 VM 步检查一次耗时，超时即中断，
            # 避免 `SELECT ... FROM huge_table ORDER BY ...` 这类查询把进程挂住。
            self._install_deadline(conn)
        return conn

    def _install_deadline(self, conn: sqlite3.Connection) -> None:
        """给连接装一个截止时间：超过 timeout_seconds 就中断查询。"""
        deadline = time.monotonic() + float(self.config.timeout_seconds)
        # 每 1000 个 VM 指令回调一次（足够细，且开销可忽略）
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)

    def execute(self, sql: str, params: Optional[list] = None) -> ExecResult:
        # 语句级安全校验对所有方言一致生效
        try:
            cleaned = _sanitize(sql, self.config)
        except SqlSecurityError as e:
            return ExecResult(ok=False, error=str(e), reason=e.reason)

        # ---- 外部连接器（MySQL / PostgreSQL）----
        if self.connector is not None:
            try:
                columns, rows = self.connector.run_query(cleaned, self.config.max_rows + 1)
            except SqlSecurityError as e:
                return ExecResult(ok=False, error=str(e), reason=e.reason)
            except Exception as e:  # noqa: BLE001
                return ExecResult(ok=False, error=f"{self.connector.dialect}: {e}",
                                  reason="db_error")
            truncated = len(rows) > self.config.max_rows
            rows = [tuple(r) for r in rows[: self.config.max_rows]]
            return ExecResult(ok=True, rows=rows, columns=list(columns),
                              rowcount=len(rows),
                              reason="truncated" if truncated else "ok")

        # ---- SQLite（默认）----
        conn = None
        try:
            conn = self._connect_ro()
            cur = conn.execute(cleaned, params or [])
            rows = cur.fetchmany(self.config.max_rows + 1)
            truncated = len(rows) > self.config.max_rows
            rows = rows[: self.config.max_rows]
            columns = [d[0] for d in (cur.description or [])]
            return ExecResult(ok=True, rows=[tuple(r) for r in rows],
                              columns=columns, rowcount=len(rows),
                              reason="truncated" if truncated else "ok")
        except SqlSecurityError as e:
            return ExecResult(ok=False, error=str(e), reason=e.reason)
        except sqlite3.Error as e:
            return ExecResult(ok=False, error=f"sqlite: {e}", reason="sqlite_error")
        finally:
            if conn is not None:
                conn.close()


def make_sandbox(spec: Dict | str | Path, config: Optional[ExecConfig] = None) -> SqlSandbox:
    """按连接规格创建沙箱。

    spec 为路径 → SQLite；为 dict → 按 kind 分派：
      {"kind":"sqlite","path":...} / {"kind":"mysql",...} / {"kind":"postgres",...}
    """
    if not isinstance(spec, dict):
        return SqlSandbox(spec, config)
    kind = spec.get("kind", "sqlite")
    if kind == "sqlite":
        return SqlSandbox(spec["path"], config)
    from sqlpa.sandbox.dialects import make_connector
    return SqlSandbox(connector=make_connector(spec), config=config)
