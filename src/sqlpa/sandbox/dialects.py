"""
sqlpa.sandbox.dialects
======================
外部数据库方言适配层（MySQL / PostgreSQL）。

设计（对齐"推理归 LLM、计算归代码"原则）：
  - 本层只负责「连接 + 只读执行 + schema 提取」；语句级安全校验
    （SELECT-only / 高危关键字拦截）统一由 sql_executor._sanitize 在进入本层之前完成。
  - 只读双保险：除语句校验外，会话级也强制只读——
      MySQL     : SET SESSION TRANSACTION READ ONLY
      PostgreSQL: 连接即 readonly 事务（psycopg2 set_session(readonly=True)）
    生产部署仍建议配合数据库侧只读账号。
  - 驱动惰性导入：只有真正连接对应数据库时才需要安装 pymysql / psycopg2。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple


class DbConnector(ABC):
    """外部数据库连接器统一接口。"""

    dialect: str = ""
    prompt_hint: str = ""   # 注入 LLM 的方言提示（生成对应方言的 SQL）

    @abstractmethod
    def _open(self):
        """打开一个（只读）连接。"""

    def run_query(self, sql: str, fetch_limit: int) -> Tuple[List[str], List[tuple]]:
        """执行只读查询，返回 (列名, 行)。连接用完即关。"""
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchmany(fetch_limit) if fetch_limit else cur.fetchall()
            columns = [d[0] for d in (cur.description or [])]
            return columns, [tuple(r) for r in rows]
        finally:
            conn.close()

    @abstractmethod
    def extract_schema(self, db_id: Optional[str] = None) -> Dict:
        """提取 schema，返回与 schema_extractor.Schema.to_dict() 同构的 dict。"""

    def ping(self) -> Tuple[bool, str]:
        """连通性自检，返回 (ok, message)。"""
        try:
            columns, rows = self.run_query("SELECT 1", 1)
            return True, f"{self.dialect} 连接正常"
        except Exception as e:  # noqa: BLE001
            return False, f"{self.dialect} 连接失败: {e}"


def _schema_dict(db_id: str, tables: Dict[str, List[Dict]],
                   fks: List[Tuple[str, str, str, str]]) -> Dict:
    """把 {表名:[列...]} + 外键列表 组装成 Schema.to_dict() 同构结构。"""
    fk_map: Dict[Tuple[str, str], Tuple[str, str]] = {
        (t.lower(), c.lower()): (rt, rc) for t, c, rt, rc in fks}
    out_tables = []
    for tname, cols in tables.items():
        out_cols = []
        for c in cols:
            ref = fk_map.get((tname.lower(), c["name"].lower()))
            out_cols.append({
                "name": c["name"], "dtype": c.get("dtype", "TEXT"),
                "primary_key": bool(c.get("primary_key")),
                "nullable": bool(c.get("nullable", True)),
                "references_table": ref[0] if ref else None,
                "references_col": ref[1] if ref else None,
            })
        out_tables.append({"name": tname, "columns": out_cols})
    return {"db_id": db_id, "tables": out_tables}


class MySQLConnector(DbConnector):
    dialect = "mysql"
    prompt_hint = "目标数据库是 MySQL，请生成 MySQL 方言的 SQL。"

    def __init__(self, host: str, user: str, password: str, database: str,
                 port: int = 3306, timeout: float = 8.0):
        self.host, self.port = host, int(port)
        self.user, self.password, self.database = user, password, database
        self.timeout = timeout

    def _open(self):
        try:
            import pymysql
        except ImportError as e:
            raise RuntimeError("未安装 pymysql：pip install pymysql") from e
        conn = pymysql.connect(host=self.host, port=self.port, user=self.user,
                               password=self.password, database=self.database,
                               charset="utf8mb4",
                               connect_timeout=int(self.timeout),
                               read_timeout=int(max(self.timeout, 5)))
        cur = conn.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY")   # 会话级只读双保险
        return conn

    def extract_schema(self, db_id: Optional[str] = None) -> Dict:
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT table_name, column_name, data_type, column_key, is_nullable "
                "FROM information_schema.columns WHERE table_schema=%s "
                "ORDER BY table_name, ordinal_position", (self.database,))
            tables: Dict[str, List[Dict]] = {}
            for tname, cname, dtype, key, nullable in cur.fetchall():
                tables.setdefault(tname, []).append(
                    {"name": cname, "dtype": dtype, "primary_key": key == "PRI",
                     "nullable": nullable == "YES"})
            cur.execute(
                "SELECT table_name, column_name, referenced_table_name, referenced_column_name "
                "FROM information_schema.key_column_usage "
                "WHERE table_schema=%s AND referenced_table_name IS NOT NULL",
                (self.database,))
            fks = [(t, c, rt, rc) for t, c, rt, rc in cur.fetchall()]
            return _schema_dict(db_id or self.database, tables, fks)
        finally:
            conn.close()


class PostgresConnector(DbConnector):
    dialect = "postgres"
    prompt_hint = "目标数据库是 PostgreSQL，请生成 PostgreSQL 方言的 SQL。"

    def __init__(self, host: str, user: str, password: str, database: str,
                 port: int = 5432, schema: str = "public", timeout: float = 8.0):
        self.host, self.port = host, int(port)
        self.user, self.password, self.database = user, password, database
        self.schema, self.timeout = schema, timeout

    def _open(self):
        try:
            import psycopg2
        except ImportError as e:
            raise RuntimeError("未安装 psycopg2：pip install psycopg2-binary") from e
        conn = psycopg2.connect(host=self.host, port=self.port, user=self.user,
                                password=self.password, dbname=self.database,
                                connect_timeout=int(self.timeout))
        conn.set_session(readonly=True, autocommit=True)   # 会话级只读双保险
        return conn

    def extract_schema(self, db_id: Optional[str] = None) -> Dict:
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT c.table_name, c.column_name, c.data_type, c.is_nullable, "
                "       (k.column_name IS NOT NULL) AS is_pk "
                "FROM information_schema.columns c "
                "LEFT JOIN information_schema.table_constraints t "
                "  ON t.table_schema=c.table_schema AND t.table_name=c.table_name "
                " AND t.constraint_type='PRIMARY KEY' "
                "LEFT JOIN information_schema.key_column_usage k "
                "  ON k.constraint_name=t.constraint_name AND k.column_name=c.column_name "
                "WHERE c.table_schema=%s "
                "ORDER BY c.table_name, c.ordinal_position", (self.schema,))
            tables: Dict[str, List[Dict]] = {}
            for tname, cname, dtype, nullable, is_pk in cur.fetchall():
                tables.setdefault(tname, []).append(
                    {"name": cname, "dtype": dtype, "primary_key": bool(is_pk),
                     "nullable": nullable == "YES"})
            cur.execute(
                "SELECT k.table_name, k.column_name, ccu.table_name, ccu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage k "
                "  ON k.constraint_name=tc.constraint_name AND k.table_schema=tc.table_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name=tc.constraint_name "
                "WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema=%s",
                (self.schema,))
            fks = [(t, c, rt, rc) for t, c, rt, rc in cur.fetchall()]
            return _schema_dict(db_id or self.database, tables, fks)
        finally:
            conn.close()


def make_connector(spec: Dict) -> DbConnector:
    """按连接规格 dict 构造连接器。

    mysql   : {kind, host, port, user, password, database}
    postgres: {kind, host, port, user, password, database, schema?}
    """
    kind = spec.get("kind", "")
    if kind == "mysql":
        return MySQLConnector(host=spec["host"], user=spec["user"],
                              password=spec.get("password", ""),
                              database=spec["database"],
                              port=spec.get("port", 3306),
                              timeout=spec.get("timeout", 8.0))
    if kind in ("postgres", "postgresql"):
        return PostgresConnector(host=spec["host"], user=spec["user"],
                                 password=spec.get("password", ""),
                                 database=spec["database"],
                                 port=spec.get("port", 5432),
                                 schema=spec.get("schema", "public"),
                                 timeout=spec.get("timeout", 8.0))
    raise ValueError(f"未知数据源类型: {kind}（支持 mysql / postgres）")
