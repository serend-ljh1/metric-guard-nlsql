"""
sqlpa.data.schema_extractor
===========================
从 SQLite 系统表提取 schema（表/列/类型/主键）。

定位：为取数/分析链提供数据表列的确定性视图（只读 introspection），
不依赖 LLM，也不参与 SQL 生成。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional


@dataclass
class Column:
    name: str
    dtype: str = "TEXT"
    primary_key: bool = False
    nullable: bool = True
    # 外键引用（可选）
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
