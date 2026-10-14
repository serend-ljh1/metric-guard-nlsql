"""apiroutes.query：取数与交付类端点（/api/query、/api/report、/api/metrics）。"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header

from .deps import (ReportIn, QueryIn, ROOT, _bind_asof, _caller_key, _cfg, _db_path,
                   _llm, _rate_limit, _resolve_role, _sandbox, answer_impl)

router = APIRouter()


@router.post("/api/query")
def api_query(q: QueryIn, x_api_token: Optional[str] = Header(default=None)):
    _bind_asof()
    # 角色只认凭据，不认请求体（修复越权：原实现直接 role=q.role）
    role = _resolve_role(x_api_token, q.role)
    _rate_limit(_caller_key(x_api_token, role), "query")
    a = answer_impl(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
                    role=role, history=q.history, confirm_id=q.confirm_id)
    a["effective_role"] = role
    # 控制响应体积
    if isinstance(a.get("rows"), list):
        a["rows"] = [list(r) for r in a["rows"][:200]]
    return a


@router.post("/api/report")
def api_report(q: ReportIn, x_api_token: Optional[str] = Header(default=None)):
    """生成**可交付产物**：带口径说明的 Markdown 报告（可选同时导出 CSV）。

    为什么要有这个端点：取数只把表格显示在屏幕上还是个 demo；真实业务需要
    能贴进周报、能转发复核的产物，且必须自带口径来源（否则数字不可信）。
    """
    from sqlpa.business.deliver import export_report
    _bind_asof()
    role = _resolve_role(x_api_token, q.role)
    _rate_limit(_caller_key(x_api_token, role), "report")
    a = answer_impl(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
                    role=role, history=q.history, confirm_id=q.confirm_id)
    if not a.get("ok"):
        return {"ok": False, "reject": a.get("reject", "取数未成功，无法生成报告")}
    out_dir = Path(os.environ.get("SQLPA_REPORT_DIR") or (ROOT / "data" / "reports"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"report-{stamp}.md"
    paths = export_report(target, q.question, a, username=role,
                          also_csv=(q.fmt in ("csv", "both")))
    return {"ok": True, "path": a.get("path"), "metric": a.get("metric"),
            "report": paths.get("report"), "csv": paths.get("csv"),
            "markdown": open(paths["report"], encoding="utf-8").read()}


@router.get("/api/metrics")
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
