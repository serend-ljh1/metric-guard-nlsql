"""apiroutes.subscriptions：订阅告警类端点（/api/subscriptions*）。"""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from .deps import SubscriptionIn, _bind_asof, _cfg, _db_path, _resolve_role

router = APIRouter()


@router.get("/api/subscriptions")
def api_list_subscriptions(x_api_token: Optional[str] = Header(default=None)):
    """订阅告警规则列表。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business.deliver import load_subscriptions
    return load_subscriptions()


@router.post("/api/subscriptions")
def api_add_subscription(s: SubscriptionIn,
                         x_api_token: Optional[str] = Header(default=None)):
    """新增订阅（**仅 admin**）：某指标波动超阈值就按通道推送。"""
    role = _resolve_role(x_api_token, "analyst")
    if role != "admin":
        raise HTTPException(status_code=403, detail="新增订阅需要 admin 权限")
    from sqlpa.business.deliver import add_subscription
    return add_subscription(s.metric, s.threshold_pct, owner=s.owner,
                            question=s.question, time_spec=s.time_spec,
                            dims=s.dims, channel=s.channel, webhook=s.webhook)


@router.delete("/api/subscriptions/{sub_id}")
def api_remove_subscription(sub_id: str,
                            x_api_token: Optional[str] = Header(default=None)):
    """删除订阅（**仅 admin**）。"""
    role = _resolve_role(x_api_token, "analyst")
    if role != "admin":
        raise HTTPException(status_code=403, detail="删除订阅需要 admin 权限")
    from sqlpa.business.deliver import remove_subscription
    if not remove_subscription(sub_id):
        raise HTTPException(status_code=404, detail=f"未找到订阅 {sub_id}")
    return {"ok": True, "removed": sub_id}


@router.post("/api/subscriptions/check")
def api_check_subscriptions(dry: bool = True,
                            x_api_token: Optional[str] = Header(default=None)):
    """立刻检查一遍订阅（**仅 admin**）。

    dry=true 只返回"哪些需要推送"；dry=false 会真的投递（webhook 会发出请求）。
    常驻调度请用 `python tools/run_subscriptions.py` 交给 cron。
    """
    role = _resolve_role(x_api_token, "analyst")
    if role != "admin":
        raise HTTPException(status_code=403, detail="检查订阅需要 admin 权限")
    from sqlpa.business.deliver import check_subscriptions, deliver_alerts
    _bind_asof()
    alerts = check_subscriptions(_cfg(), sqlite3.connect(_db_path()))
    need = [a for a in alerts if a.get("status") == "alert"]
    out = {"alerts": alerts, "need_push": len(need), "dry": dry}
    if not dry:
        out["delivery"] = deliver_alerts(need)
    return out
