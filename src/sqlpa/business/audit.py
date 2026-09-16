"""
sqlpa.business.audit
====================
业务查询审计留痕：每次业务查询落一条记录（append 到本地 jsonl）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

_DEFAULT_LOG = Path(__file__).resolve().parents[3] / "data" / "business_audit.jsonl"


@dataclass
class AuditRecord:
    query_id: str = ""
    username: str = ""
    user_role: str = "analyst"
    user_input: str = ""
    matched_metric: str = ""
    generated_sql: str = ""
    is_success: bool = False
    execute_cost_ms: float = 0.0
    result_rows: int = 0
    reject_reason: str = ""
    create_time: str = ""
    mode: str = "metric"        # metric=口径内(认证) / free=自由查询(未认证)
    certified: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def append_audit(rec: AuditRecord, path: str | Path | None = None) -> None:
    # 双写：JSONL（兼容旧脚本读取）+ SQLite（新查询入口）
    p = Path(path or _DEFAULT_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not rec.create_time:
        rec.create_time = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
    try:
        from sqlpa.business import storage
        storage.insert_audit(rec.to_dict())
    except Exception:
        pass  # SQLite 失败不阻断主流程


def dump_audit(path: str | Path | None = None) -> list:
    p = Path(path or _DEFAULT_LOG)
    if not p.exists():
        return []
    return [json.loads(line) for line in open(p, encoding="utf-8") if line.strip()]
