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

运行：uvicorn api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
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
    return SqlSandbox(_db_path(), ExecConfig(max_rows=2000))


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
    role: str = Field("analyst", description="analyst / admin")
    history: List[dict] = Field(default_factory=list,
                                description="对话历史 [{'question','metric','sql'}]，用于多轮追问")


# ---------------- 端点 ----------------

@app.get("/health")
def health():
    return {"status": "ok", "llm": _llm() is not None}


@app.post("/api/query")
def api_query(q: QueryIn):
    from sqlpa.business.service import answer
    a = answer(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
               role=q.role, history=q.history)
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
