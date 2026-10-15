"""
api.py
======
FastAPI 薄后端：把多智能体取数能力暴露为 REST API（与 Vue3 前端共用同一套
业务服务层，保证口径一致）。

**职责拆分**（原 700+ 行的单文件已按职责拆到 `apiroutes/`，见下）：
  api.py                        app 组装：CORS + include_router（+ /health），并**再导出**共享符号
  apiroutes/deps.py             惰性单例 / 鉴权 / 限速 / 会话记忆 / 请求模型（唯一来源）
  apiroutes/auth.py             POST /api/auth/login、/api/auth/logout、GET /api/auth/me
  apiroutes/query.py            POST /api/query、POST /api/report、GET /api/metrics
  apiroutes/analyze.py          POST /api/analyze、/api/analyze/stream、/api/analyze/report、
                                GET /api/analyze/chart-template
  apiroutes/governance.py       GET /api/governance/conflicts、/api/audit、/api/metrics/history、
                                /api/metrics/lineage、POST /api/metrics/rollback、GET /api/datasources
  apiroutes/hitl.py             GET/POST /api/hitl*、/api/confirmations*
  apiroutes/subscriptions.py    GET/POST/DELETE /api/subscriptions*
  apiroutes/workspace.py        GET /api/history、/api/favorites、/api/feedback（个人工作台）

**对外契约不变**：所有端点路径、请求/响应结构与拆分前完全一致；`import api` 仍可
访问 `app` 以及 `_resolve_role / _redact_audit / _session_memory / _db_path / _llm` 等
（大量测试与工具脚本依赖），故在此显式**再导出**。

鉴权（重要）：
    - 配置 `SQLPA_API_TOKENS="tokenA:admin,tokenB:analyst"` 后，请求必须带
      `X-API-Token` 头，**角色取自 token**；请求体里的 role 仅可用于在自身
      权限范围内"降级"（不能提权）。
    - 未配置 `SQLPA_API_TOKENS` 时（本地/演示/测试），忽略请求体 role，
      一律按**最小权限角色 `SQLPA_DEFAULT_ROLE`（默认 analyst）**执行。
    生产部署应配置 token 或接入企业鉴权。

运行：uvicorn api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 共享依赖层：**唯一来源**（惰性单例/鉴权/限速/会话记忆/模型）
from apiroutes.deps import (AnalyzeIn, HitlDecisionIn, QueryIn, ReportIn,
                            SubscriptionIn, _SESSION_LOCK, _SESSION_MAX,
                            _SESSION_MEMORY, _SESSION_SEEN, _SESSION_TTL_SECONDS,
                            _analyze_run, _bind_asof, _caller_key, _cfg, _db_path,
                            _get_session_lock, _identify, _llm, _rate_limit,
                            _resolve_identity, _resolve_role, _sandbox,
                            _session_guard, _session_memory, _token_map, _token_owner,
                            answer_impl)
from apiroutes.governance import _redact_audit
from apiroutes import analyze as _r_analyze
from apiroutes import auth as _r_auth
from apiroutes import governance as _r_governance
from apiroutes import hitl as _r_hitl
from apiroutes import query as _r_query
from apiroutes import subscriptions as _r_subscriptions
from apiroutes import workspace as _r_workspace

app = FastAPI(title="多智能体 Text-to-SQL 智能取数 API", version="1.0.0")
# CORS：默认只放行本地演示来源（Vite dev 5173 / 容器 web 8080），生产用
# SQLPA_CORS_ORIGINS="https://a.com,https://b.com" 覆盖。此前硬编码 allow_origins=["*"]，
# 等于任何站点都能从浏览器直接打到取数/归因端点（配合 allow_credentials 还有凭据风险）。
_CORS_ORIGINS = [o.strip() for o in (os.getenv("SQLPA_CORS_ORIGINS") or
                                     "http://localhost:5173,http://127.0.0.1:5173,"
                                     "http://localhost:8080,http://127.0.0.1:8080").split(",")
                 if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_CORS_ORIGINS, allow_methods=["*"],
                   allow_headers=["*"])

# 各职责 router 挂载（路径与拆分前逐一对应）
app.include_router(_r_auth.router)
app.include_router(_r_query.router)
app.include_router(_r_analyze.router)
app.include_router(_r_governance.router)
app.include_router(_r_hitl.router)
app.include_router(_r_subscriptions.router)
app.include_router(_r_workspace.router)


@app.get("/health")
def health():
    """健康检查 + 关键护栏状态（审计是否真在落盘，是这套系统的可信度前提）。"""
    from sqlpa.business.audit import audit_mirror_failures, audit_write_failures
    return {"status": "ok", "llm": _llm() is not None,
            "audit_write_failures": len(audit_write_failures()),
            "audit_mirror_failures": len(audit_mirror_failures())}


def _bootstrap_accounts() -> None:
    """启动时引导管理员账号 —— **仅当设置了 `SQLPA_BOOTSTRAP_ADMIN_PASSWORD`** 才动作。

    为什么要接上：`storage.ensure_default_users()` 此前**只有测试会调**，于是"设了
    引导口令就会自动建管理员账号"这件事实际上从没发生过，登录端点也就没有账号可用。
    未设置该环境变量时直接返回：既不建号，也不往启动日志里塞提示（`ensure_default_users`
    的"安全提示"留给显式调用它的脚本/测试）。
    """
    if not (os.environ.get("SQLPA_BOOTSTRAP_ADMIN_PASSWORD") or "").strip():
        return
    try:
        from sqlpa.business import storage
        storage.ensure_default_users()
    except Exception as e:  # noqa: BLE001 —— 建号失败不应阻止服务启动
        print(f"[警告] 引导管理员账号失败（不影响启动）：{type(e).__name__}: {e}")


_bootstrap_accounts()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
