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

import json
import os
import sqlite3
import sys
from datetime import datetime
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


def answer_impl(*args, **kwargs):
    """惰性导入业务服务层（避免 import 期就加载 LLM/数据库依赖）。"""
    from sqlpa.business.service import answer
    return answer(*args, **kwargs)


# ---------------- 依赖（惰性单例） ----------------

def _cfg():
    from sqlpa.business.metric_config import load_config
    return load_config()


def _db_path() -> str:
    """业务库路径：环境变量 SQLPA_DB_PATH > 真实全量库 > 仓库自带样本库。

    这样 `git clone` 之后不下载 66MB 真实数据也能跑通接口与前端演示
    （样本库见 data/sample/olist_sample.db，由 tools/build_olist_sample.py 生成）。
    """
    override = (os.getenv("SQLPA_DB_PATH") or "").strip()
    if override:
        return override
    from sqlpa.config import resolve_db_path
    path, kind = resolve_db_path()
    if kind == "sample":
        # 只提示一次，避免刷屏
        global _DB_KIND_WARNED
        if not _DB_KIND_WARNED:
            _DB_KIND_WARNED = True
            print("[数据] 未找到 data/olist/olist.db，使用仓库自带样本库 "
                  "data/sample/olist_sample.db（按月抽样，数值仅供流程演示）。")
    return path


_DB_KIND_WARNED = False


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
    """健康检查 + 关键护栏状态（审计是否真在落盘，是这套系统的可信度前提）。"""
    from sqlpa.business.audit import audit_mirror_failures, audit_write_failures
    return {"status": "ok", "llm": _llm() is not None,
            "audit_write_failures": len(audit_write_failures()),
            "audit_mirror_failures": len(audit_mirror_failures())}


@app.post("/api/query")
def api_query(q: QueryIn, x_api_token: Optional[str] = Header(default=None)):
    # 角色只认凭据，不认请求体（修复越权：原实现直接 role=q.role）
    role = _resolve_role(x_api_token, q.role)
    a = answer_impl(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
                    role=role, history=q.history)
    a["effective_role"] = role
    # 控制响应体积
    if isinstance(a.get("rows"), list):
        a["rows"] = [list(r) for r in a["rows"][:200]]
    return a


class ReportIn(QueryIn):
    """报告导出请求：沿用查询入参，外加导出格式。"""
    fmt: str = Field("markdown", description="markdown / csv / both")


@app.post("/api/report")
def api_report(q: ReportIn, x_api_token: Optional[str] = Header(default=None)):
    """生成**可交付产物**：带口径说明的 Markdown 报告（可选同时导出 CSV）。

    为什么要有这个端点：取数只把表格显示在屏幕上还是个 demo；真实业务需要
    能贴进周报、能转发复核的产物，且必须自带口径来源（否则数字不可信）。
    """
    from sqlpa.business.deliver import export_report
    role = _resolve_role(x_api_token, q.role)
    a = answer_impl(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
                    role=role, history=q.history)
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


class AnalyzeIn(QueryIn):
    """多 Agent 分析请求：沿用查询入参，可用 alert_threshold_pct 覆盖默认告警阈值。"""
    alert_threshold_pct: float = Field(0.05, description="波动告警阈值（0.05 = 5%")
    session_id: Optional[str] = Field(
        None, description="会话 ID：复用则会话级 Agent 记忆（跨轮续下钻）；缺省为一次性无记忆会话")
    confirm_metric: Optional[str] = Field(
        None, description="口径待确认时用户确认/改选后的指标 key；携带后按该口径确定性执行，跳过确认门")


# 会话级工作记忆：session_id -> {last_drill, metric, ...}
# 加固点（此前是无上限、无过期、且只以**客户端自报**的 session_id 为键的全局 dict）：
#   - 键包含调用者（token 哈希/角色），换一个凭据拿同一个 session_id 读不到别人的记忆；
#   - 有 TTL 与容量上限（LRU 淘汰），不会随会话数无限增长；
#   - 带锁访问，避免同 session 并发写撕裂。
import threading as _threading
import time as _time

_SESSION_MEMORY: Dict[str, Dict] = {}
_SESSION_SEEN: Dict[str, float] = {}
_SESSION_LOCK = _threading.Lock()
_SESSION_TTL_SECONDS = float(os.getenv("SQLPA_SESSION_TTL", "3600") or 3600)
_SESSION_MAX = int(os.getenv("SQLPA_SESSION_MAX", "500") or 500)


