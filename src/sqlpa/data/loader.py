"""
sqlpa.data.loader
=================
加载 Text-to-SQL 评测基准（Spider / BIRD / 自建迷你基准）到统一中间格式。

统一格式（本项目的 canonical 结构）：
  benchmark.json = {
    "db_id": str,
    "db_path": str,            # 对应 SQLite 文件路径
    "tables_json": str|None,   # 可选，Spider 风格 tables.json
    "questions": [
      {"id": int, "question": str, "gold_sql": str,
       "gold_result": [[...], ...] | None,   # None 则用 sandbox 执行 gold_sql 求出
       "difficulty": "simple"|"complex",     # 用于路由/分层评测
       "complexity_hint": str|None,          # 标注是否多表/嵌套/聚合/数值约束
       "domain_knowledge": bool}             # BIRD 标记是否需要库外常识
    ]
  }

Spider/BIRD 原始格式:
  Spider: tests 目录含 database/*.db, tables.json, dev.json {question, query, db_id}
  BIRD:   benchmark.json / dev.json + database sqlite → 这里做适配，缺少字段用 None。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Question:
    id: int
    question: str
    gold_sql: str
    db_id: str
    difficulty: str = "simple"
    gold_result: Optional[List[list]] = None
    complexity_hint: Optional[str] = None
    domain_knowledge: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Benchmark:
    db_id: str
    db_path: str
    questions: List[Question] = field(default_factory=list)
    tables_json: Optional[str] = None
    name: str = "benchmark"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "db_id": self.db_id, "db_path": self.db_path,
            "tables_json": self.tables_json,
            "questions": [q.to_dict() for q in self.questions],
        }


def load_json_benchmark(path: str | Path) -> Benchmark:
    """加载自定义 canonical 格式。"""
    data = json.load(open(str(path), encoding="utf-8"))
    db_id = data["db_id"]
    questions = []
    for i, q in enumerate(data.get("questions", [])):
        questions.append(Question(
            id=q.get("id", i),
            question=q["question"],
            gold_sql=q["gold_sql"],
            db_id=q.get("db_id", db_id),
            difficulty=q.get("difficulty", "simple"),
            gold_result=q.get("gold_result"),
            complexity_hint=q.get("complexity_hint"),
            domain_knowledge=q.get("domain_knowledge", False),
        ))
    return Benchmark(db_id=db_id, db_path=data["db_path"],
                     tables_json=data.get("tables_json"),
                     questions=questions, name=data.get("name", "benchmark"))


def load_spider_dev(path: str | Path, db_root: str | Path,
                    db_name: Optional[str] = None) -> Dict[str, Benchmark]:
    """加载 Spider 的 dev/test.json。返回 {db_id: Benchmark}。

    Spider 的 database/ 下每个库一个 <db_id>.sqlite（兼容 .db）。
    """
    db_root = Path(db_root)
    data = json.load(open(str(path), encoding="utf-8"))
    # data 是 [{question, query, db_id}]；spider 的 query 即 gold_sql
    per_db: Dict[str, list] = {}
    for rec in data:
        per_db.setdefault(rec["db_id"], []).append(rec)
    out: Dict[str, Benchmark] = {}
    for db_id, recs in per_db.items():
        # Spider 实际用 .sqlite，兼容 .db
        p_sqlite = db_root / db_id / f"{db_id}.sqlite"
        p_db = db_root / db_id / f"{db_id}.db"
        db_path = p_sqlite if p_sqlite.exists() else (p_db if p_db.exists() else p_sqlite)
        questions = []
        for i, rec in enumerate(recs):
            questions.append(Question(
                id=i, question=rec["question"], gold_sql=rec["query"],
                db_id=db_id, gold_result=None))
        out[db_id] = Benchmark(db_id=db_id, db_path=str(db_path),
                               questions=questions, name=f"Spider-{db_id}")
    return out
