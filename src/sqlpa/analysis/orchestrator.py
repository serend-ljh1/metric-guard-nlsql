"""
sqlpa.analysis.orchestrator
===========================
多 Agent 协作**分析编排**层（档3 的产品主角，独立于取数底座之上）。

目标形态：用户问一句 "GMV 为什么跌" → 系统沿一条多 Agent 链路逐步工作，每一步都被
发射成**流式事件**推到前端，最终输出「诊断结论 + 依据 + 建议动作」，并由决策 Agent
决定该不该告警 / 推给谁 / 写进 HITL 工单。

链路（每个环节都是命名 Agent，可流式展示）：
  RouterAgent     意图分流：取数 vs 分析（分析 = 命中指标口径 + 有波动归因价值）
  MetricMatcher   意图识别：定位指标 / 维度 / 时间范围（复用业务语义层）
  ExecutorAgent   确定性编译 SQL → 只读沙箱取数（口径已认证）
  AttributionAgent 归因拆解：analyze → decide_drill → factorize / drill（可并行）
  ConclusionAgent  把归因链翻成人话「诊断结论 + 依据」（LLM + 确定性兜底）
  DecisionAgent    决策收口：is_abnormal → 是否告警、推给谁、写 HITL 工单（ai_note 带 AI 草稿）

设计约束（对齐既有红线）：
  - 指标公式/维度/过滤全部来自业务语义层配置；SQL 由 compiler **确定性编译**，
    LLM 不碰 SQL 生成路径 —— 归因分析也复用这套确定性算术，可复核。
  - 任一 Agent 失败都降级不阻塞（抛给 emit 的 error 事件继续），保证永远能出结论。
  - 事件全部可 JSON 序列化（供 SSE / 前端直用）。
"""
from __future__ import annotations

import datetime
import time
import uuid
from typing import Callable, Dict, List, Optional, TypedDict

from sqlpa.business import attribution, confirm as _confirm
from sqlpa.business.metric_config import BusinessConfig
from sqlpa.business.metric_matcher import match


# 事件发射器签名：emit(payload_dict)
# 典型 payload：{"type": "agent_start"|"agent_step"|"agent_done"|"done"|"error",
#                "name": Agent名, "role": 职责, "stage": 阶段, "detail": 人话,
#                "payload": {...}, "at": iso时间戳}
Emit = Callable[[Dict], None]


def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------- 可观测性

def _require_significance() -> bool:
    """告警是否要求"通过显著性检验"（config/settings.yaml: pipeline.require_significance）。

    默认开启：单期环比的假阳性主要来自日间波动与月份长度，阈值单独把关不够。
    """
    try:
        from sqlpa.config import get as cfg_get
        return bool(cfg_get("pipeline.require_significance", True))
    except Exception:  # noqa: BLE001
        return True


def _llm_usage_snapshot(llm) -> Optional[Dict]:
    """取 LLM 客户端的累计用量快照（不支持统计的实现返回 None）。"""
    if llm is None:
        return None
    stats = getattr(llm, "stats", None)
    if callable(stats):
        try:
            s = stats() or {}
            return {"usage": dict(s.get("usage") or {}), "cost": float(s.get("cost") or 0.0),
                    "model": s.get("last_model") or ""}
        except Exception:  # noqa: BLE001
            return None
    return None


def _observability(before: Optional[Dict], after: Optional[Dict], t0: float) -> Dict:
    """把"本次分析花了多少"算出来：LLM 调用次数 / Token / 成本 / 端到端时延。

    为什么要：此前生产路径**完全不计量**，成本与延迟只能靠猜；评测里的调用计数又是坏的
    （读错字段恒为 0）。现在每次分析都带 observability，可直接进日志与看板。
    """
    out: Dict = {"latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                 "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                 "total_tokens": 0, "cost": 0.0, "model": ""}
    if not before or not after:
        out["llm_available"] = False
        return out
    out["llm_available"] = True
    for k in ("calls", "prompt_tokens", "completion_tokens", "total_tokens"):
        out["llm_calls" if k == "calls" else k] = int(
            after["usage"].get(k, 0)) - int(before["usage"].get(k, 0))
    out["cost"] = round(float(after.get("cost", 0)) - float(before.get("cost", 0)), 6)
    out["model"] = after.get("model") or ""
    return out


class _Emitter:
    """给事件补上时间戳，保证每个事件都可 JSON 序列化。"""

    def __init__(self, emit: Emit):
        self._emit = emit

    def __call__(self, payload: Dict):
        p = dict(payload)
        p.setdefault("at", _now())
        # 剔除报告里不可 JSON 序列化的对象（一般只有基础类型，防御性兜底）
        self._emit(p)

    def agent_start(self, name: str, role: str, detail: str = ""):
        self({"type": "agent_start", "name": name, "role": role, "detail": detail})

    def agent_step(self, name: str, stage: str, detail: str, **payload):
        self({"type": "agent_step", "name": name, "stage": stage, "detail": detail,
              "payload": payload})

    def agent_done(self, name: str, result: Dict, stage: str = "完成"):
        self({"type": "agent_done", "name": name, "stage": stage, "payload": result})


def _extract_schema(sb, db_path) -> Dict:
    """按沙箱类型提取 schema：外部连接器走方言提取，否则 SQLite。"""
    if getattr(sb, "connector", None) is not None:
        return sb.connector.extract_schema()
    from sqlpa.data.schema_extractor import extract_from_sqlite
    return extract_from_sqlite(db_path).to_dict()


# ---------------------------------------------------------------- 意图分流