def _caller_key(token: Optional[str], role: str) -> str:
    """调用者标识：优先用凭据指纹（不落原文），无凭据时退化为角色。"""
    import hashlib
    if token:
        return hashlib.sha256(token.encode()).hexdigest()[:12]
    return f"role:{role}"


def _session_memory(session_id: Optional[str], caller: str = "role:analyst") -> Dict:
    """取/建会话记忆 dict（可变引用，供 run_analysis 跨轮读写）。"""
    if not session_id:
        return {}
    key = f"{caller}::{session_id}"
    now = _time.time()
    with _SESSION_LOCK:
        # 过期清理
        for k in [k for k, ts in _SESSION_SEEN.items() if now - ts > _SESSION_TTL_SECONDS]:
            _SESSION_SEEN.pop(k, None)
            _SESSION_MEMORY.pop(k, None)
        # 容量淘汰（dict 保序 → 丢最早插入的一批，近似 LRU）
        if key not in _SESSION_MEMORY and len(_SESSION_MEMORY) >= _SESSION_MAX:
            for k in list(_SESSION_SEEN)[:max(1, _SESSION_MAX // 10)]:
                _SESSION_SEEN.pop(k, None)
                _SESSION_MEMORY.pop(k, None)
        _SESSION_SEEN[key] = now
        return _SESSION_MEMORY.setdefault(key, {})


@app.post("/api/analyze")
def api_analyze(q: AnalyzeIn, x_api_token: Optional[str] = Header(default=None)):
    """多 Agent 协作分析（**非流式**聚合返回，供脚本/测试/预览使用）。

    与 /api/analyze/stream 同一套编排，只是把流式事件收集成一个数组返回。
    """
    from sqlpa.analysis.orchestrator import run_analysis
    role = _resolve_role(x_api_token, q.role)
    events: List[Dict] = []

    def _emit(p):
        events.append(p)

    final = run_analysis(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
                         role=role,
                         alert_threshold_pct=q.alert_threshold_pct,
                         emit=_emit,
                         confirm_metric=q.confirm_metric,
                         memory=_session_memory(q.session_id,
                                                _caller_key(x_api_token, role)))
    return {"events": events, "final": final, "effective_role": role}


@app.post("/api/analyze/stream")
def api_analyze_stream(q: AnalyzeIn, x_api_token: Optional[str] = Header(default=None)):
    """多 Agent 协作分析（**SSE 流式**）：逐 Agent 事件推给前端。

    事件格式（SSE data: JSON 一行一条）：
      session_start / agent_start / agent_step / agent_done / done / error
    前端消费后渲染 Agent 执行面板 + 炫的可视化 + 决策闭环。
    """
    from sqlpa.analysis.orchestrator import run_analysis
    from fastapi.responses import StreamingResponse
    import queue, threading

    role = _resolve_role(x_api_token, q.role)
    caller = _caller_key(x_api_token, role)
    qq: "queue.Queue[Dict]" = queue.Queue()

    def _emit(p):
        qq.put(p)

    def _run():
        """跑编排并把**任何异常**翻译成终帧事件。

        此前线程目标没有 try/except：任一节点抛异常就静默死线程，客户端既不收到
        done 也收不到 error，只能干等 300 秒后拿到一句假的"分析超时"。
        """
        try:
            run_analysis(
                question=q.question, cfg=_cfg(), sb=_sandbox(), db_path=_db_path(),
                llm=_llm(), role=role,
                alert_threshold_pct=q.alert_threshold_pct, emit=_emit,
                confirm_metric=q.confirm_metric,
                memory=_session_memory(q.session_id, caller))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            qq.put({"type": "error", "detail": f"分析执行失败: {type(e).__name__}: {e}"})
            qq.put({"type": "done", "payload": {"ok": False,
                                                "error": f"{type(e).__name__}: {e}"}})

    def _stream():
        yield "data: " + json.dumps({"type": "session_start",
                                     "question": q.question}) + "\n\n"
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        while True:
            try:
                p = qq.get(timeout=300)
            except queue.Empty:
                yield "data: " + json.dumps({"type": "error",
                                             "detail": "分析超时"}) + "\n\n"
                break
            yield "data: " + json.dumps(p, ensure_ascii=False) + "\n\n"
            if p.get("type") == "done":
                break

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/analyze/chart-template")
def api_analyze_chart_template():
    """ECharts 可视化模板说明：前端据此渲染归因瀑布/主因占比/下钻树/因子占比。"""
    return {"summary": "归因瀑布(waterfall)/主因占比(share)/下钻树(tree)/因子占比(factors) 见 /api/analyze 返回的 final.chart"}


@app.post("/api/analyze/report")
def api_analyze_report(q: AnalyzeIn, x_api_token: Optional[str] = Header(default=None)):
    """导出「多 Agent 分析」的 Markdown 报告（诊断结论+依据+动作+决策闭环）。"""
    from sqlpa.business.deliver import export_analysis_report
    role = _resolve_role(x_api_token, q.role)
    a = _analyze_run(q, role, _caller_key(x_api_token, role))
    if not a.get("ok"):
        return {"ok": False, "reject": a.get("reject", "分析未完成，无法生成报告")}
    out_dir = Path(os.environ.get("SQLPA_REPORT_DIR") or (ROOT / "data" / "reports"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"analysis-{stamp}.md"
    paths = export_analysis_report(target, q.question, a, username=role)
    return {"ok": True, "report": open(paths["report"], encoding="utf-8").read()}


def _analyze_run(q: AnalyzeIn, role: str, caller: str = "role:analyst") -> Dict:
    """非流式多 Agent 分析（/api/analyze/report 内部复用，避免重复实现）。"""
    from sqlpa.analysis.orchestrator import run_analysis
    return run_analysis(q.question, _cfg(), _sandbox(), _db_path(), _llm(),
                        role=role, alert_threshold_pct=q.alert_threshold_pct,
                        emit=None, memory=_session_memory(q.session_id, caller))


@app.get("/api/audit")
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


@app.get("/api/metrics/history")
def api_metrics_history(metric: Optional[str] = None, limit: int = 20,
                        x_api_token: Optional[str] = Header(default=None)):
    """指标口径变更历史（append-only）：谁、什么时候、把哪个指标从什么改成了什么。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import metric_store
    if metric:
        return metric_store.metric_history(metric, limit=max(1, min(limit, 200)))
    from sqlpa.business import storage
    return storage.list_metric_versions(limit=max(1, min(limit, 200)))


@app.get("/api/metrics/lineage")
def api_metrics_lineage(metric: str, x_api_token: Optional[str] = Header(default=None)):
    """指标的表/列级血缘（静态解析，非数据库字段级血缘）。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import metric_store
    return metric_store.metric_lineage(_cfg(), metric)


@app.post("/api/metrics/rollback")
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


class SubscriptionIn(BaseModel):
    """新增订阅规则（指标 + 阈值 + 负责人 + 投递通道）。"""
    metric: str = Field(..., description="指标 key，如 gmv")
    threshold_pct: float = Field(0.05, description="波动阈值（0.05 = 5%）")
    owner: str = Field("", description="负责人（推送给谁）")
    question: str = Field("", description="订阅的业务问题（留空则用指标名）")
    time_spec: str = Field("本月", description="检查的时间窗语义，如 上月/2018-06")
    dims: List[str] = Field(default_factory=list, description="归因维度")
    channel: str = Field("console", description="投递通道：console / file / webhook")
    webhook: str = Field("", description="channel=webhook 时的回调地址")


@app.get("/api/subscriptions")
def api_list_subscriptions(x_api_token: Optional[str] = Header(default=None)):
    """订阅告警规则列表。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business.deliver import load_subscriptions
    return load_subscriptions()


@app.post("/api/subscriptions")
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


@app.delete("/api/subscriptions/{sub_id}")
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


@app.post("/api/subscriptions/check")
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
    alerts = check_subscriptions(_cfg(), sqlite3.connect(_db_path()))
    need = [a for a in alerts if a.get("status") == "alert"]
    out = {"alerts": alerts, "need_push": len(need), "dry": dry}
    if not dry:
        out["delivery"] = deliver_alerts(need)
    return out


@app.get("/api/datasources")
def api_datasources(x_api_token: Optional[str] = Header(default=None)):
    """已注册数据源（密码掩码）。与取数同权限：不对外匿名开放主机/库名。"""
    _resolve_role(x_api_token, "analyst")
    from sqlpa.business import datasources as dss
    return [{"id": d["id"], "name": d["name"], "kind": d["kind"],
             "display": dss.display(d)} for d in dss.list_datasources()]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
