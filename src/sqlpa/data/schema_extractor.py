"""
sqlpa.data.schema_extractor
===========================
从 SQLite 数据库或 Spider/BIRD 的 tables.json 提取数据库 schema（表/列/类型/主外键）。

定位：这是 Schema Linker Agent 的"知识底座"。提取出的 schema 供两层使用：
  - Schema Linker：做检索与裁剪，过滤冗余表列。
  - LLM Agent：作为生成 SQL 的上下文。
本模块为确定性逻辑，与 LLM 无关。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Column:
    name: str
    dtype: str = "TEXT"
    primary_key: bool = False
    nullable: bool = True
    # Spider 扩展字段（可选）
    references_table: Optional[str] = None
    references_col: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Table:
    name: str
    columns: List[Column] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "columns": [c.to_dict() for c in self.columns]}


@dataclass
class Schema:
    db_id: str
    tables: List[Table] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"db_id": self.db_id, "tables": [t.to_dict() for t in self.tables]}

    def table_names(self) -> List[str]:
        return [t.name for t in self.tables]

    def size(self) -> int:
        return sum(len(t.columns) for t in self.tables)


def extract_from_sqlite(db_path: str | Path, db_id: Optional[str] = None) -> Schema:
    """从 SQLite 系统表提取 schema。"""
    db_path = Path(db_path)
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables_rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables: List[Table] = []
        for tname, _sql in tables_rows:
            col_rows = conn.execute(f'PRAGMA table_info("{tname}")').fetchall()
            cols: List[Column] = []
            for cid, cname, ctype, notnull, dflt, pk in col_rows:
                cols.append(Column(name=cname, dtype=ctype or "TEXT",
                                   primary_key=(pk > 0), nullable=not bool(notnull)))
            tables.append(Table(name=tname, columns=cols))
        return Schema(db_id=db_id or db_path.stem, tables=tables)
    finally:
        conn.close()


def extract_from_tables_json(path: str | Path, db_id: Optional[str] = None) -> Schema:
    """从 Spider/BIRD 的 tables.json 解析 schema。

    tables.json 条目结构（Spider）：{column_names, column_types, primary_keys,
      foreign_keys, table_names}。这里做轻量解析。
    """
    data = json.load(open(str(path), encoding="utf-8"))
    # 单库：data 可能直接是一个库对象列表；Spider 顶层常为 list，BIRD 为 dict
    if isinstance(data, dict):
        entries = [data]
    else:
        entries = data
    # 若为 list[db]，取第一个（调用方可指定 db_id）
    entry = entries[0] if entries else {}
    table_names = entry.get("table_names", [])
    column_names = entry.get("column_names", [])
    column_types = entry.get("column_types", [])
    primary_keys = entry.get("primary_keys", [])
    foreign_keys = entry.get("foreign_keys", [])
    # Spider: column_names 为 [table_idx, name]；table 0 为 "*" 特殊表
    tables: Dict[int, Table] = {}
    for t_idx, tname in enumerate(table_names):
        tables[t_idx] = Table(name=tname)
    for col_idx, (t_idx, cname) in enumerate(column_names):
        ctype = column_types[col_idx] if col_idx < len(column_types) else "TEXT"
        pk = col_idx in primary_keys
        tbl = tables.get(t_idx)
        if tbl is not None:
            tbl.columns.append(Column(name=cname, dtype=ctype, primary_key=pk))
    # 外键：Spider 用 [i, col_idx, ref_t, ref_c]
    for fk in foreign_keys:
        try:
            t_idx, c_idx, ref_t, ref_c = fk
            tbl = tables.get(t_idx)
            if tbl and c_idx < len(tbl.columns):
                tbl.columns[c_idx].references_table = table_names[ref_t]
                tbl.columns[c_idx].references_col = ref_c
        except Exception:
            continue
    return Schema(db_id=db_id or entry.get("db_id", "unknown"),
                  tables=list(tables.values()))