def _route(question: str, cfg: BusinessConfig, llm, memory: Optional[Dict] = None) -> Dict:
    """RouterAgent：先把意图分成「取数 / 分析」。

    判定是确定性 + 可解释的：命中业务口径配置 → 才可能做归因分析（分析至少要有
    口径锁定的指标，否则连'基准数'都不存在）；未命中 → 提示先确认口径或走兜底取数。
    分析链是否真正下钻，由后续 AttributionAgent 的波动阈值与 decide_drill 决定。

    会话记忆：追问式问题（"那 SP 呢"）自身往往不含指标/时间词，此时若
    会话记忆里有上轮 metric + time_spec，就用记忆补全口径，实现跨轮续问。
    """
    res = match(question, cfg, llm=llm)
    matched = bool(res.matched)

    # ---- 追问补全：命中会话记忆口径 ----
    resumed_metric = None
    if (not matched) and memory:
        followup_tone = any(k in question for k in ("那", "呢", "为什么", "为何",
                                                    "继续", "下钻", "再看", "拆"))
        if followup_tone and memory.get("metric"):
            # 用记忆里的指标 + 时间范围重建 MatchResult，口径可信，直接接管
            from sqlpa.business.metric_matcher import MatchResult
            resumed_metric = memory.get("metric")
            dims = memory.get("dims") or []
            res = MatchResult(matched=True, metric_key=resumed_metric, dims=dims,
                              filters=[("time_range", memory.get("time_spec"))],
                              method="memory")
            matched = True

    metric = res.metric_key if matched else None
    time_spec = memory.get("time_spec") or (
        next((v for ft, v in res.filters if ft == "time_range") or [],
             None) if matched else None)
    return {
        "route": "analyze" if (matched and time_spec) else "query_only" if matched else "unmatched",
        "matched": matched,
        "resumed": bool(resumed_metric),
        "question": question,      # 结论 Agent 要把用户原话写进 prompt（此前误传了对象类型）
        "metric": metric,
        "metric_name": cfg.metrics[metric].name if (matched and metric in cfg.metrics) else "",
        "dims": list(res.dims or []) if matched else [],
        "time_spec": time_spec,
        "method": res.method if matched else None,
        "reason": (f"命中指标「{metric}」且圈定了时间范围 → 可做波动归因分析"
                   if route_is_analyze(res, time_spec)
                   else "命中口径但未圈定时间范围 → 仅返回取数结果"
                   if matched else "未命中业务口径配置 → 无法做归因分析（缺基准口径）"),
        "res": res,
    }


def route_is_analyze(res, time_spec) -> bool:
    """res.matched 且 time_spec 非空 → 分析链。"""
    return bool(res.matched and time_spec)


# ---------------------------------------------------------------- 编排主入口

class _AnalysisState(TypedDict, total=False):
    """LangGraph 分析图的工作状态：各 Agent 节点把产物写回这几个键。

    每个键独立由对应节点覆盖写回（节点只返回自己产出的键），形成
    Router → Executor → Attribution → Conclusion → Decision 的状态流。
    reject 仅在「口径未命中 / 执行失败」两条条件边上落到终结节点时写入，
    用于提前返回拒绝/兜底结论。
    """
    routing: Dict          # RouterAgent / MetricMatcher 输出
    query_res: Dict        # ExecutorAgent 输出
    attribution_res: Dict  # AttributionAgent 输出
    merged: Dict           # query_res + attribution_res 合并视界
    conclusion: Dict       # ConclusionAgent 输出
    decision: Dict         # DecisionAgent 输出
    reject: Dict           # 条件边上的终结产物（口径未命中 / 执行失败）


