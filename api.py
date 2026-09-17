"""
api.py
======
FastAPI 薄后端：把多智能体取数能力暴露为 REST API（与 Streamlit 前端共用同一套
业务服务层，保证口径一致）。

端点：
  GET  /health              健康检查
  POST /api/query           自然语言取数（分级放行：口径内认证 / 口径外降级标注）
  GET  /api/metrics         指标中心（指标/维度/过滤模板/别名）
  GET  /api/audit           审计留痕（最近 N 条）
  GET  /api/datasources     已注册数据源（密码掩码）

鉴权（重要）：
  修复前 /api/query 无鉴权，且 `role` 直接取自请求体 —— 任何调用方只要传
  `{"role": "admin"}` 就能绕过全部表列权限。现改为：
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
import sys
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

app = FastAPI(title="多智能体 Text-to-SQL 智能取数 API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


# ---------------- 依赖（惰性单例） ----------------

def _cfg():
    from sqlpa.business.metric_config import load_config
    return load_config()


def _db_path() -> str:
    real = ROOT / "data" / "olist" / "olist.db"
    if real.exists():
        return str(real)
    from build_olist_sample import build
    sample = ROOT / "data" / "olist_sample" / "sample.db"
    if not sample.exists():
        build(sample)
    return str(sample)


def _sandbox():
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox
    # 由 config/settings.yaml 驱动（超时/多语句/额外禁用关键字）
    return SqlSandbox(_db_path(), ExecConfig.from_settings(max_rows=2000))


def _llm():
    """有 Key 用真实 LLM，否则 None（离线：口径内走确定性组装，口径外不支持）。"""
    if os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") \
            or os.environ.get("OPENAI_API_KEY"):
        from sqlpa.llm.openai_compat import OpenAICompatLLM
        return OpenAICompatLLM()
    return None


# ---------------- 模型 ----------------

class QueryIn(BaseModel):
    question: str = Field(..., description="自然语言业务问题")
    role: str = Field("analyst",
                      description="期望角色；仅可在 token 授予的权限范围内使用（不能提权）")
    history: List[dict] = Field(default_factory=list,
                                description="对话历史 [{'question','metric','sql'}]，用于多轮追问")


# ---------------- 鉴权 ----------------

def _token_map() -> Dict[str, str]:
    """解析 SQLPA_API_TOKENS="tokenA:admin,tokenB:analyst" → {token: role}。"""
    raw = (os.getenv("SQLPA_API_TOKENS") or "").strip()
    out: Dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        tok, _, role = part.partition(":")
        tok, role = tok.strip(), role.strip()
        if tok and role:
            out[tok] = role
    return out


def _resolve_role(token: Optional[str], requested: str) -> str:
    """决定本次请求实际使用的角色。

    原则：**角色只能来自凭据，不能来自请求体**。
    - 配了 token：必须提供有效 token；角色取自 token。请求体 role 只能用于降级
      （当它与 token 角色不同时，一律以 token 角色为准，避免提权）。
    - 未配 token：忽略请求体 role，使用最小权限默认角色。
    """
    tokens = _token_map()
    if tokens:
        if not token or token not in tokens:
            raise HTTPException(status_code=401, detail="缺少或无效的 X-API-Token")
        return tokens[token]
    return (os.getenv("SQLPA_DEFAULT_ROLE") or "analyst").strip() or "analyst"


# ---------------- 端点 ----------------

@app.get("/health")
def health():
    return {"status": "ok", "llm": _llm() is not None}


@app.post("/api/query")
def api_query(q: QueryIn, x_api_token: Optional[str] = Header(default=None)):
    from sqlpa.business.service import answer
    # 角色只认凭据，不认请求体（修复越权：原实现直接 role=q.role）
    role = _resolve_role(x_api_token, q.role)
    a = answer(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
               role=role, history=q.history)
    a["effective_role"] = role
    # 控制响应体积
    if isinstance(a.get("rows"), list):
        a["rows"] = [list(r) for r in a["rows"][:200]]
    return a


@app.get("/api/metrics")
def api_metrics():
    cfg = _cfg()
    return {
        "metrics": [{"key": m.key, "name": m.name, "desc": m.desc,
                     "metric_expr": m.metric_expr, "support_dims": m.support_dims,
                     "support_filters": m.support_filters}
                    for m in cfg.metrics.values()],
        "dimensions": [{"key": d.key, "name": d.name, "sql_fragment": d.sql_fragment}
                       for d in cfg.dimensions.values()],
        "filter_templates": list(cfg.filter_templates.keys()),
        "alias": cfg.alias,
    }


@app.get("/api/audit")
def api_audit(limit: int = 50):
    from sqlpa.business.audit import dump_audit
    return dump_audit()[-max(1, min(limit, 500)):]


@app.get("/api/datasources")
def api_datasources():
    from sqlpa.business import datasources as dss
    return [{"id": d["id"], "name": d["name"], "kind": d["kind"],
             "display": dss.display(d)} for d in dss.list_datasources()]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
