"""
sqlpa.business.service
======================
业务"混合模式"的统一入口：CLI(run_business) 与 UI(app.py) 共用这套逻辑，保证一致。

分级放行（产品化的核心策略）：
  - 口径内（命中配置指标且组合受支持）→ 权威公式作为【硬约束】喂给 Writer →
    引擎(LLM)生成查询结构 → 校验"公式未被篡改" → 权限 → 只读执行/掩码 → 审计。
    结果标记 certified=True（口径已认证）。
  - 口径外（未命中指标 / 维度组合不受支持）→ 不再生硬拦截，而是走引擎多 Agent
    自由生成，权限/沙箱/掩码照常生效，结果标记 certified=False（未经口径认证），
    由用户自行判断。拦截是实验思维，分级放行才是产品思维。

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


def _engine_run(question: str, db_id: str, schema: Dict, sb, llm, **kw):
    """多 Agent 引擎调度：优先 LangGraph StateGraph（生产版编排），
    未安装 langgraph 时回退 pipeline（确定性等价编排器）。"""
    try:
        from sqlpa.graph.langgraph_graph import run_graph
        return run_graph(sb, schema, llm, question, db_id, **kw)
    except ImportError:
        from sqlpa.graph.pipeline import run_question
        return run_question(question, db_id, schema, sb, llm, **kw)


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
        if llm is None:
            return {"decision": "reject",
                    "reason": "未命中业务口径配置且当前无 LLM 可做兜底自由查询 → 拒绝生成未经认证的结果。"}
        return {"decision": "escalate",
                "reason": "未命中业务口径配置（" + "; ".join(res.reject_reasons) +
                          "）→ 升级到多 Agent 引擎兜底自由生成，结果标注为未经口径认证。"}
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
           hitl_path: Optional[str] = None) -> Dict:
    from sqlpa.business.followup import rewrite
    from sqlpa.business.metric_guard import build_constraint, formula_of, verify_formula
    from sqlpa.business.permissions import check_access, mask_result
    from sqlpa.business.assembler import assemble
    import uuid as _uuid
    query_id = _uuid.uuid4().hex[:12]

    # ---- 多轮追问：把省略式追问改写成语义完整的独立问题 ----
    rewritten, used_ctx = rewrite(question, history or [], llm)
    q = rewritten

    res = match(q, cfg, llm=llm)

    # Supervisor 路由决策：语义层优先，仅在真实缺口上升级/拒绝；reason 供 UI 展示"为什么走这条路"。
    sup = _supervise(res, llm, question)

    # ================= 口径外 → 自由查询（分级放行，结果降级标注） =================
    if not res.matched:
        if llm is None:
            reason = "; ".join(res.reject_reasons) + "（离线模式无 LLM，不支持自由查询）"
            append_audit(AuditRecord(query_id=query_id, username=username, user_role=role, user_input=question,
                                     matched_metric="", is_success=False,
                                     reject_reason=reason, mode="free", certified=False,
                                     supervisor=sup["decision"]))
            return {"query_id": query_id, "ok": False, "matched": False, "certified": False, "mode": "free",
                    "reject": reason, "rewritten_question": q, "used_context": used_ctx,
                    "supervisor": sup}

        schema = _extract_schema(sb, db_path)
        db_id = schema.get("db_id", "business")
        er = _engine_run(q, db_id, schema, sb, llm, gold_sql=None,
                         dialect_hint=_dialect_hint(sb))
        final_sql = er.final_sql
        ok = bool(er.final_valid and er.exec_result.get("ok"))
        cols = er.exec_result.get("columns", [])
        rows = er.exec_result.get("rows", []) if ok else []

        # 护栏：自由查询同样必须过表列权限（传 schema 以解析非限定列名）
        unauth = check_access(role, cfg.permissions, final_sql, schema=schema)
        if unauth:
            reason = "权限: " + "; ".join(unauth)
            append_audit(AuditRecord(query_id=query_id, username=username, user_role=role, user_input=question,
                                     matched_metric="", generated_sql=final_sql,
                                     is_success=False, reject_reason=reason, mode="free", certified=False,
                                     supervisor=sup["decision"]))
            enqueue(q, "", final_sql, reason, role=role)
            return {"query_id": query_id, "ok": False, "matched": False, "certified": False, "mode": "free",
                    "reject": reason, "sql": final_sql,
                    "rewritten_question": q, "used_context": used_ctx,
                    "supervisor": sup}

        if ok:
            # 按列来源掩码（覆盖 AS 别名绕过）
            rows = mask_result(cols, rows, cfg.permissions.get("sensitive_columns", {}),
                               sql=final_sql, perms=cfg.permissions, schema=schema)
        append_audit(AuditRecord(query_id=query_id, username=username, user_role=role, user_input=question,
                                 matched_metric="", generated_sql=final_sql,
                                 is_success=ok, result_rows=len(rows), mode="free", certified=False,
                                 supervisor=sup["decision"]))
        return {"query_id": query_id, "ok": ok, "matched": False, "certified": False, "mode": "free",
                "path": "fallback",       # 口径外 → 多 Agent 兜底（供降级率埋点）
                "reject": "" if ok else (er.exec_result.get("error") or "引擎未能生成有效查询"),
                "source": "引擎多Agent自由生成(未经口径认证)",
                "sql": final_sql, "columns": cols, "rows": rows,
                "agent_trace": er.agent_trace,
                "rewritten_question": q, "used_context": used_ctx,
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
                     time_grain=getattr(res, "time_grain", None))
    trace: List[Dict] = [{"agent": "MetricMatcher", "role": "意图识别",
                          "detail": f"指标={res.metric_key} 维度={res.dims} "
                                    f"过滤={res.filters} 方法={res.method}",
                          "ms": 0}]
    try:
        cq = compile_spec(cfg, spec)
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

    # 护栏1: 公式校验
    issues = verify_formula(final_sql, formula)
    if issues:
        reason = "; ".join(issues)
        append_audit(AuditRecord(query_id=query_id, username=username, user_role=role, user_input=question,
                                 matched_metric=res.metric_key, generated_sql=final_sql,
                                 is_success=False, reject_reason=reason, mode="metric", certified=False,
                                 supervisor=sup["decision"]))
        enqueue(q, res.metric_key, final_sql, reason, role=role)   # HITL: 公式被改 -> 人工
        return {"query_id": query_id, "ok": False, "matched": True, "certified": True, "mode": "metric",
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
        return {"query_id": query_id, "ok": False, "matched": True, "certified": True, "mode": "metric",
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
    append_audit(AuditRecord(query_id=query_id, username=username, user_role=role, user_input=question,
                             matched_metric=res.metric_key, generated_sql=final_sql,
                             is_success=ok, result_rows=len(rows), mode="metric", certified=ok,
                             supervisor=sup["decision"]))
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
             **extra}