def _build_analysis_graph(cfg: BusinessConfig, sb, db_path: str, llm, role: str,
                          hitl_path: Optional[str], alert_threshold_pct: float,
                          dims_override: Optional[List[str]], memory: Dict,
                          _emit: "_Emitter", question: str, username: str = "",
                          confirm_id: Optional[str] = None):
    """用 LangGraph StateGraph 编排 6 个分析 Agent（节点 + 条件边 + 终止节点）。

    每个 Agent 的业务逻辑仍是独立的确定性/LLM 函数（见 `_route` / `_executor` /
    `_attribution_chain` / `_conclusion` / `_decision`），这里只负责把"顺序调度"
    换成图上的**节点 + 条件边**：
      START → RouterAgent ──(命中?)──→ ExecutorAgent ──(ok?)──→ AttributionAgent
                 │                           │                    │
                 └─未命中 Edge→ "unmatched"(拒绝)  └─失败 Edge→ "executor_reject" ─┐
            AttributionAgent → ConclusionAgent → DecisionAgent → END
    条件边让流程真正"分流"而非硬编码 if/return：口径未命中走拒绝终结节点。
    结论由同一个 StateGraph 收敛；各节点沿用既有 `_emit` 事件约定（事件序列不变）。
    """
    from langgraph.graph import END, START, StateGraph

    # 确认门：确认记录在**进入图之前**就从库里取出来并校验（越界/重放在这层拒绝）
    _confirm_spec, _confirm_err = (None, None)
    if confirm_id:
        _confirm_spec, _confirm_err = _confirm.load(cfg, confirm_id)

    # ---- 节点：每个 = 一个命名 Agent（发射同名流式事件，逻辑复用原函数）----

    def _confirmed_routing(spec: Dict):
        """按**落库的确认记录**逐字重建路由（不再用关键词二次解析）。

        旧实现的三个问题：① 只认客户端自报的指标字符串，任何真值都能绕过门；
        ② 维度/时间用 `_keyword_match(question)` 重新推 → 与确认卡展示的可能不一致；
        ③ 确认状态只活在前端内存、不落库、无审计。
        现在 spec 来自 `confirm.load()`（指标/维度/过滤/粒度逐字），method="confirmed"。
        """
        res = _confirm.res_from_spec(spec)
        time_spec = spec.get("time_spec")
        return {"route": "analyze" if time_spec else "query_only", "matched": True,
                "resumed": False, "question": question, "metric": spec["metric"],
                "metric_name": spec.get("metric_name") or spec["metric"],
                "dims": list(spec.get("dims") or []), "time_spec": time_spec,
                "method": "confirmed",
                "reason": f"口径已由用户确认：指标「{spec.get('metric_name') or spec['metric']}」",
                "confirm_id": spec.get("confirm_id"), "res": res}

    def node_router(state):
        _emit.agent_start("RouterAgent", "意图分流", "判断本次提问是取数还是波动归因分析")
        if _confirm_err:
            # 越界即拒绝：confirm_id 无效/已用过/指标已不可编译 → 不走 LLM 兜底，直接拒
            reason = f"口径确认无效：{_confirm_err}"
            _emit.agent_step("RouterAgent", "confirm-invalid", reason)
            routing = {"route": "unmatched", "matched": False, "resumed": False,
                       "question": question, "metric": None, "metric_name": "",
                       "dims": [], "time_spec": None, "method": "rejected",
                       "reason": reason, "res": None}
        elif _confirm_spec:
            # 用户已确认 → 按落库的 spec **逐字**执行（确定性锁定，不再让 LLM 猜）
            routing = _confirmed_routing(_confirm_spec)
        else:
            routing = _route(question, cfg, llm, memory)
        _emit.agent_step("RouterAgent", "match", routing["reason"],
                         metric=routing["metric"], method=routing["method"])
        _emit.agent_done("RouterAgent", {"route": routing["route"],
                                         "reason": routing["reason"]})
        # MetricMatcher 复用同一份语义层 match 结果，仅作意图识别上报
        _emit.agent_start("MetricMatcher", "意图识别",
                          f"指标={routing['metric']} 维度={routing['dims']} 方法={routing['method']}")
        _emit.agent_done("MetricMatcher", {"metric": routing["metric"],
                                           "metric_name": routing["metric_name"],
                                           "dims": routing["dims"],
                                           "time_spec": routing["time_spec"],
                                           "method": routing["method"]})
        return {"routing": routing}

    def route_next(state):
        # 条件边三分：确定性命中（keyword/memory/confirmed）→ 执行器；
        # **LLM 推断的口径一律先过确认门**；未命中 → 拒绝终结边。
        # 注意：这里只看 routing.method，不再看任何客户端传入的参数 ——
        # 旧实现用 `and not confirm_metric`，于是"随便传个真值"就能让 LLM 猜的口径直接执行。
        r = state["routing"]
        if r.get("matched") and r.get("method") == "llm":
            return "confirm"
        return "executor" if r.get("matched") else "unmatched"

    def node_unmatched(state):
        reason = state["routing"]["reason"]
        _emit({"type": "error", "name": "RouterAgent", "detail": reason, "at": _now()})
        return {"reject": {"ok": False, "route": "unmatched", "reject": reason,
                           "metric": None, "conclusion": "无法定位指标口径，未能开展分析。",
                           "evidence": [], "actions": {},
                           "decision": {"alert": False, "reason": reason}}}

    def node_confirm(state):
        """口径确认终结节点（HITL 前置）：LLM 推断的口径不回显确认就直接进昂贵归因，
        是"确定性保底、LLM 兜底、越界即拒绝"的可信闭环节点——先让用户认口径，再算数。

        **落库**：把待确认 spec 登记为一条记录并返回 confirm_id；客户端只能凭 id 确认，
        不能自报指标名。这样确认动作可审计、可被他人接手、也不会因刷新丢失。
        """
        r = state["routing"]
        spec = _confirm.proposed_spec(r.get("res"), r.get("time_spec"), question, cfg=cfg)
        cid = _confirm.persist(spec, actor=username)
        proposed = {"metric": spec["metric"], "metric_name": spec["metric_name"],
                    "dims": spec["dims"], "time_spec": spec["time_spec"],
                    "method": r.get("method")}
        cands = _confirm.candidates(cfg)
        _emit({"type": "confirm_required", "name": "MetricMatcher",
               "detail": (f"系统用 LLM 把问题推断为口径「{spec['metric_name']}」"
                          f"（{spec['metric']}），请确认后再拉取并归因"),
               "confirm_id": cid, "proposed": proposed, "candidates": cands, "at": _now()})
        return {"reject": {"ok": False, "need_confirm": True, "route": "confirm",
                           "reject": "口径待确认", "confirm_id": cid,
                           "metric": spec["metric"], "metric_name": spec["metric_name"],
                           "conclusion": "", "evidence": [], "actions": {},
                           "decision": {"alert": False, "reason": "口径待用户确认"},
                           "proposed": proposed, "candidates": cands}}

    def node_executor(state):
        _emit.agent_start("ExecutorAgent", "确定性取数",
                          f"按语义层口径确定性编译 SQL（指标={state['routing']['metric']}）")
        query_res = _executor(cfg, sb, db_path, state["routing"]["res"],
                              state["routing"], role, question, username)
        # 拒绝路径（口径不支持/越权/执行失败）返回的 dict 没有 metric_name/sql 全字段，
        # 这里必须用 .get：否则"取数失败"会升级成节点内的 KeyError。
        _emit.agent_done("ExecutorAgent", {
            "metric": query_res.get("metric"), "metric_name": query_res.get("metric_name", ""),
            "sql": (query_res.get("sql") or "")[:120], "rows": query_res.get("row_count", 0),
            "compile": query_res.get("compile", {}),
            "ok": query_res.get("ok", False),
        })
        return {"query_res": query_res}

    def executor_next(state):
        # 条件边：确定性取数成功 → 归因；失败 → 拒绝终结边
        return "attribution" if state["query_res"].get("ok") else "executor_reject"

    def node_executor_reject(state):
        _emit({"type": "error", "name": "ExecutorAgent",
               "detail": state["query_res"].get("reject", "取数执行失败"), "at": _now()})
        return {"reject": state["query_res"]}

    def node_attribution(state):
        attribution_res = _attribution_chain(cfg, db_path, state["routing"]["res"],
                                             state["routing"], llm, question, _emit,
                                             hitl_path, alert_threshold_pct,
                                             dims_override, memory)
        return {"attribution_res": attribution_res}

    def node_conclusion(state):
        merged = {**state["query_res"], **state["attribution_res"]}
        conclusion = _conclusion(merged, state["routing"], llm, _emit, cfg)
        return {"merged": merged, "conclusion": conclusion}

    def node_decision(state):
        decision = _decision(state["merged"], state["routing"], llm, cfg, hitl_path,
                             _emit, alert_threshold_pct, state["conclusion"])
        return {"decision": decision}

    # ---- 构图 ----
    graph = StateGraph(_AnalysisState)
    graph.add_node("router", node_router)
    graph.add_node("unmatched", node_unmatched)
    graph.add_node("confirm", node_confirm)
    graph.add_node("executor", node_executor)
    graph.add_node("executor_reject", node_executor_reject)
    graph.add_node("attribution", node_attribution)
    graph.add_node("conclusion", node_conclusion)
    graph.add_node("decision", node_decision)

    graph.add_edge(START, "router")
    graph.add_conditional_edges("router", route_next,
                                {"executor": "executor", "unmatched": "unmatched",
                                 "confirm": "confirm"})
    # 注意：这里**只能**有 executor 的条件边。此前还额外挂了一条无条件边
    # `add_edge("executor", "attribution")`，导致取数失败时 reject 与 attribution
    # 两条路同时走（attribution 随即 KeyError 被吞掉）——图上的分流形同虚设。
    graph.add_conditional_edges("executor", executor_next,
                                {"attribution": "attribution",
                                 "executor_reject": "executor_reject"})
    graph.add_edge("attribution", "conclusion")
    graph.add_edge("conclusion", "decision")
    graph.add_edge("unmatched", END)
    graph.add_edge("confirm", END)
    graph.add_edge("executor_reject", END)
    graph.add_edge("decision", END)
    return graph.compile()


