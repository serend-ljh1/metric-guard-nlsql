"""apiroutes.workspace：个人工作台端点（查询历史 / 收藏 / 反馈）。

为什么要有这一组端点：`storage` 里早就有 `audit_logs` / `favorites` / `feedback`
三张表与配套读写函数，但**没有任何端点**把它们暴露出来 —— 用户问过的历史只能去
查审计、收藏与反馈更是"有表无入口"。于是"查询历史 / 收藏回访 / 反馈坏例"这三件
最日常的事在系统里根本做不到。

这里把它们接上（**只增不改**：既有端点路径与响应结构不动）：
    GET    /api/history             当前调用者的查询历史（分页，来自审计权威存储）
    GET    /api/favorites           当前调用者的收藏
    POST   /api/favorites           新增收藏
    DELETE /api/favorites/{id}      删除自己的收藏（他人收藏删不掉）
    POST   /api/feedback            提交点赞/点踩 + 评论
    GET    /api/feedback            查看反馈（非 admin 只看自己的；admin 看全部）

身份一律走 `_identify`（登录会话 > 静态 token > 默认角色）：历史与收藏都以
**调用者为维度**隔离，收集到的反馈可用于补充评测集（见 storage.export_badcases）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from .deps import FavoriteIn, FeedbackIn, _identify, _rate_limit

router = APIRouter()


@router.get("/api/history")
def api_history(limit: int = 50, offset: int = 0,
                x_api_token: Optional[str] = Header(default=None)):
    """当前调用者的查询历史（时间倒序，分页）。

    读审计权威存储（SQLite 而非 JSONL 镜像），按 `username` 隔离：
    登录用户看到的就是"我"问过的；未配置凭据时本地默认身份为 anonymous，
    只看到本机匿名查询，不会串到别人。
    """
    ident = _identify(x_api_token, "analyst")
    from sqlpa.business import storage
    lim = max(1, min(limit, 500))
    off = max(0, offset)
    rows = storage.list_audit(username=ident["username"], limit=lim, offset=off)
    return {"username": ident["username"], "total": storage.count_audit(ident["username"]),
            "limit": lim, "offset": off, "items": rows}


@router.get("/api/favorites")
def api_favorites(x_api_token: Optional[str] = Header(default=None)):
    """当前调用者的收藏列表。"""
    ident = _identify(x_api_token, "analyst")
    from sqlpa.business import storage
    return storage.list_favorites(ident["username"])


@router.post("/api/favorites")
def api_add_favorite(body: FavoriteIn, x_api_token: Optional[str] = Header(default=None)):
    """新增一条收藏（标题必填）。"""
    ident = _identify(x_api_token, "analyst")
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="收藏标题不能为空")
    _rate_limit(ident["caller"], "favorite")
    from sqlpa.business import storage
    storage.add_favorite(ident["username"], title[:120], body.user_input or "",
                         body.generated_sql or "")
    return {"ok": True, "username": ident["username"]}


@router.delete("/api/favorites/{fav_id}")
def api_delete_favorite(fav_id: int, x_api_token: Optional[str] = Header(default=None)):
    """删除**自己的**收藏（`delete_favorite` 按 username 过滤，删不到别人的）。"""
    ident = _identify(x_api_token, "analyst")
    from sqlpa.business import storage
    if not storage.delete_favorite(int(fav_id), ident["username"]):
        raise HTTPException(status_code=404, detail=f"收藏不存在或不属于当前用户：{fav_id}")
    return {"ok": True, "id": int(fav_id)}


@router.post("/api/feedback")
def api_submit_feedback(body: FeedbackIn, x_api_token: Optional[str] = Header(default=None)):
    """提交对一次查询的反馈（rating 仅允许 1 / -1）。"""
    ident = _identify(x_api_token, "analyst")
    if body.rating not in (1, -1):
        raise HTTPException(status_code=400, detail="rating 仅允许 1（点赞）或 -1（点踩）")
    _rate_limit(ident["caller"], "feedback")
    from sqlpa.business import storage
    storage.insert_feedback(body.query_id or "", ident["username"],
                            body.user_input or "", body.generated_sql or "",
                            int(body.rating), body.comment or "")
    return {"ok": True, "rating": int(body.rating)}


@router.get("/api/feedback")
def api_list_feedback(rating: Optional[int] = None, limit: int = 100,
                      x_api_token: Optional[str] = Header(default=None)):
    """查看反馈：非 admin 只看自己的，admin 可看全部（rating=-1 即为坏例）。"""
    ident = _identify(x_api_token, "analyst")
    from sqlpa.business import storage
    rows = storage.list_feedback(rating=rating, limit=max(1, min(limit, 500)))
    if ident["role"] != "admin":
        rows = [r for r in rows if r.get("username") == ident["username"]]
    return {"rating": rating, "count": len(rows), "items": rows}
