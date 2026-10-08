"""
sqlpa.business.hitl
===================
HITL(人工介入)工作流：把"指标异常 / 权限拦截 / 公式被改 / 引擎失败"等边界情况流转给
人工分析师复核，形成"机器处理 + 人工兜底 + **重跑验证**"的闭环。

本轮加固（对应评审发现"写入是真、状态机是真，但推不动、也无法验证"）：
  - **SQLite 权威**：`storage.hitl_queue` 是权威存储；`data/hitl_queue.jsonl` 退化为
    append-only 事件日志（不再原地重写——旧实现会在重写时静默丢弃解析不了的行）。
  - **幂等键**：同一 (指标, 时间窗) 已有未关闭工单时复用，不再每次分析都新开一张
    （旧实现每问一次就重复告警，人工队列被刷屏）。
  - **状态流转可追溯**：每次流转写 `hitl_history`（谁把工单从什么状态改成了什么）。
  - **闭环靠重跑**：`verify()` 会把该指标在工单时间窗上**重新算一遍**，仍异常 → reopened，
    已恢复 → verified。人工说"修好了"不算数，数据说了才算。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

_DEFAULT = Path(__file__).resolve().parents[3] / "data" / "hitl_queue.jsonl"

# 工单状态机（README 口径：待确认 → 处理中 → 已修复 → 已验证 / 误报）
STATUSES = ("pending", "in_progress", "fixed", "verified", "dismissed", "reopened")
_OPEN_STATUSES = ("pending", "in_progress", "fixed", "reopened")


@dataclass
class HITLRecord:
    record_id: str = ""
    user_input: str = ""
    matched_metric: str = ""
    owner: str = ""
    generated_sql: str = ""
    reject_reason: str = ""
    role: str = "analyst"
    status: str = "pending"
    time_spec: str = ""            # 异常所属时间窗（重跑验证用）
    dims: List[str] = field(default_factory=list)
    human_note: str = ""
    ai_note: str = ""
    corrected_sql: str = ""
    create_time: str = ""
    resolve_time: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _append_event(rec: dict, event: str, path: Optional[str | Path] = None) -> None:
    """append-only 事件日志（兼容旧脚本读取；权威数据在 SQLite）。"""
    p = Path(path or _DEFAULT)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, **rec}, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 镜像失败不影响权威存储


def enqueue(user_input: str, matched_metric: str, generated_sql: str,
            reject_reason: str, role: str = "analyst",
            path: str | Path | None = None, owner: str = "",
            ai_note: str = "", time_spec: str = "",
            dims: Optional[List[str]] = None) -> str:
    """登记工单并返回 record_id。

    **幂等**：同一 (matched_metric, time_spec) 已有未关闭工单 → 复用其 record_id，
    避免同一异常每次分析都新开一张、把人工队列刷屏。
    """
    from sqlpa.business import storage
    dims = list(dims or [])
    try:
        existing = storage.hitl_find_open(matched_metric, time_spec)
    except Exception:  # noqa: BLE001
        existing = None
    if existing and (not ai_note or existing.get("reject_reason") == reject_reason):
        if ai_note:            # 新一次分析带了 AI 草稿 → 更新备注，但仍是同一张工单
            try:
                storage.hitl_set_status(existing["record_id"], existing.get("status")
                                        or "pending", actor="system",
                                        note=f"补充 AI 归因草稿：{ai_note[:200]}")
            except Exception:  # noqa: BLE001
                pass
        # 幂等复用也要在镜像日志里留痕（否则只看 jsonl 的人会以为"这次没告警"）
        _append_event({**existing, "reused": True}, "dedup", path)
        return existing["record_id"]

    rec = HITLRecord(record_id=uuid.uuid4().hex[:8], user_input=user_input,
                     matched_metric=matched_metric, owner=owner,
                     generated_sql=generated_sql, reject_reason=reject_reason,
                     role=role, status="pending", ai_note=ai_note,
                     time_spec=time_spec, dims=dims,
                     create_time=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        storage.insert_hitl({
            "record_id": rec.record_id, "username": role, "user_input": user_input,
            "matched_metric": matched_metric, "owner": owner,
            "generated_sql": generated_sql, "reject_reason": reject_reason,
            "status": "pending", "time_spec": time_spec, "dims": dims,
        })
    except Exception:  # noqa: BLE001
        pass
    _append_event(rec.to_dict(), "enqueue", path)
    return rec.record_id


def queue(path: str | Path | None = None, status: str = "pending") -> List[dict]:
    """工单列表（**读 SQLite 权威存储**；status='all' 返回全部）。"""
    from sqlpa.business import storage
    if status == "closed":
        rows = storage.list_hitl("all", limit=500)
        return [r for r in rows if r.get("status") not in _OPEN_STATUSES]
    return storage.list_hitl(status, limit=200)


def get(record_id: str) -> Optional[dict]:
    from sqlpa.business import storage
    return storage.hitl_get(record_id)


def history(record_id: str) -> List[dict]:
    from sqlpa.business import storage
    return storage.hitl_history(record_id)


def resolve(record_id: str, decision: str, human_note: str = "",
            corrected_sql: str = "", path: str | Path | None = None,
            actor: str = "") -> bool:
    """流转工单状态（**可被 API 调用**；旧实现零调用者，工单永远推不动）。

    decision ∈ STATUSES；`human_note` 是备注，`actor` 是操作者（不再拿备注当操作者）。
    """
    from sqlpa.business import storage
    if decision not in STATUSES:
        return False
    rec = storage.hitl_get(record_id)
    if not rec:
        return False
    ok = storage.hitl_set_status(record_id, decision, actor=actor, note=human_note)
    if ok:
        _append_event({**rec, "status": decision, "human_note": human_note,
                       "corrected_sql": corrected_sql, "actor": actor}, "transition", path)
        try:
            from sqlpa.business import audit as _audit
            _audit.append_audit(_audit.AuditRecord(
                username=actor or "unknown", user_role="admin",
                user_input=rec.get("user_input", ""),
                matched_metric=rec.get("matched_metric", ""),
                is_success=True, mode="hitl",
                reject_reason=f"HITL {record_id}: {rec.get('status')} → {decision}"
                              f"（{human_note[:60]}）", certified=False))
        except Exception:  # noqa: BLE001
            pass
    return ok


def verify(record_id: str, cfg, db, db_path: Optional[str] = None,
           threshold_pct: float = 0.05, actor: str = "system") -> Dict:
    """**重跑验证**：把工单对应的指标在工单时间窗上重新算一遍，用数据判定是否真的恢复。

    - 仍判异常 → 状态置 `reopened`（"已修复"不成立）；
    - 已不异常 → 状态置 `verified`（闭环关闭）。
    没有这一步，"闭环"就只是"人工点了个按钮"。
    """
    from . import attribution
    from sqlpa.business import storage
    rec = storage.hitl_get(record_id)
    if not rec:
        return {"ok": False, "reason": f"工单不存在：{record_id}"}
    metric = rec.get("matched_metric") or ""
    time_spec = rec.get("time_spec") or ""
    if not metric or not time_spec:
        return {"ok": False, "reason": "工单缺少 指标/时间窗 信息，无法重跑验证",
                "record_id": record_id}
    try:
        r = attribution.analyze(cfg, db, metric, current_spec=time_spec,
                                dims=(rec.get("dims") or None),
                                threshold_pct=threshold_pct)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"重跑失败：{type(e).__name__}: {e}",
                "record_id": record_id}
    if not r.get("ok"):
        new_status = "fixed"          # 数据不足时不宣布已验证，留在"已修复"等人工
        note = f"重跑未取得可比较数据：{r.get('reason', '')}"
    elif r.get("is_abnormal"):
        new_status = "reopened"
        note = (f"重跑仍判异常：波动 {r.get('change_pct', 0) * 100:+.1f}%"
                f"（阈值 {threshold_pct:.0%}）")
    else:
        new_status = "verified"
        note = (f"重跑已恢复：波动 {r.get('change_pct', 0) * 100:+.1f}%"
                f"（阈值 {threshold_pct:.0%}）")
    storage.hitl_set_status(record_id, new_status, actor=actor, note=note)
    _append_event({**rec, "status": new_status, "human_note": note}, "verify")
    return {"ok": True, "record_id": record_id, "status": new_status, "note": note,
            "change_pct": r.get("change_pct"), "is_abnormal": bool(r.get("is_abnormal")),
            "reverify_sql": r.get("sql") or ""}
