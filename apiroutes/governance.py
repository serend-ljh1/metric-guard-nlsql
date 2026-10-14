"""apiroutes.governance：治理与元数据类端点（口径冲突/审计/指标历史/血缘/回滚/数据源）。"""
from __future__ import annotations

import sqlite3
from typing import Dict, Optional

from fastapi import APIRouter, Header, HTTPException

from .deps import (_bind_asof, _cfg, _db_path, _llm, _resolve_role)

router = APIRouter()


@router.get("/api/governance/conflicts")
def api_governance_conflicts(run_llm: bool = False):
    """口径冲突检测（治理提示）：四类冲突 + 可选 LLM 解读。

    - **同名不同式 / 同式不同名 / 版本漂移**：纯确定性规则，毫秒级、无需 API Key；
    - **近义不同式**：别名族启发式（原逻辑）；
    - `run_llm=true` 时对每条冲突追加 `ai_analysis`（LLM 只做解释，不改判定）。
    """
    from sqlpa.business import governance
    cfg = _cfg()
    items = governance.detect_conflicts(cfg, llm=_llm() if run_llm else None)
    by_type: Dict[str, int] = {}
    for c in items:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    return {"total": len(items), "by_type": by_type, "conflicts": items}


def _redact_audit(rec: Dict) -> Dict:
    """非管理员可见的审计字段（保留"什么时候、问的哪个指标、成没成"，去掉内容本身）。

    审计要能证明"发生过什么"，但提问原文与生成 SQL 里可能带业务敏感信息，
    不应向任何拿到 analyst 凭据的人无差别开放。
    """
    keep = ("query_id", "user_role", "matched_metric", "is_success", "result_rows",
            "mode", "certified", "created_at", "create_time", "reject_reason")
    out = {k: rec.get(k) for k in keep if k in rec}
    out["user_input"] = "（已脱敏）" if rec.get("user_input") else ""
    return out


@router.get("/api/audit")
def api_audit(limit: int = 50, offset: int = 0,
              x_api_token: Optional[str] = Header(default=None)):
    """审计留痕（最近 N 条，来自**权威 sink** SQLite，可分页）。

    加固点（此前该端点**完全无鉴权**，匿名即可拉走 user_input / generated_sql）：
      - 走与取数同一套 `_resolve_role`（配了 token 就 401）；
      - 非 admin 只返回脱敏后的记录（去 user_input / generated_sql / username）；
      - 读 SQLite 而非 JSONL 镜像：镜像可能缺失或滞后，审计的"权威版本"只能有一个；
      - 分页读取，不再把整个 JSONL 载入内存（防 DoS）。
    """
    role = _resolve_role(x_api_token, "analyst")
    from sqlpa.business import storage
    rows = storage.list_audit(limit=max(1, min(limit, 500)), offset=max(0, offset))
    if role == "admin":
        return rows
    return [_redact_audit(r) for r in rows]


@router.get("/api/metrics/history")
def api_metrics_history(metric: Optional[str] = None, limit: int = 20,
                        x_api_token: Optional[str] = Header(default=None)):
    """指标口径变更历史（append-only）：谁、什么时候、把哪个指标从什么改成了什么。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import metric_store
    if metric:
        return metric_store.metric_history(metric, limit=max(1, min(limit, 200)))
    from sqlpa.business import storage
    return storage.list_metric_versions(limit=max(1, min(limit, 200)))


@router.get("/api/metrics/lineage")
def api_metrics_lineage(metric: str, x_api_token: Optional[str] = Header(default=None)):
    """指标的表/列级血缘（静态解析，非数据库字段级血缘）。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import metric_store
    return metric_store.metric_lineage(_cfg(), metric)


@router.post("/api/metrics/rollback")
def api_metrics_rollback(metric: str, version_seq: int,
                         x_api_token: Optional[str] = Header(default=None)):
    """把指标回滚到历史版本（**仅 admin**，且会记录一次 rollback 版本）。"""
    role = _resolve_role(x_api_token, "analyst")
    if role != "admin":
        raise HTTPException(status_code=403, detail="回滚指标口径需要 admin 权限")
    from sqlpa.business import metric_store
    ok, msg = metric_store.rollback_metric(metric, version_seq, actor=f"api:{role}")
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@router.get("/api/datasources")
def api_datasources(x_api_token: Optional[str] = Header(default=None)):
    """已注册数据源（密码掩码）。与取数同权限：不对外匿名开放主机/库名。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import datasources as dss
    return [{"id": d["id"], "name": d["name"], "kind": d["kind"],
             "display": dss.display(d)} for d in dss.list_datasources()]
