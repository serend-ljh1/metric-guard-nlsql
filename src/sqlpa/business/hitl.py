"""
sqlpa.business.hitl
====================
HITL(人工介入)工作流：把"指标不支持 / 权限拦截 / 引擎修复失败"等边界情况，流转给
人工数据分析师复核/修正，形成"机器处理 + 人工兜底"的闭环。

记录落盘到 data/hitl_queue.jsonl。人工可：退回修正 / 采纳 / 驳回。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

_DEFAULT = Path(__file__).resolve().parents[3] / "data" / "hitl_queue.jsonl"


@dataclass
class HITLRecord:
    record_id: str = ""
    user_input: str = ""
    matched_metric: str = ""
    generated_sql: str = ""
    reject_reason: str = ""
    role: str = "analyst"
    status: str = "pending"        # pending / approved / corrected / dismissed
    human_note: str = ""
    corrected_sql: str = ""
    create_time: str = ""
    resolve_time: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def enqueue(user_input: str, matched_metric: str, generated_sql: str,
            reject_reason: str, role: str = "analyst",
            path: str | Path | None = None) -> str:
    p = Path(path or _DEFAULT)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = HITLRecord(record_id=uuid.uuid4().hex[:8], user_input=user_input,
                     matched_metric=matched_metric, generated_sql=generated_sql,
                     reject_reason=reject_reason, role=role,
                     status="pending", create_time=time.strftime("%Y-%m-%d %H:%M:%S"))
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
    try:
        from sqlpa.business import storage
        storage.insert_hitl({
            "record_id": rec.record_id, "username": role,
            "user_input": user_input, "matched_metric": matched_metric,
            "generated_sql": generated_sql, "reject_reason": reject_reason,
            "status": "pending",
        })
    except Exception:
        pass
    return rec.record_id


def queue(path: str | Path | None = None, status: str = "pending") -> List[dict]:
    p = Path(path or _DEFAULT)
    if not p.exists():
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()
            and json.loads(l).get("status") == status]


def resolve(record_id: str, decision: str, human_note: str = "",
            corrected_sql: str = "", path: str | Path | None = None) -> bool:
    p = Path(path or _DEFAULT)
    if not p.exists():
        return False
    lines = open(p, encoding="utf-8").read().splitlines()
    out = []
    hit = False
    for l in lines:
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get("record_id") == record_id and not hit:
            d["status"] = decision
            d["human_note"] = human_note
            d["corrected_sql"] = corrected_sql
            d["resolve_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            hit = True
        out.append(json.dumps(d, ensure_ascii=False))
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    try:
        from sqlpa.business import storage
        storage.decide_hitl(record_id, decision, decided_by=human_note)
    except Exception:
        pass
    return hit
