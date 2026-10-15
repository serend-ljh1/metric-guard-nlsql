"""apiroutes.hitl：人工复核与口径确认类端点（/api/hitl*、/api/confirmations*）。"""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from .deps import HitlDecisionIn, _bind_asof, _cfg, _db_path, _identify, _resolve_role

router = APIRouter()


@router.get("/api/hitl/escalations")
def api_hitl_escalations(limit: int = 50,
                         x_api_token: Optional[str] = Header(default=None)):
    """**已超 SLA 且升级**的未闭环工单（另附临期未催办的工单数）。

    为什么要有：只把工单写进表里、没人推动，"闭环"就只是文档里的词。
    这里把"谁的任务已经超期"变成可查询的事实。
    """
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import hitl, storage
    return {"sla_hours": storage.sla_hours(),
            "escalated": storage.list_hitl_escalated(limit=max(1, min(limit, 500))),
            "soon_due": [{"record_id": r.get("record_id"),
                          "matched_metric": r.get("matched_metric"),
                          "owner": r.get("owner"), "due_at": r.get("due_at")}
                         for r in hitl.soon_due()]}


@router.post("/api/hitl/sweep")
def api_hitl_sweep(x_api_token: Optional[str] = Header(default=None)):
    """立即执行一次 SLA 巡检（**仅 admin**）：超期 → 升级并留痕；临期 → 催办。

    生产上应由 cron 调用（见 tools/run_hitl_sla.py）；这个端点让演示与运维
    可以不依赖调度器手动触发一次。
    """
    ident = _identify(x_api_token, "analyst")
    if ident["role"] != "admin":
        raise HTTPException(status_code=403, detail="执行 HITL SLA 巡检需要 admin 权限")
    from sqlpa.business import hitl
    return hitl.sweep(actor=f"api:{ident['username']}")


@router.get("/api/hitl")
def api_hitl(status: str = "pending", limit: int = 50,
             x_api_token: Optional[str] = Header(default=None)):
    """人工复核队列（SQLite 权威存储）。status=all / closed 可查全量或已关闭。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import storage
    if status == "closed":
        rows = storage.list_hitl("all", limit=max(1, min(limit, 500)))
        return [r for r in rows if r.get("status") in ("verified", "dismissed")]
    return storage.list_hitl(status=status, limit=max(1, min(limit, 500)))


@router.get("/api/hitl/{record_id}/history")
def api_hitl_history(record_id: str, x_api_token: Optional[str] = Header(default=None)):
    """工单状态流转历史（谁在什么时候把它从什么状态改成了什么）。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import hitl
    if not hitl.get(record_id):
        raise HTTPException(status_code=404, detail=f"工单不存在：{record_id}")
    return hitl.history(record_id)


@router.post("/api/hitl/{record_id}/decide")
def api_hitl_decide(record_id: str, body: HitlDecisionIn,
                    x_api_token: Optional[str] = Header(default=None)):
    """推进工单状态（**仅 admin**）——人工处理的入口，此前系统里根本不存在这个端点。"""
    ident = _identify(x_api_token, "analyst")
    if ident["role"] != "admin":
        raise HTTPException(status_code=403, detail="推进 HITL 工单需要 admin 权限")
    from sqlpa.business import hitl
    if body.status not in hitl.STATUSES:
        raise HTTPException(status_code=400,
                            detail=f"非法状态 {body.status}，允许：{list(hitl.STATUSES)}")
    if not hitl.resolve(record_id, body.status, human_note=body.note,
                        actor=f"api:{ident['username']}"):
        raise HTTPException(status_code=404, detail=f"工单不存在：{record_id}")
    return {"ok": True, "record_id": record_id, "status": body.status}


@router.post("/api/hitl/{record_id}/verify")
def api_hitl_verify(record_id: str, x_api_token: Optional[str] = Header(default=None)):
    """**重跑验证**（仅 admin）：把工单指标在其时间窗上重新算一遍，
    仍异常 → reopened，已恢复 → verified。人工说"修好了"不算数，数据说了才算。"""
    ident = _identify(x_api_token, "analyst")
    if ident["role"] != "admin":
        raise HTTPException(status_code=403, detail="重跑验证需要 admin 权限")
    from sqlpa.business import hitl
    _bind_asof()
    out = hitl.verify(record_id, _cfg(), sqlite3.connect(_db_path()), _db_path(),
                      actor=f"api:{ident['username']}")
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("reason", "重跑验证失败"))
    return out


@router.get("/api/confirmations")
def api_confirmations(status: str = "pending", limit: int = 50,
                      x_api_token: Optional[str] = Header(default=None)):
    """待确认口径列表（HITL 前置门）：LLM 猜出来的口径在确认前不允许拉数。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import storage
    return storage.list_confirmations(status=status or None,
                                      limit=max(1, min(limit, 500)))


@router.post("/api/confirmations/{confirm_id}/override")
def api_confirmations_override(confirm_id: str, metric: str,
                               x_api_token: Optional[str] = Header(default=None)):
    """**改选**口径：把待确认记录换成另一个合法指标，返回新的 confirm_id。

    原记录置 dismissed（留痕），新记录继承维度/时间范围，只换指标 ——
    改选不等于可以顺手改口径范围。
    """
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import confirm as _confirm
    new_id, err = _confirm.override(_cfg(), confirm_id, metric)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"ok": True, "confirm_id": new_id, "metric": metric,
            "replaced": confirm_id}


@router.post("/api/confirmations/{confirm_id}/dismiss")
def api_confirmations_dismiss(confirm_id: str,
                              x_api_token: Optional[str] = Header(default=None)):
    """驳回一条待确认口径（仅 admin）。"""
    ident = _identify(x_api_token, "analyst")
    if ident["role"] != "admin":
        raise HTTPException(status_code=403, detail="处理口径确认需要 admin 权限")
    from sqlpa.business import storage
    if not storage.decide_confirmation(confirm_id, "dismissed",
                                       actor=f"api:{ident['username']}"):
        raise HTTPException(status_code=404, detail=f"确认记录不存在或已处理：{confirm_id}")
    return {"ok": True, "confirm_id": confirm_id, "status": "dismissed"}
