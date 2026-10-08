"""
sqlpa.business.service
======================
取数入口的统一实现：CLI(`run_business.py`) / REST(`api.py`) / 分析链(`orchestrator.py`)
共用这套逻辑，保证"取数链与分析链"口径、权限、审计一致。

两条分支（**没有第三条**）：
  - **口径内**（命中配置指标、且维度/过滤组合受支持）→ 由 `compiler.compile_spec`
    从配置**确定性编译** SQL（LLM 不在生成路径上）→ 公式结构绑定校验 → 表列/行级权限
    → 只读沙箱执行 → PII 掩码 → 审计。结果 `certified=True`，`path=semantic`。
  - **口径外**（未命中指标 / 组合不受支持 / 句式超出表达力）→ **明确拒绝**并给出可操作原因。
    不降级到"自由 SQL 生成"（该链路已整体删除：失败模式是口径漂移、无法审计、
    错误伪装成合理数字）。拒绝同样进审计，可统计"哪些问法还没被口径覆盖"。

LLM 的触点只有两处：意图识别（`method=llm` 时必须过**口径确认门**才执行）与结论/归因讲解。

多轮追问：传入 history 时先做上下文改写（指代消解/省略补全），再进入识别链路。
"""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

from sqlpa.business import attribution, governance
from sqlpa.business.audit import append_audit, AuditRecord
from sqlpa.business.hitl import enqueue
from sqlpa.business.metric_matcher import match


def _extract_schema(sb, db_path) -> Dict:
    """按沙箱类型提取 schema：外部连接器走方言提取，否则 SQLite。"""
    if getattr(sb, "connector", None) is not None:
        return sb.connector.extract_schema()
    from sqlpa.data.schema_extractor import extract_from_sqlite
    return extract_from_sqlite(db_path).to_dict()


def _dialect_hint(sb) -> str:
    return getattr(getattr(sb, "connector", None), "prompt_hint", "") or ""


def _infer_drill_path(question: str, history: List[Dict], extra: Dict) -> List[Dict]:
    """判断本次是否在"追问上一轮的主因"，并据此构造下钻路径。

    多轮下钻是真实分析的核心形态：「GMV 为什么跌？」→「那 SP 为什么跌？」。
    识别方式是**确定性**的（不依赖 LLM）：
      1) 问题里出现了上一轮主因维度的取值（如州名 SP）；或
      2) 问题是追问语气（那/呢/为什么/继续/下钻/拆），且上一轮给出了下钻建议。
    命中则沿该维度值下钻一层；否则不做任何额外分析。
    """
    prev = None
    for h in reversed(history or []):
        if isinstance(h, dict) and h.get("drill_suggestion"):
            prev = h["drill_suggestion"]
            break
    if prev is None:
        prev = extra.get("drill_suggestion")
    if not prev:
        return []
    q = str(question or "")
    value = str(prev.get("value", ""))
    followup_tone = any(k in q for k in ("那", "呢", "为什么", "为何", "继续", "下钻", "再看", "拆"))
    if value and (value.lower() in q.lower() or followup_tone):
        return [{"dim": prev["dim"], "value": prev["value"]}]
    return []