def run_analysis(question: str, cfg: BusinessConfig, sb, db_path: str, llm,
                 role: str = "analyst", username: str = "",
                 hitl_path: Optional[str] = None,
                 emit: Optional[Emit] = None,
                 alert_threshold_pct: float = 0.05,
                 dims_override: Optional[List[str]] = None,
                 memory: Optional[Dict] = None,
                 confirm_id: Optional[str] = None) -> Dict:
    """多 Agent 分析编排（**LangGraph StateGraph**）：节点+条件边+收敛，发射流式事件。

    编排由 `_build_analysis_graph` 编译出的图 `invoke` 驱动，而不是手写 if/return：
    RouterAgent 后接条件边分「命中→执行器 / 未命中→拒绝」；ExecutorAgent 后接
    条件边分「成功→归因 / 失败→拒绝」。6 个 Agent 的业务逻辑全部保留在对应节点内，
    事件序列与最终载荷对外契约不变。

    emit: 事件回调（由 SSE 端点注入）；None 时静默收集，返回最终聚合结果。
    memory: **会话级工作记忆**（可选，可变 dict，由调用方跨轮持有）。本轮各 Agent
      读写该 dict，使下钻建议、主因定位能跨问题续传，实现「那 SP 呢」式多轮下钻。
      ├ 写：last_drill={dim,value,path_desc}、metric、att_change_pct
      └ 读：followup 追问时沿上轮主因续下钻（不再从零开始）
    不传则本轮独立、无记忆（向后兼容）。
    confirm_id: 口径确认门的**落库确认记录 id**（LLM 推断口径时由本函数返回）。
      传入后按记录里的 spec 逐字执行；无效/已用过/指标已下线 → 明确拒绝。用掉即置 confirmed。
    """
    if memory is None:
        memory = {}
    _emit = _Emitter(emit or (lambda p: None))
    aid = uuid.uuid4().hex[:12]

    _emit({"type": "session_start", "analysis_id": aid, "question": question,
           "at": _now()})

    graph = _build_analysis_graph(cfg, sb, db_path, llm, role, hitl_path,
                                  alert_threshold_pct, dims_override, memory,
                                  _emit, question, username, confirm_id)
    # ---- 可观测性：逐请求记录 LLM 调用/Token/成本/时延（此前生产路径完全不计量）----
    t0 = time.perf_counter()
    llm_before = _llm_usage_snapshot(llm)
    state = graph.invoke({"routing": {}})
    obs = _observability(llm_before, _llm_usage_snapshot(llm), t0)

    # 条件边上的终结产物：口径未命中 / 取数失败 / 口径待确认 → 收敛为兜底/确认结果
    if state.get("reject") is not None:
        out = dict(state["reject"])
        out["observability"] = obs
        if out.get("need_confirm"):
            # 口径确认：不套 summarize_final 的结论包装，把 confirm_id/proposed/candidates
            # 原样透出给前端渲染确认卡（确认后带 confirm_id 重新发起同一分析）。
            final = {
                "analysis_id": aid, "question": question,
                "ok": False, "route": "confirm", "reject": out.get("reject"),
                "metric": out.get("metric"), "metric_name": out.get("metric_name", ""),
                "conclusion": "", "evidence": [], "actions": {},
                "decision": out.get("decision", {}),
                "need_confirm": True, "confirm_id": out.get("confirm_id"),
                "proposed": out.get("proposed"),
                "candidates": out.get("candidates", []),
                "observability": obs,
            }
            if emit:
                emit({"type": "done", "analysis_id": aid, "payload": final, "at": _now()})
                emit({"type": "confirm_required", "analysis_id": aid,
                      "confirm_id": out.get("confirm_id"),
                      "proposed": out.get("proposed"), "candidates": out.get("candidates"),
                      "at": _now()})
            return final
        return summarize_final(question, aid, out, _emit)

    # 确认记录用掉即置 confirmed（防重放）：走到这里说明按该记录成功执行了
    if confirm_id:
        _confirm.consume(confirm_id, actor=username)

    merged = state["merged"]
    final = summarize_final(question, aid, {
        **merged, "route": state["routing"]["route"],
        "conclusion": state["conclusion"], "decision": state["decision"],
        "observability": obs,
    }, _emit, emit_done=False)
    _emit({"type": "done", "analysis_id": aid,
           "conclusion": final["conclusion"],
           "decision": final["decision"],
           "chart": final.get("chart", {}), "at": _now()})
    return final


# ---------------------------------------------------------------- 执行器

def _executor(cfg: BusinessConfig, sb, db_path: str, res, routing: Dict,
              role: str, question: str, username: str = "") -> Dict:
    """ExecutorAgent：沿用 service.answer 语义层主路径（确定性编译，口径已认证）。

    与 service.answer 对齐的**两道治理**（此前分析链整条缺失）：
      1. 执行前 check_access —— 分析链不能成为绕过表列权限的第二入口；
      2. 执行后 append_audit —— 分析链同样要留痕（否则"审计覆盖率 100%"是假的）。
    role/username 曾经是死参数：端点上解析了角色、传进来却被忽略。
    """
    from sqlpa.business.compiler import compile_spec, QuerySpec, CompileError
    from sqlpa.business.permissions import check_access, mask_result, row_filter_clauses
    from sqlpa.business.audit import AuditRecord, append_audit
    schema = _extract_schema(sb, db_path)
    spec = QuerySpec(metric=routing["metric"], dims=list(res.dims or []),
                     filters=list(res.filters or []),
                     having=list(getattr(res, "having", None) or []),
                     time_grain=getattr(res, "time_grain", None))
    # 行级权限：分析链与取数链走同一套（否则分析入口就是绕过 RLS 的第二条路）
    row_filters = row_filter_clauses(role, cfg.permissions)
    try:
        cq = compile_spec(cfg, spec, row_filters=row_filters)
    except CompileError as e:
        reason = f"语义层编译失败: {e}"
        return {"ok": False, "metric": routing["metric"], "reject": reason,
                "conclusion": f"该问题当前无法用已认证口径回答：{reason}",
                "route": "analyze", "certified": False}
    final_sql = cq.sql
    # ---- 护栏1：表列权限（执行之前，与 service.answer 同序）----
    unauth = check_access(role, cfg.permissions, final_sql, schema=schema)
    if unauth:
        reason = "权限: " + "; ".join(unauth)
        append_audit(AuditRecord(username=username, user_role=role, user_input=question,
                                 matched_metric=routing["metric"], generated_sql=final_sql,
                                 is_success=False, reject_reason=reason,
                                 mode="metric", certified=False, supervisor="analyze"))
        return {"ok": False, "metric": routing["metric"], "sql": final_sql,
                "reject": reason, "route": "analyze", "certified": False,
                "conclusion": f"本次分析未执行：{reason}"}
    r = sb.execute(final_sql)
    if not r.ok:
        reason = r.error or "执行失败"
        append_audit(AuditRecord(username=username, user_role=role, user_input=question,
                                 matched_metric=routing["metric"], generated_sql=final_sql,
                                 is_success=False, reject_reason=reason,
                                 mode="metric", certified=False, supervisor="analyze"))
        return {"ok": False, "metric": routing["metric"], "sql": final_sql,
                "reject": reason, "route": "analyze", "certified": False,
                "conclusion": f"本次分析未执行：{reason}"}
    cols = r.columns or []
    rows = r.rows or []
    rows = mask_result(cols, rows, cfg.permissions.get("sensitive_columns", {}),
                       sql=final_sql, perms=cfg.permissions, schema=schema)
    # ---- 护栏2：审计留痕（成功路径）----
    append_audit(AuditRecord(username=username, user_role=role, user_input=question,
                             matched_metric=routing["metric"], generated_sql=final_sql,
                             is_success=True, result_rows=len(rows),
                             mode="metric", certified=True, supervisor="analyze"))
    return {
        "ok": True, "metric": routing["metric"], "metric_name": routing["metric_name"],
        "sql": final_sql, "columns": cols, "rows": rows, "row_count": len(rows),
        "compile": cq.to_dict(), "certified": True, "route": routing["route"],
        "supervisor": {"decision": "analyze",
                       "reason": "命中语义层口径 → 确定性编译取数，叠加多 Agent 归因分析。"},
    }


