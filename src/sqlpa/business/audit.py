"""
sqlpa.business.audit
====================
业务查询审计留痕：每次业务查询落一条记录（append 到本地 jsonl）并双写 SQLite。

设计要点（2026-09 加固）：
  - **单一权威 sink**：SQLite（可查询、append-only 触发器保护）是权威来源，JSONL 仅作兼容镜像。
    旧实现两个 sink 平权，可能分叉（JSONL 写成功、SQLite 失败时 /api/audit 与库内容不一致）；
    现在 /api/audit 一律读 SQLite，镜像失败只记日志不影响权威性。
  - **写入失败不再静默**：旧实现 `except Exception: pass`，SQLite 写失败无人知晓，
    审计可以悄悄丢；现在记录错误、计数、并暴露给 /health 与测试断言。
  - `tail_audit` 仍保留（读 JSONL 镜像、尾部倒读），供离线排查与旧脚本使用。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

_logger = logging.getLogger("sqlpa.audit")

_DEFAULT_LOG = Path(__file__).resolve().parents[3] / "data" / "business_audit.jsonl"

# 进程内审计写失败清单（供 /health、测试与运维排查"审计是否真在落盘"）
_FAILED_WRITES: List[Dict] = []
_MIRROR_FAILURES: List[Dict] = []


def audit_write_failures() -> List[Dict]:
    """返回本进程内**权威 sink（SQLite）**写入失败的审计记录（空 = 全部落盘成功）。"""
    return list(_FAILED_WRITES)


def audit_mirror_failures() -> List[Dict]:
    """返回 JSONL 镜像写入失败清单（不影响权威性，但要可见）。"""
    return list(_MIRROR_FAILURES)


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
    supervisor: str = ""        # Supervisor 路由决策标签：direct/drill/escalate/reject
    # 可观测性：这次查询花了多少次 LLM 调用、多少 token、多少钱、多久
    llm_calls: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def append_audit(rec: AuditRecord, path: str | Path | None = None) -> bool:
    """写一条审计。返回 False 表示**权威 sink（SQLite）写失败**。

    顺序即优先级：先写 SQLite（权威、可查询、append-only），再写 JSONL 镜像。
    权威失败 → 返回 False（调用方应告警/失败），并记账；
    镜像失败 → 只记日志与计数，不影响权威性（不再出现"两个 sink 各说各话"）。
    """
    p = Path(path or _DEFAULT_LOG)
    if not rec.create_time:
        rec.create_time = time.strftime("%Y-%m-%d %H:%M:%S")
    payload = rec.to_dict()

    ok = True
    try:
        from sqlpa.business import storage
        storage.insert_audit(payload)
    except Exception as e:  # noqa: BLE001
        ok = False
        _logger.error("审计权威 sink（SQLite）写入失败 query_id=%s err=%s",
                      rec.query_id, e, exc_info=True)
        _FAILED_WRITES.append({"query_id": rec.query_id, "at": rec.create_time})

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        _logger.warning("审计 JSONL 镜像写入失败（权威 sink 已成功，不影响一致性）path=%s err=%s",
                        p, e)
        _MIRROR_FAILURES.append({"query_id": rec.query_id, "at": rec.create_time})
    return ok


def _tail_lines(p: Path, n: int, chunk: int = 65536) -> List[str]:
    """从文件尾部倒读最多 n 行（内存占用与文件大小无关）。"""
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0 and buf.count(b"\n") <= n:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
        return [ln.decode("utf-8", "replace") for ln in buf.split(b"\n") if ln.strip()]


def tail_audit(n: int = 50, path: str | Path | None = None) -> list:
    """最近 n 条审计（从尾部读，不把全量 JSONL 载入内存）。"""
    p = Path(path or _DEFAULT_LOG)
    if not p.exists():
        return []
    out: List[Dict] = []
    for line in _tail_lines(p, max(1, int(n))):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-max(1, int(n)):]


def dump_audit(path: str | Path | None = None) -> list:
    p = Path(path or _DEFAULT_LOG)
    if not p.exists():
        return []
    return [json.loads(line) for line in open(p, encoding="utf-8") if line.strip()]