def _supervise(res, llm, question: str, drill_decision: Optional[Dict] = None) -> Dict:
    """**Supervisor 路由决策**（多 Agent 编排的"最后一块空位"）。

    语义层优先是产品设计约束，因此 Supervisor **不做 50/50 乱选**，只在真实缺口上
    升级/拒绝，并始终给出可展示给用户的"为什么走这条路"。

       decision ∈ {direct, drill, escalate, reject}
         - direct   命中口径配置 → 语义层确定性编译（结果已认证）
         - drill    命中口径 + 归因 Agent 判定需继续下钻/换维/因子分解 → 叠加归因路径
         - escalate 未命中口径且当前有 LLM → 升级到多 Agent 兜底引擎自由生成（未经认证）
         - reject   未命中口径且无 LLM（离线）→ 不生成未经认证的结果，明确拒绝
    有 llm 时归因决策的 reason 已被 LLM 产出（decision_source=llm），此处直接采纳，
    不额外调 LLM、不改变既有分级放行行为，仅新增"路由+理由"这一可观测层。
    """
    if not res.matched:
        return {"decision": "reject",
                "reason": "未命中业务口径配置（" + "; ".join(res.reject_reasons) +
                          "）→ 明确拒绝：本系统为可信归因诊断，仅支持口径内指标，"
                          "不生成未经口径认证的查询结果。可尝试口径内指标或调整维度/时间。"}
    # matched：语义层优先；若归因 Agent 有后续动作，则路由进一步标注为 drill 路径。
    if drill_decision and drill_decision.get("action") not in ("", "none"):
        act = drill_decision.get("action")
        rsn = drill_decision.get("reason") or ""
        return {"decision": "drill",
                "reason": f"命中语义层口径（指标={res.metric_key}）→ 确定性编译；"
                          f"叠加归因决策 action={act}：{rsn}"}
    return {"decision": "direct",
            "reason": f"命中语义层口径（指标={res.metric_key}）→ 编译器确定性编译，结果已认证。"}