# ---------------------------------------------------------------- 归因链

def _attribution_chain(cfg: BusinessConfig, db_path: str, res, routing: Dict, llm,
                       question: str, _emit: "_Emitter", hitl_path,
                       alert_threshold_pct: float,
                       dims_override: Optional[List[str]],
                       memory: Optional[Dict] = None) -> Dict:
    """AttributionAgent：analyze → decide_drill → factorize / drill，产出归因/下钻/因子。

    会话记忆：若本轮是「那 SP 呢 / 继续下钻」式追问且上轮存了 last_drill，
    则**沿上轮主因续下钻**，把定位过程接起来；否则从零开始分析。
    """
    _emit.agent_start("AttributionAgent", "波动归因",
                      f"对指标「{routing['metric_name']}」做按维度拆解与主因定位")
    time_spec = routing["time_spec"]
    if not time_spec:
        _emit.agent_done("AttributionAgent", {"note": "未圈定时间范围，跳过归因"})
        return {"attribution": None, "drill": None, "factor_split": None,
                "drill_decision": {"action": "none", "reason": "无时间范围"}}

    import sqlite3
    _db = sqlite3.connect(db_path)
    out: Dict = {}
    try:
        requested = [d for d in (dims_override or routing["dims"])
                     if d in cfg.dimensions]
        # 归因维度兜底：用户只问"为什么跌"（未点名维度）时 RouterAgent 给空 dims，
        # 若不为事件指定维度，attribution.analyze 就不拆维度、top_contributors 恒空。
        # 此时默认按该指标支持、且具有归因价值的业务维度拆解
        # （排除时间 dt，及已被口径过滤框死的 status），保证 ANY Question 都有真实归因。
        dims = requested
        if not requested:
            m_cfg = cfg.metrics.get(routing["metric"])
            if m_cfg is not None:
                dims = [d for d in m_cfg.support_dims
                        if d in cfg.dimensions and d not in ("dt", "status")][:3]
        att = attribution.analyze(cfg, _db, routing["metric"], current_spec=time_spec,
                                  dims=dims, threshold_pct=alert_threshold_pct)
        out["attribution"] = att
        for i, c in enumerate((att.get("top_contributors") or [])[:4]):
            _emit.agent_step("AttributionAgent", "analyze",
                             c.get("desc", "") + (f"（占波动 {abs(c.get('pct_of_change', 0)) * 100:.0f}%）"
                                                   if c.get("pct_of_change") else ""),
                             dim=c.get("dim"), key=c.get("key"), delta=c.get("delta"))
        _emit.agent_step("AttributionAgent", "abnormal",
                         f"当期 {att.get('current_total')} vs 上期 {att.get('previous_total')}，"
                         f"波动 {att.get('change_pct', 0) * 100:+.1f}%"
                         f"（{'异常' if att.get('is_abnormal') else '未超阈值'}）",
                         is_abnormal=att.get("is_abnormal", False))

        # 决策下一层该拆哪
        decision = attribution.decide_drill(att, question, llm)
        action = decision.get("action")

        # ---- 会话记忆：追问续下钻 ----
        # 判断是否为「那 SP 呢 / 继续下钻 / 再看某维」式追问（确定性，不依赖 LLM）
        followup_tone = any(k in question for k in ("那", "呢", "为什么", "为何",
                                                    "继续", "下钻", "再看", "拆"))
        last = memory.get("last_drill") if memory else None
        resumed = False
        if followup_tone and last and last.get("metric") == routing.get("metric"):
            # 沿上轮主因续钻：即便本轮 analyze 判 none，也强制继续下钻那一个主因
            action = "drill"
            decision = {"action": "drill", "reason": f"追问续钻上轮主因「{last.get('dim')}·{last.get('value')}」",
                        "decision_source": "memory", "path": last.get("path")}
            resumed = True
        out["drill_decision"] = decision
        _emit.agent_step("AttributionAgent", "decide",
                         f"下一步动作={action}{'（会话记忆续钻）' if resumed else ''}"
                         f"（来源={decision.get('decision_source')}）：{decision.get('reason','')}",
                         action=action, decision_source=decision.get("decision_source"))

        if att.get("is_abnormal") or resumed:
            if action == "factorize":
                fz = attribution.factorize(cfg, _db, routing["metric"], current_spec=time_spec)
                if fz.get("ok"):
                    out["factor_split"] = fz
                    for f in (fz.get("factors") or [])[:2]:
                        _emit.agent_step("AttributionAgent", "factorize",
                                         f"{f.get('label')} 变化 {f.get('change'):+.2f}，"
                                         f"对总变动贡献 {f.get('share', 0) * 100:+.1f}%",
                                         factor=f.get("label"), share=f.get("share"))
            elif action in ("drill", "switch_dim") and decision.get("path") or (decision.get("dim")):
                path = decision.get("path")
                if not path and decision.get("dim"):
                    # switch_dim 需重新按新维度 analyze 后走 path
                    path = [{"dim": decision["dim"],
                             "value": (att.get("top_contributors") or [{}])[0].get("key", "")}]
                d = attribution.drill(cfg, _db, routing["metric"], current_spec=time_spec,
                                      path=path)
                if d.get("ok"):
                    out["drill"] = d
                    for i, c in enumerate((d.get("top_contributors") or [])[:3]):
                        _emit.agent_step("AttributionAgent", "drill", c.get("desc", ""),
                                         path=d.get("path_desc", ""))
        # ---- 写回会话记忆（最近一次主因建议），供下一轮追问续钻 ----
        if memory is not None:
            sugg = attribution.next_drill_suggestion(out.get("drill") or att)
            if sugg and sugg.get("dim") and sugg.get("value"):
                memory["last_drill"] = {
                    "dim": sugg["dim"], "value": sugg["value"],
                    "path": [{  # 存下「上轮已拆」的维路径，续钻时叠在其上
                        "dim": sugg["dim"], "value": sugg["value"]}],
                    "metric": routing.get("metric"), "path_desc": f"{sugg['dim']}·{sugg['value']}",
                }
            memory["metric"] = routing.get("metric")
            memory["time_spec"] = time_spec
            memory["dims"] = list(routing.get("dims") or [])
            memory["att_change_pct"] = att.get("change_pct", 0)
        _emit.agent_done("AttributionAgent", {
            "is_abnormal": att.get("is_abnormal", False),
            "change_pct": att.get("change_pct", 0),
            "top_dims": [c.get("dim") for c in (att.get("top_contributors") or [])[:3]],
            "action": decision.get("action"),
        })
    except Exception as e:  # noqa: BLE001 —— 归因失败不阻塞结论
        _emit.agent_done("AttributionAgent", {"error": str(e)[:120]})
    finally:
        _db.close()
    return out


