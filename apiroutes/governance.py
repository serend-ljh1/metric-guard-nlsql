"""apiroutes.governance：治理与元数据类端点（口径冲突/审计/指标历史/血缘/回滚/数据源/指标中心）。"""
from __future__ import annotations

import os
import sqlite3
from typing import Dict, Optional

from fastapi import APIRouter, Header, HTTPException

from .deps import (AliasIn, DimensionIn, MetricIn, _bind_asof, _cfg, _db_path,
                   _identify, _llm, _resolve_role)

router = APIRouter()


def _metric_config_path() -> Optional[str]:
    """指标中心写入目标：`SQLPA_METRIC_CONFIG_PATH` 覆盖，缺省写回权威 business_config.yaml。

    为什么要一个可覆盖路径：写接口会把指标/维度/别名**落回配置文件**。测试若直接打
    真配置，跑一次用例就把仓库的语义层改了 —— 所以提供环境变量指向临时副本。
    生产/演示不设该变量，默认仍写回权威配置。
    """
    return (os.environ.get("SQLPA_METRIC_CONFIG_PATH") or "").strip() or None


def _require_admin(x_api_token: Optional[str]) -> Dict[str, str]:
    """指标中心写操作统一鉴权：非 admin 一律 403（口径是治理资产，不能谁都能改）。"""
    ident = _identify(x_api_token, "analyst")
    if ident["role"] != "admin":
        raise HTTPException(status_code=403, detail="指标中心写操作需要 admin 权限")
    return ident



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
    ident = _identify(x_api_token, "analyst")
    if ident["role"] != "admin":
        raise HTTPException(status_code=403, detail="回滚指标口径需要 admin 权限")
    from sqlpa.business import metric_store
    ok, msg = metric_store.rollback_metric(metric, version_seq,
                                           actor=f"api:{ident['username']}")
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


# ================= 指标中心（CRUD，写操作仅 admin） =================
# 此前 `metric_store` 已具备指标/维度/别名的增删改与**校验**（公式非空、来源须 FROM、
# 维度/过滤须已登记），但**没有任何端点调用它** —— 语义层只能靠人改 YAML，"指标中心"
# 名不副实。这里把它接出来，且所有写操作落回**同一份权威配置**（口径仍只来自配置）。
# 每次变更都会追加一条 `metric_versions`（append-only、可回滚），与本文件的回滚端点闭环。

@router.post("/api/metrics/upsert")
def api_metrics_upsert(body: MetricIn, x_api_token: Optional[str] = Header(default=None)):
    """新增/更新一条指标（仅 admin）：公式/来源经校验后写回配置并记口径版本。"""
    ident = _require_admin(x_api_token)
    from sqlpa.business import metric_store
    metric = body.model_dump()
    editing_key = (metric.pop("editing_key", "") or "").strip()
    ok, msg = metric_store.upsert_metric(metric, editing_key=editing_key,
                                         path=_metric_config_path(),
                                         actor=f"api:{ident['username']}")
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg, "key": metric.get("key")}


@router.delete("/api/metrics/{key}")
def api_metrics_delete(key: str, x_api_token: Optional[str] = Header(default=None)):
    """删除一条指标（仅 admin）。"""
    ident = _require_admin(x_api_token)
    from sqlpa.business import metric_store
    ok, msg = metric_store.delete_metric(key, path=_metric_config_path(),
                                         actor=f"api:{ident['username']}")
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"ok": True, "message": msg}


@router.post("/api/metrics/dimensions/upsert")
def api_dimensions_upsert(body: DimensionIn, x_api_token: Optional[str] = Header(default=None)):
    """新增/更新一个维度（仅 admin）。"""
    _require_admin(x_api_token)
    from sqlpa.business import metric_store
    ok, msg = metric_store.upsert_dimension(body.model_dump(),
                                            editing_key=(body.editing_key or "").strip(),
                                            path=_metric_config_path())
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg, "key": body.key}


@router.delete("/api/metrics/dimensions/{key}")
def api_dimensions_delete(key: str, x_api_token: Optional[str] = Header(default=None)):
    """删除一个维度（仅 admin；被指标引用时拒绝删除）。"""
    _require_admin(x_api_token)
    from sqlpa.business import metric_store
    ok, msg = metric_store.delete_dimension(key, path=_metric_config_path())
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@router.post("/api/metrics/aliases")
def api_alias_upsert(body: AliasIn, x_api_token: Optional[str] = Header(default=None)):
    """新增/更新一条业务别名（仅 admin）。"""
    _require_admin(x_api_token)
    from sqlpa.business import metric_store
    ok, msg = metric_store.upsert_alias(body.name, body.table, body.col,
                                        path=_metric_config_path())
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@router.delete("/api/metrics/aliases/{name}")
def api_alias_delete(name: str, x_api_token: Optional[str] = Header(default=None)):
    """删除一条业务别名（仅 admin）。"""
    _require_admin(x_api_token)
    from sqlpa.business import metric_store
    ok, msg = metric_store.delete_alias(name, path=_metric_config_path())
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"ok": True, "message": msg}