def answer(question: str, cfg, sb, db_path, llm, role: str = "analyst",
           history: Optional[List[Dict]] = None, username: str = "",
           hitl_path: Optional[str] = None,
           confirm_id: Optional[str] = None) -> Dict:
    """取数入口。confirm_id 为口径确认记录 id（LLM 推断口径时由上一次调用返回）。"""
    from sqlpa.business.followup import rewrite
    from sqlpa.business.metric_guard import verify_formula
    from sqlpa.business.permissions import check_access, mask_result, row_filter_clauses
    from sqlpa.analysis.orchestrator import _llm_usage_snapshot
    import time as _time
    import uuid as _uuid
    query_id = _uuid.uuid4().hex[:12]
    # 可观测性基线：进入时先取 LLM 累计用量与起始时刻
    _t0 = _time.perf_counter()
    usage_before = _llm_usage_snapshot(llm)

    # ---- 多轮追问：把省略式追问改写成语义完整的独立问题 ----
    rewritten, used_ctx = rewrite(question, history or [], llm)
    q = rewritten

    # ---- 口径确认门（与多 Agent 分析链**同一套**逻辑，杜绝两条治理路径分叉）----
    # 确认记录的读取/校验/逐字还原都在 sqlpa.business.confirm 里，取数链与分析链共用。
    # 这两条分支都是"拒绝/待确认"，supervisor 直接标 reject（此时 sup 尚未计算）。
    from sqlpa.business import confirm as _confirm
    if confirm_id:
        _spec, _confirm_err = _confirm.load(cfg, confirm_id)
        if _confirm_err:
            reason = f"口径确认无效：{_confirm_err}"
            append_audit(AuditRecord(query_id=query_id, username=username, user_role=role,
                                     user_input=question, matched_metric="",
                                     is_success=False, reject_reason=reason, mode="metric",
                                     certified=False, supervisor="reject"))
            return {"query_id": query_id, "ok": False, "matched": False, "certified": False,
                    "mode": "metric", "reject": reason, "rewritten_question": q,
                    "used_context": used_ctx, "supervisor": {"decision": "reject",
                                                             "reason": reason}}
        res = _confirm.res_from_spec(_spec)
    else:
        res = match(q, cfg, llm=llm)
        if res.matched and getattr(res, "method", "") == "llm":
            # LLM 猜出来的口径**不允许直接执行**：登记一条待确认记录并返回确认卡。
            spec = _confirm.proposed_spec(
                res, next((v for t, v in (res.filters or []) if t == "time_range"), None),
                question, cfg=cfg)
            cid = _confirm.persist(spec, actor=username)
            append_audit(AuditRecord(query_id=query_id, username=username, user_role=role,
                                     user_input=question, matched_metric=res.metric_key,
                                     is_success=False, reject_reason="口径待确认（LLM 推断）",
                                     mode="metric", certified=False, supervisor="reject"))
            return {"query_id": query_id, "ok": False, "matched": True, "need_confirm": True,
                    "certified": False, "mode": "metric", "confirm_id": cid,
                    "reject": "口径待确认", "metric": res.metric_key,
                    "metric_name": getattr(res, "metric_name", ""),
                    "proposed": {"metric": spec["metric"], "metric_name": spec["metric_name"],
                                 "dims": spec["dims"], "time_spec": spec["time_spec"],
                                 "method": spec["method"]},
                    "candidates": _confirm.candidates(cfg),
                    "rewritten_question": q, "used_context": used_ctx,
                    "supervisor": {"decision": "reject", "reason": "口径待用户确认"}}

    # Supervisor 路由决策：语义层优先，仅在真实缺口上升级/拒绝；reason 供 UI 展示"为什么走这条路"。
    sup = _supervise(res, llm, question)

    # ================= 口径外 → 明确拒绝（不再自由生成 SQL） =================
    # 产品定位：可信归因诊断系统只生成口径内经认证的结果。
    # 未命中指标时不再升级到取数 SQL 引擎自由生成（已移除），统一拒绝并引导。
    if not res.matched:
        reason = "; ".join(res.reject_reasons) + "（系统仅支持口径内指标的自助归因诊断）"
        append_audit(AuditRecord(query_id=query_id, username=username, user_role=role, user_input=question,
                                 matched_metric="", is_success=False,
                                 reject_reason=reason, mode="metric", certified=False,
                                 supervisor=sup["decision"]))
        return {"query_id": query_id, "ok": False, "matched": False, "certified": False, "mode": "metric",
                "reject": reason, "rewritten_question": q, "used_context": used_ctx,
                "supervisor": sup}

    # ================= 口径内 → 语义层确定性编译（**主路径**）=================
    # 架构说明（本轮反转）：命中语义层时，SQL 由 compiler.py 从配置**确定性编译**，
    # LLM 完全不在 SQL 生成路径上 —— 可审计、零口径漂移、零 token、毫秒级。
    # 修复前：即便命中语义层也要让 LLM 生成 SQL（只是加个公式约束），既慢又可能被改坏。
    from sqlpa.business.compiler import compile_spec, QuerySpec, CompileError
    schema = _extract_schema(sb, db_path)
    db_id = schema.get("db_id", "olist")
    spec = QuerySpec(metric=res.metric_key, dims=list(res.dims or []),
                     filters=list(res.filters or []),
                     having=list(getattr(res, "having", None) or []),
                     time_grain=getattr(res, "time_grain", None))
    trace: List[Dict] = [{"agent": "MetricMatcher", "role": "意图识别",
                          "detail": f"指标={res.metric_key} 维度={res.dims} "
                                    f"过滤={res.filters} 方法={res.method}",
                          "ms": 0}]
    # 行级权限（RLS-lite）：把该角色的行过滤谓词注入编译，谓词无法生效时编译期即拒绝
    row_filters = row_filter_clauses(role, cfg.permissions)
    try:
        cq = compile_spec(cfg, spec, row_filters=row_filters)
    except CompileError as e:
        # 规格不合法 → 明确拒绝（不给用户一个口径不明的数字）
        reason = f"语义层编译失败: {e}"
        append_audit(AuditRecord(query_id=query_id, username=username, user_role=role,
                                 user_input=question, matched_metric=res.metric_key,
                                 is_success=False, reject_reason=reason,
                                 mode="metric", certified=False,
                                 supervisor=sup["decision"]))
        return {"query_id": query_id, "ok": False, "matched": True, "certified": False,
                "mode": "metric", "reject": reason, "metric": res.metric_key,
                "rewritten_question": q, "used_context": used_ctx,
                "compile_error": str(e), "supervisor": sup}

    final_sql = cq.sql
    formula = cq.metric_expr          # 口径来自配置（派生指标则为展开后的表达式）
    r = sb.execute(final_sql)
    ok = bool(r.ok)
    cols = r.columns if r.ok else []
    rows = r.rows if r.ok else []
    source = "语义层确定性编译(口径已认证)"
    agent_trace = trace
    compile_info = cq.to_dict()

    # 护栏1: 公式校验（结构绑定：口径列必须就是配置表达式）
    issues = verify_formula(final_sql, formula, getattr(cq, "metric_key", ""))
    if issues:
        reason = "; ".join(issues)
        append_audit(AuditRecord(query_id=query_id, username=username, user_role=role, user_input=question,
                                 matched_metric=res.metric_key, generated_sql=final_sql,
                                 is_success=False, reject_reason=reason, mode="metric", certified=False,
                                 supervisor=sup["decision"]))
        enqueue(q, res.metric_key, final_sql, reason, role=role)   # HITL: 公式被改 -> 人工
        return {"query_id": query_id, "ok": False, "matched": True, "certified": False, "mode": "metric",
                "reject": reason, "metric": res.metric_key,
                "sql": final_sql, "source": source,
                "rewritten_question": q, "used_context": used_ctx,
                "supervisor": sup}
    # 护栏2: 表列权限（传 schema 以解析非限定列名）
    unauth = check_access(role, cfg.permissions, final_sql, schema=schema)
    if unauth:
        reason = "权限: " + "; ".join(unauth)
        append_audit(AuditRecord(query_id=query_id, username=username, user_role=role, user_input=question,
                                 matched_metric=res.metric_key, generated_sql=final_sql,
                                 is_success=False, reject_reason=reason, mode="metric", certified=False,
                                 supervisor=sup["decision"]))
        enqueue(q, res.metric_key, final_sql, reason, role=role)   # HITL: 越权 -> 人工
        return {"query_id": query_id, "ok": False, "matched": True, "certified": False, "mode": "metric",
                "reject": reason, "metric": res.metric_key,
                "sql": final_sql, "source": source,
                "rewritten_question": q, "used_context": used_ctx,
                "supervisor": sup}

    if ok:
        # 按列来源掩码（覆盖 AS 别名绕过）
        rows = mask_result(cols, rows, cfg.permissions.get("sensitive_columns", {}),
                           sql=final_sql, perms=cfg.permissions, schema=schema)
    else:
        enqueue(q, res.metric_key, final_sql, "引擎执行未通过", role=role)  # HITL: 引擎失败 -> 人工

    # ---- 治理面增强：口径解释 + 异常归因 + HITL 闭环 ----
    # 把 LLM 从"只写/改 SQL"扩展到治理层（LLM 生成归因总结），打通
    # "取数 → 口径可追溯 → 波动归因 → 异常闭环"的端到端链路。
    # 全部 try/except 降级：任何一步失败都不阻塞主取数结果。
    extra: Dict = {}
    if ok:
        try:
            extra["metric_explain"] = governance.explain_metric(cfg, res.metric_key)
            time_spec = next((v for ft, v in res.filters if ft == "time_range"), None)
            if time_spec:
                _db = sqlite3.connect(db_path)
                try:
                    att = attribution.analyze(cfg, _db, res.metric_key,
                                              current_spec=time_spec, dims=res.dims)
                    extra["attribution"] = att
                    extra["attribution_summary"] = attribution.summarize(att, llm)
                    # 多轮下钻：把"下一步可往哪拆"一并给出，供对话层直接追问
                    sug = attribution.next_drill_suggestion(att)
                    if sug:
                        extra["drill_suggestion"] = sug
                    # 若本次是追问 → 由归因 Agent 决策"下一步拆哪"，代码只执行计算。
                    # （decide_drill 返回 action；失败/无 LLM 时内部回退确定性规则，绝不抛异常）
                    decision = attribution.decide_drill(att, question, llm)
                    extra["drill_decision"] = decision   # 决策理由+来源，供 UI/评测埋点
                    action = decision.get("action")
                    if action == "drill" and decision.get("path"):
                        d = attribution.drill(cfg, _db, res.metric_key, current_spec=time_spec,
                                              path=decision["path"])
                        extra["drill"] = d
                        extra["drill_summary"] = attribution.summarize(
                            {**d, "current_total": d.get("path_desc", ""),
                             "previous_total": "", "change": 0.0}, llm) if d.get("ok") else ""
                        extra["drill_reason"] = decision.get("reason")
                    elif action == "switch_dim" and decision.get("dim"):
                        # 贡献分散 → 主动换一个维度再拆（Agent 自主决策）
                        d = attribution.analyze(cfg, _db, res.metric_key, current_spec=time_spec,
                                                previous_spec=None, dims=[decision["dim"]])
                        extra["drill"] = d
                        extra["drill_reason"] = "贡献分散，主动切换维度："
                        extra["drill_reason"] += decision.get("reason") or ""
                    elif action == "factorize":
                        fz = attribution.factorize(cfg, _db, res.metric_key, current_spec=time_spec)
                        if fz.get("ok"):
                            extra["factor_split"] = fz
                    # 注意：若本层已执行下钻，必须把建议**置空**（无进一步贡献时）而不要
                    # 保留上一层，否则会把用户反复导向同一个维度（下钻死循环）。
                    if "drill" in extra:
                        extra["drill_suggestion"] = attribution.next_drill_suggestion(extra["drill"])
                    if att.get("is_abnormal"):
                        # 把 AI 归因总结一并写入工单 ai_note，人工复核时可直接看 AI 分析草稿
                        extra["hitl_id"] = attribution.notify_anomaly(
                            cfg, att, path=hitl_path,
                            summary=extra.get("attribution_summary") or "")
                finally:
                    _db.close()
        except Exception:
            pass

    # 归因决策已得出 → 由 Supervisor 并入"drill"路径（direct 升级为 drill），
    # 让 UI 能展示"走了语义层 + 又自动下钻/分解"。
    sup = _supervise(res, llm, question, extra.get("drill_decision"))
    # 可观测性：逐请求计量 LLM 调用/Token/成本/时延（此前 /api/query 完全不计量）
    from sqlpa.analysis.orchestrator import _llm_usage_snapshot, _observability
    obs = _observability(usage_before, _llm_usage_snapshot(llm), _t0)
    append_audit(AuditRecord(query_id=query_id, username=username, user_role=role, user_input=question,
                             matched_metric=res.metric_key, generated_sql=final_sql,
                             is_success=ok, result_rows=len(rows), mode="metric", certified=ok,
                             supervisor=sup["decision"],
                             llm_calls=obs.get("llm_calls", 0),
                             total_tokens=obs.get("total_tokens", 0),
                             cost=obs.get("cost", 0.0),
                             latency_ms=obs.get("latency_ms", 0.0)))
    # metric_name 取编译产物：派生指标（ratio@/share@）不在 cfg.metrics 里，
    # 修复前这里用 cfg.metrics[res.metric_key].name，派生指标会直接 KeyError。
    return {"query_id": query_id, "ok": ok, "matched": True, "certified": True, "mode": "metric",
             "path": "semantic",           # 语义层确定性编译（主路径，供命中率埋点）
             "metric": res.metric_key,
             "metric_name": cq.metric_name,
             "metric_expr": formula, "dims": res.dims, "source": source,
             "match_method": res.method,   # 意图识别来源(llm/keyword)：红线边界的可观测点
             "compile": compile_info,      # 口径来源/负责人/版本/派生定义，产品上要展示
             "sql": final_sql, "columns": cols, "rows": rows,
             "agent_trace": agent_trace,
             "supervisor": sup,            # 路由决策+理由，UI 展示"为什么走这条路"
             "rewritten_question": q, "used_context": used_ctx,
             "observability": obs,
             "applied_row_filters": row_filters,   # 行级权限确实生效了哪些谓词（可复核）
             **extra}