# ---------------------------------------------------------------- 结论 Agent

def _verify_references(att: Dict, fz: Optional[Dict], drill: Optional[Dict]) -> Dict:
    """结论引用校验（确定性、可复核）——"事实-证据"握手。

    结论 Agent 只能引用 evidence 里的值，因此必须保证这些证据值**彼此自洽**：
      1. 波动百分比 change_pct 与（当期-上期）/上期 一致；
      2. 主因维度贡献占比 pct_of_change 落在 (0, 1] 区间；
      3. 因子分解的 share 合计 ≈ 1（守恒）；
      4. 下钻链有实际定位结果。
    任一不自洽 → verified=False，结论标记为"未通过引用校验"，把 LLM 幻觉挡在结论层外。
    这是确定性算术校验，不经 LLM，零 token、可测试。
    """
    checks: List[str] = []
    ok = True

    if att and att.get("ok") is not False:
        cur = att.get("current_total")
        pre = att.get("previous_total")
        chg = att.get("change_pct")
        if cur is not None and pre not in (None, 0):
            expected = (cur - pre) / pre
            # 容差 0.1%：change_pct 可能被存储时四舍五入（如 -0.4667），
            # 但真实错（如把波动写成 +50%）仍会被判出。
            if chg is not None and abs(chg - expected) > 1e-3:
                ok = False
                checks.append(f"波动不自洽：change_pct={chg} 与 (当期-上期)/上期={expected:.4f} 不一致")
            else:
                v = chg if chg is not None else expected
                checks.append(f"基准波动自洽：{cur} vs {pre} → {v * 100:+.1f}%")
        # 主因贡献占比 pct_of_change 可为 >100%（多维度正负抵消时被放大），
        # 语义非"必须∈(0,1]"，故不设硬判，仅记录来源以体现"每个结论数字都在证据里"。
        if att.get("top_contributors"):
            checks.append(f"主因维度 {len(att['top_contributors'])} 条均有来源")

    if fz and fz.get("factors"):
        shares = [f.get("share") for f in fz["factors"] if f.get("share") is not None]
        if shares:
            s = sum(shares)
            if abs(s - 1.0) > 0.05:
                ok = False
                checks.append(f"因子分解 share 不守恒：合计={s:.3f}")
            else:
                checks.append(f"因子分解守恒：share 合计≈{s:.2f}")

    if drill and not drill.get("top_contributors") and not drill.get("path_desc"):
        ok = False
        checks.append("下钻链无定位结果")

    return {"verified": ok, "checks": checks}


