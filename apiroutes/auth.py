"""apiroutes.auth：登录 / 登出 / 身份查询（`/api/auth/*`）。

为什么要有这一组端点：此前"身份"只有一个静态环境变量 `SQLPA_API_TOKENS="tok:role"` ——
token 与角色写死、无法登出、审计里记不出"这是谁"。有了登录会话后：
  - `users` 表 + PBKDF2 校验 → 签发随机 token（`sessions` 表，含归属人与到期时间）；
  - `deps._resolve_identity` 因此能把**审计关联到具体的人**；
  - 登出即撤销 token。
未配置任何账号时（本地/演示），这组端点也可用（`SQLPA_BOOTSTRAP_ADMIN_PASSWORD` 建号），
但系统在"无凭据"下仍按最小权限默认角色放行，行为与之前完全一致（向后兼容）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .deps import _identify, _rate_limit, _resolve_identity

router = APIRouter()


class LoginIn(BaseModel):
    """登录入参：用户名 + 口令。"""
    username: str = Field(..., description="用户名（users 表）")
    password: str = Field(..., description="口令")


def _auth_audit(action: str, username: str, role: str) -> None:
    """登录/登出留痕（审计表 append-only；写失败不影响登录本身）。"""
    try:
        from sqlpa.business.audit import AuditRecord, append_audit
        append_audit(AuditRecord(username=username, user_role=role, user_input="",
                                 is_success=(action == "login"), mode="auth",
                                 reject_reason=f"auth {action}"))
    except Exception:  # noqa: BLE001
        pass


@router.post("/api/auth/login")
def api_auth_login(body: LoginIn):
    """口令校验通过 → 签发会话 token（前端存 localStorage，后续请求带 X-API-Token）。"""
    from sqlpa.business import storage
    # 口令尝试限速：避免被脚本拿常见口令字典无限次撞库
    _rate_limit(f"login:{body.username}", "login")
    role = storage.verify_user(body.username, body.password)
    if not role:
        _auth_audit("login_failed", body.username, "")
        raise HTTPException(status_code=401, detail="用户名或口令不正确")
    sess = storage.create_session(body.username, role)
    _auth_audit("login", body.username, role)
    return {"ok": True, "token": sess["token"], "username": body.username,
            "role": role, "expires_at": sess["expires_at"]}


@router.post("/api/auth/logout")
def api_auth_logout(x_api_token: Optional[str] = Header(default=None)):
    """撤销当前会话 token（只对"登录签发的会话"有效；静态 token 无法在此撤销）。"""
    from sqlpa.business import storage
    ident = _resolve_identity(x_api_token, "")
    removed = bool(x_api_token) and ident.get("source") == "session" \
        and storage.delete_session(x_api_token)
    if removed:
        _auth_audit("logout", ident.get("username") or "", ident.get("role") or "")
    return {"ok": True, "logged_out": bool(removed)}


@router.get("/api/auth/me")
def api_auth_me(x_api_token: Optional[str] = Header(default=None)):
    """当前请求的身份（前端据此显示"登录用户/角色"，并决定是否展示 admin 操作）。"""
    ident = _identify(x_api_token, "")
    return {"username": ident["username"], "role": ident["role"], "source": ident["source"]}