def _conclusion(merged: Dict, routing: Dict, llm, _emit: "_Emitter",
                cfg: Optional[BusinessConfig] = None) -> Dict:
    """ConclusionAgent：把归因/因子/下钻链翻成人话「诊断结论 + 依据」。

    LLM 生成自然语言结论；无 LLM 或失败时用确定性拼接兜底（可复核）。
    """
    _emit.agent_start("ConclusionAgent", "诊断结论",
                      "把归因数据翻译成业务人员能看懂的结论 + 建议动作")
    att = merged.get("attribution") or {}
    fz = merged.get("factor_split")
    drill = merged.get("drill")
    metric_name = routing["metric_name"] or routing.get("metric", "")

    # ---------- 依据（结构化证据，给前端渲染 + 给 LLM 当上下文） ----------
    evidence: List[Dict] = []
    # ---------- "分析没做成" ≠ "波动正常" ----------
    # 归因查询失败（当期/上期取不到数）时必须单独报"数据不足"，否则会退化成
    # "波动 0%、正常波动、无需干预"——把一次失败伪装成一个结论，是最危险的一类错。
    analysis_failed = bool(att) and att.get("ok") is False
    insufficient_reason = str(att.get("reason") or "当期或上期数据不足")
    if analysis_failed:
        evidence.append({"type": "数据不足",
                         "detail": f"归因未取得可比较的当期/上期数据：{insufficient_reason}"})
    if att and not analysis_failed:
        evidence.append({
            "type": "基准波动",
            "detail": (f"指标「{metric_name}」当期 {att.get('current_total')}，"
                       f"上期 {att.get('previous_total')}，波动 {att.get('change_pct', 0) * 100:+.1f}%"),
        })
    for c in (att.get("top_contributors") or [])[:3]:
        evidence.append({"type": "主因维度",
                         "detail": (c.get("desc", "") +
                                    (f"（占波动 {abs(c.get('pct_of_change', 0)) * 100:.0f}%）"
                                     if c.get("pct_of_change") else ""))})
    # 守恒自检失败时把原因作为独立依据推给用户：比率/均值类指标不做"占波动"表述，
    # 否则报告与告警里会出现数学上不成立的占比。
    if att.get("contribution_note"):
        evidence.append({"type": "口径提示", "detail": str(att["contribution_note"])})
    # 比率类指标的 rate/mix 分解（已通过"重建总值"自校验才可能出现在这里）
    for dim, dec in (att.get("ratio_decomposition") or {}).items():
        if not dec.get("valid"):
            continue
        dim_name = (cfg.dimensions[dim].name if (cfg and dim in cfg.dimensions) else dim)
        evidence.append({
            "type": "量价分解",
            "detail": (f"按「{dim_name}」分解：比率/价格效应 {dec['rate_effect']:+.4g}、"
                       f"结构(mix)效应 {dec['mix_effect']:+.4g}、交互 {dec['interaction']:+.4g}"
                       f"（合计 = 总变化 {dec['change']:+.4g}）"),
        })
    # 月份天数差异提示：让"是不是月长造成的假波动"直接出现在依据里
    if (att.get("calendar") or {}).get("note"):
        evidence.append({"type": "日历提示", "detail": str(att["calendar"]["note"])})
    if fz:
        for f in (fz.get("factors") or [])[:2]:
            evidence.append({"type": "因子分解",
                             "detail": f"{f.get('label')} 变化 {f.get('change'):+.2f}，"
                                       f"贡献 {f.get('share', 0) * 100:+.1f}%"}),
    if drill:
        for c in (drill.get("top_contributors") or [])[:2]:
            evidence.append({"type": "下钻定位",
                             "detail": f"[{drill.get('path_desc', '')}] {c.get('desc', '')}"})

    # ---------- 兜底结论（确定性，永远有输出） ----------
    def _fallback() -> str:
        if analysis_failed:
            return (f"数据不足，暂时无法给出「{metric_name}」的诊断结论："
                    f"{insufficient_reason}。请确认时间范围与数据覆盖后再试。")
        if not att:
            return f"未得到可用的波动归因结果，暂无诊断结论。"
        if not att.get("is_abnormal"):
            return (f"指标「{metric_name}」当期 {att.get('current_total')}、"
                    f"上期 {att.get('previous_total')}，波动 {att.get('change_pct', 0) * 100:+.1f}%，"
                    f"未超过告警阈值，属于正常波动，无需干预。")
        parts = [f"「{metric_name}」出现明显波动（{att.get('change_pct', 0) * 100:+.1f}%）"]
        for c in (att.get("top_contributors") or [])[:3]:
            parts.append(c.get("desc", ""))
        return "。主因来自：\n" + "\n".join(parts) + "。"

    conclusion_text = _fallback()
    # ---------- 建议动作（结构化的，给决策 Agent 用） ----------
    actions: Dict = {}
    if analysis_failed:
        # 置信度显式三态：confirmed / normal / insufficient
        actions["confidence"] = "insufficient"
        actions["is_abnormal"] = None
        actions["action"] = "先补齐数据（确认时间范围与数据覆盖），本轮不下结论、不告警。"
    elif att and att.get("is_abnormal"):
        actions["confidence"] = "confirmed"
        top = (att.get("top_contributors") or [])
        actions["is_abnormal"] = True
        actions["action"] = ("建议推送给指标负责人复核，并优先核查主因维度 "
                             + (f"「{top[0].get('key')}」" if top else "") + "。")
    else:
        actions["confidence"] = "normal"
        actions["is_abnormal"] = bool(att and att.get("is_abnormal"))
        actions["action"] = "本次波动在正常范围，无需额外动作。"

    # ---------- 结论引用校验（"事实-证据"握手）：证据自洽才允许 LLM 生成 ----------
    trace = _verify_references(att, fz, drill)

    # 数据不足时不调 LLM：没有依据可依据，让它"写一段话"只会产生幻觉结论
    # 校验不通过时不调 LLM：自由文本会放大证据出的错，改用确定性拼接并如实标出来
    if llm is not None and trace["verified"] and not analysis_failed:
        prompt = (
            "你是数据分析结论 Agent。用户问了业务问题，系统已完成归因拆解。"
            "请把以下结构化依据写成一段**业务人员能看懂的诊断结论**（2-4 句）："
            "先给结论走向（涨/跌/正常），再给主因证据，最后给一句建议动作。"
            "**只能使用下面给出的依据，不得引入依据中没有的数字或维度。**"
            "不要提 SQL、不要讲口径算法。\n"
            + "依据：\n" + "\n".join(f"- {e['type']}: {e['detail']}" for e in evidence)
            + f"\n\n用户问题：{routing.get('question') or ''}"
            + "\n请直接输出结论文本。"
        )
        try:
            text = llm.complete(prompt).strip()
            if text:
                conclusion_text = text
        except Exception:  # noqa: BLE001
            pass

    result = {"text": conclusion_text, "evidence": evidence, "actions": actions,
              "traceability": trace}
    for e in evidence:
        _emit.agent_step("ConclusionAgent", "evidence", e["detail"], type=e["type"])
    _emit.agent_done("ConclusionAgent",
                    {"conclusion": conclusion_text, "actions": actions,
                     "references_verified": trace["verified"]})
    return result


# ---------------------------------------------------------------- 决策 Agent

def _decision(merged: Dict, routing: Dict, llm, cfg: BusinessConfig, hitl_path,
              _emit: "_Emitter", alert_threshold_pct: float, conclusion: Dict) -> Dict:
    """DecisionAgent：决策收口 —— 该不该告警、推给谁、是否写 HITL。

    判据（确定性 + 可配置）：attribution.is_abnormal（阈值可配）→ 告警；
    负责人取指标 owner（来自业务语义层配置）；写请求交给 hitl.enqueue（ai_note 带 AI 草稿）。
    """
    _emit.agent_start("DecisionAgent", "决策收口",
                      "判断是否告警、推给谁，并把结论写入 HITL 工单闭环")
    att = merged.get("attribution") or {}
    # 三态决策：异常→告警；正常→不告警；**分析没做成→既不告警也不声称正常**。
    # 此前只分两态，导致"归因失败"被归入"正常波动、无需告警"，把失败说成结论。
    inconclusive = bool(att) and att.get("ok") is False
    is_abnormal = bool(att and att.get("is_abnormal"))
    metric = routing.get("metric")
    owner = ""
    if metric in cfg.metrics:
        owner = cfg.metrics[metric].owner
    hitl_id = None

    # ---- 统计门控（零 token、确定性）：阈值过了但没过显著性检验 → 视为噪声，不告警 ----
    # 这是本系统"减少假阳性"的核心开关：单期环比里几个百分点的差异常常只是日间波动。
    sig = att.get("significance") or {}
    require_sig = _require_significance()
    noise_suppressed = bool(is_abnormal and require_sig and sig.get("tested")
                            and not sig.get("is_significant"))
    if noise_suppressed:
        is_abnormal = False

    if inconclusive:
        _emit.agent_step("DecisionAgent", "inconclusive",
                         f"分析未取得可比较数据 → 不下结论、不告警：{att.get('reason', '')}")
    elif noise_suppressed:
        _emit.agent_step(
            "DecisionAgent", "suppressed",
            f"波动 {att.get('change_pct', 0) * 100:+.1f}% 超过阈值，但未通过显著性检验"
            f"（z={sig.get('z')}, p={sig.get('p_value')}）→ 判定为日间波动噪声，"
            f"本轮不告警、不建工单")
    elif is_abnormal:
        # 复用归因的异常入队（ai_note 带决策Agent生成的 AI 分析草稿），写进闭环
        try:
            from sqlpa.business.attribution import notify_anomaly
            summary = (conclusion.get("text") if isinstance(conclusion, dict)
                       else str(conclusion)) or ""
            hitl_id = notify_anomaly(cfg, att, path=hitl_path, summary=summary)
        except Exception as e:  # noqa: BLE001
            _emit.agent_step("DecisionAgent", "hitl", f"写 HITL 失败：{e}")
    elif not is_abnormal:
        _emit.agent_step("DecisionAgent", "normal", "波动在正常范围内，无需告警。")

    if inconclusive:
        alert_level, reason = "inconclusive", \
            f"数据不足，无法判定是否异常（{att.get('reason', '')}）→ 本轮不告警、不下结论"
    elif noise_suppressed:
        # 阈值过了但没过显著性检验：不是"正常"，而是"证据不足"——降级为观察项，
        # 原文数字全部保留在 decision 里，避免把"没敢报"伪装成"没问题"。
        alert_level = "watch"
        reason = (f"波动 {att.get('change_pct', 0) * 100:+.1f}% 超过阈值 "
                  f"{alert_threshold_pct * 100:.0f}%，但未通过显著性检验"
                  f"（z={sig.get('z')}, p={sig.get('p_value')}）→ 证据不足（可能是日间波动噪声），"
                  f"降级为观察项：本轮不推送负责人、不建工单")
    elif is_abnormal:
        alert_level = "alert"
        reason = (f"波动 {att.get('change_pct', 0) * 100:+.1f}% 超过阈值 "
                  f"{alert_threshold_pct * 100:.0f}% 且通过显著性检验")
    else:
        alert_level = "normal"
        reason = "波动在正常范围，无需告警"

    decision = {
        "alert": None if inconclusive else is_abnormal,
        "alert_level": alert_level,
        "inconclusive": inconclusive,
        "significance": sig or None,
        "reason": reason,
        "owner": owner,
        "channel": "none" if inconclusive else ("hitl" if is_abnormal else "none"),
        "hitl_id": hitl_id,
    }
    _emit.agent_step("DecisionAgent", "decide",
                     (f"告警=不适用（数据不足）" if inconclusive
                      else f"告警={decision['alert']}（{alert_level}）→ 负责人「{owner or '未指定'}」"
                           + (f"，HITL工单 {hitl_id}" if hitl_id else "")),
                     alert=decision["alert"], alert_level=alert_level, owner=owner,
                     hitl_id=hitl_id, inconclusive=inconclusive)
    _emit.agent_done("DecisionAgent", decision)
    return decision


# ---------------------------------------------------------------- 汇总 + 可视化数据

def _build_chart(merged: Dict) -> Dict:
    """把归因/因子/下钻链转成前端 ECharts 可直接消费的数据（瀑布/占比/树）。"""
    att = merged.get("attribution") or {}
    fz = merged.get("factor_split")
    drill = merged.get("drill")

    # 归因瀑布：上期 → 各维度主因增量 → 当期
    waterfall = {"labels": [], "values": [], "deltas": []}
    if att:
        prev = float(att.get("previous_total") or 0)
        cur = float(att.get("current_total") or 0)
        waterfall["labels"] = ["上期"] + [c.get("key", "") for c in (att.get("top_contributors") or [])[:5]] + ["当期"]
        deltas = [c.get("delta", 0.0) for c in (att.get("top_contributors") or [])[:5]]
        # 尾项 = 当期 - 上期 - 已列主因增量（其余/误差收敛到 Other）
        other = cur - prev - sum(deltas)
        values = [prev] + [deltas[i] for i in range(len(deltas))] + [other]
        waterfall["values"] = values
        waterfall["deltas"] = [0] + list(deltas) + [other]

    # 主因占比（占波动的比例）
    share = {"dims": [], "values": []}
    for c in (att.get("top_contributors") or [])[:6]:
        share["dims"].append(f"{c.get('dim')}·{c.get('key')}")
        share["values"].append(c.get("delta", 0.0))

    # 下钻树：ROOT → path 各层 → 各维主因
    tree = []
    if drill:
        path = drill.get("path") or []
        node = {"name": drill.get("path_desc") or "下钻", "children": []}
        for c in (drill.get("top_contributors") or [])[:3]:
            node["children"].append({"name": c.get("desc", c.get("key", "")),
                                     "value": c.get("delta", 0)})
        tree.append(node)

    # 因子占比（factorize）
    factors = {"labels": [], "shares": []}
    if fz:
        for f in (fz.get("factors") or []):
            factors["labels"].append(f.get("label"))
            factors["shares"].append(round(f.get("share", 0) * 100, 1))
        factors["interaction"] = round((fz.get("interaction") or {}).get("share", 0) * 100, 1)

    return {"waterfall": waterfall, "share": share, "tree": tree, "factors": factors,
            "summary": {
                "metric_name": merged.get("metric_name", ""),
                "current": (att or {}).get("current_total"),
                "previous": (att or {}).get("previous_total"),
                "change_pct": (att or {}).get("change_pct", 0),
                "is_abnormal": bool((att or {}).get("is_abnormal")),
            }}


def summarize_final(question: str, aid: str, result: Dict, _emit: "_Emitter",
                    emit_done: bool = True) -> Dict:
    """把一次分析汇总成前端能用的最终载荷：结论 + 依据 + 动作 + 决策 + 可视化数据。"""
    conclusion = result.get("conclusion")
    if isinstance(conclusion, dict):
        text = conclusion.get("text", "")
        evidence = conclusion.get("evidence", [])
        actions = conclusion.get("actions", {})
        traceability = conclusion.get("traceability", {})
    else:
        text, evidence, actions, traceability = (str(conclusion or ""), [], {}, {})
    final = {
        "analysis_id": aid,
        "question": question,
        "ok": bool(result.get("ok", True)),
        "route": result.get("route", "analyze"),
        # 拒绝原因必须出现在最终载荷里：此前只有流式事件里有，聚合返回的 final
        # 既没有 reject 也没有 conclusion（空字符串），用户只看到 ok=False 不知为何。
        "reject": result.get("reject"),
        "metric": result.get("metric"),
        "metric_name": result.get("metric_name", ""),
        "certified": result.get("certified", False),
        "sql": result.get("sql", ""),
        "rows": result.get("rows", [])[:50],
        "columns": result.get("columns", []),
        "attribution": result.get("attribution"),
        "factor_split": result.get("factor_split"),
        "drill": result.get("drill"),
        "drill_decision": result.get("drill_decision"),
        "conclusion": text,
        "evidence": evidence,
        "actions": actions,
        "traceability": traceability,
        "decision": result.get("decision", {}),
        "chart": _build_chart(result),
        "supervisor": result.get("supervisor", {}),
        # 逐请求成本/延迟：让"这个产品一次分析花多少钱、多久"变成可读取的数字
        "observability": result.get("observability", {}),
    }
    if emit_done:
        _emit({"type": "done", "analysis_id": aid, "payload": final, "at": _now()})
    return final