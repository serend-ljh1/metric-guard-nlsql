"""
sqlpa.graph.langgraph_graph
===========================
生产版：真正的 LangGraph StateGraph 多智能体 Text-to-SQL 引擎。

与 `pipeline.py`（确定性参考编排器）**语义等价**，这里的"调度"用 LangGraph 显式建模：
Supervisor 协调 + 专业化 Agent 节点 + 条件路由 + 自愈循环护栏。

Critic 采用事后评审：SQL 先执行，只有"执行成功但结果不对"时才让 Critic 看执行结果
判断是否回答了问题，避免旧版"事前审写法"导致同模型自评退化为风格改写、把对的改错。

运行前提（在你的 PyCharm 环境）：
  1) pip install -r requirements.txt（含 langgraph）
  2) .env 配好 LLM_API_KEY（DeepSeek/OpenAI 兼容）
  3) 用 `--engine langgraph` 切换（run_eval / run_business 已支持）

用法：
  from sqlpa.graph.langgraph_graph import build_graph, initial_state
  from sqlpa.llm.openai_compat import OpenAICompatLLM
  g = build_graph(sb, schema, llm, gold_sql=..., use_critic=...)
  out = g.invoke(initial_state(question, db_id))
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, TypedDict

from sqlpa.agents.router import route_decision, route_with_llm_fallback
from sqlpa.eval.metrics import execution_match, gold_match
from sqlpa.llm.base import build_schema_text
from sqlpa.sandbox.sql_executor import SqlSandbox


class AgentState(TypedDict, total=False):
    question: str
    db_id: str
    schema_text: str
    route: str
    query_plan: str
    current_sql: str
    exec_result: Dict[str, Any]
    repairs: int
    max_repair_round: int
    review_rounds: int
    max_review_round: int
    use_critic: bool
    metric_constraint: str
    dialect_hint: str
    review_pass: bool
    review_feedback: str
    final_sql: str
    final_valid: bool
    terminate_reason: str
    final_answer: str
    agent_trace: List[Dict]


def _execute(sb: SqlSandbox, sql: str) -> Dict:
    r = sb.execute(sql)
    return {"sql": sql, "ok": r.ok, "reason": r.reason, "error": r.error,
            "rows": r.rows, "columns": r.columns}


def _make_gold_runner(sb: SqlSandbox, gold_sql: Optional[str]):
    """返回一个"只执行金标准一次"的取结果函数。

    条件边与 validate 节点都会判定 gold，而金标准 SQL 在一次 run_graph 内是常量：
    缓存后避免同一段 SQL 被反复执行（多次执行还可能触发沙箱超时/抖动）。
    """
    cache: Dict[str, Dict] = {}

    def gold_result() -> Optional[Dict]:
        if gold_sql is None:
            return None
        if gold_sql not in cache:
            cache[gold_sql] = _execute(sb, gold_sql)
        return cache[gold_sql]

    return gold_result


def _logged(state: AgentState, agent: str, role: str, detail: str, t0: float) -> List[Dict]:
    tr = list(state.get("agent_trace", []))
    tr.append({"agent": agent, "role": role, "detail": detail,
               "ms": int((time.time() - t0) * 1000)})
    return tr


def _check(sb: SqlSandbox, llm, state: AgentState, gold_sql: Optional[str],
           gold_result=None) -> "tuple[bool, str]":
    """候选 SQL 是否可判为正确，以及原因。

    返回 (valid, reason)，reason ∈ {ok, exec_failed, gold_failed, mismatch, semantic}。

    关键点：**金标准执行失败时必须单独识别**。修复前这里直接
    `execution_match(gold["rows"], res["rows"])`，而金标准失败时 gold["rows"] 为空，
    候选若也返回空结果就会被判为"一致"，把错答案算成对（系统性虚高 EX）。
    """
    res = state["exec_result"]
    if not res.get("ok"):
        return False, "exec_failed"
    if gold_sql is not None:
        gold = gold_result() if callable(gold_result) else _execute(sb, gold_sql)
        if gold is None or not gold.get("ok"):
            # 金标准自身跑不出结果 → 本题不可判定，绝不算对
            return False, "gold_failed"
        matched, _ = gold_match(gold["rows"], True, res["rows"])
        return (True, "ok") if matched else (False, "mismatch")
    valid = bool(llm.validate_semantics(state["question"], res.get("sql", ""), res).get("valid", True))
    return valid, "semantic"


def _is_valid(sb: SqlSandbox, llm, state: AgentState, gold_sql: Optional[str],
              gold_result=None) -> bool:
    return _check(sb, llm, state, gold_sql, gold_result)[0]


def build_graph(sb: SqlSandbox, schema: Dict, llm, gold_sql: Optional[str] = None,
                max_repair_round: int = 3, use_critic: bool = False,
                max_review_round: int = 2, schema_text: Optional[str] = None):
    from langgraph.graph import StateGraph, START, END

    schema_text = schema_text or build_schema_text(schema)
    gold_result = _make_gold_runner(sb, gold_sql)

    # ---------- 节点 ----------
    def route(state: AgentState) -> Dict:
        t0 = time.time()
        rt = route_with_llm_fallback(state["question"], schema, llm=llm,
                                     schema_text=schema_text)
        tr = _logged(state, "RouterAgent", "难度路由",
                     f"判定={rt['decision']} (涉及 {rt['n_tables']} 表"
                     + (", LLM兜底=是" if rt.get("llm_fallback") else "") + ")", t0)
        return {"route": rt["decision"], "agent_trace": tr}

    def plan(state: AgentState) -> Dict:
        t0 = time.time()
        p = "拆解为多表连接/聚合步骤" if state.get("route") == "complex" else ""
        return {"query_plan": p, "agent_trace": _logged(state, "PlannerAgent", "查询规划", p or "简单,跳过", t0)}

    def write(state: AgentState) -> Dict:
        t0 = time.time()
        ntry = state.get("current_sql")
        prev = None
        if state.get("review_feedback"):
            prev = {"sql": state.get("current_sql"), "feedback": state.get("review_feedback")}
        elif state.get("exec_result") and not state["exec_result"].get("ok"):
            prev = {"sql": state.get("current_sql"), "error": state["exec_result"].get("error")}
        sql = llm.generate_sql(state["question"], schema_text, plan=state.get("query_plan", ""),
                               previous_try=prev, metric_constraint=state.get("metric_constraint", ""),
                               dialect_hint=state.get("dialect_hint", ""))
        return {"current_sql": sql,
                "agent_trace": _logged(state, "SQLWriterAgent",
                                       "LLM生成SQL" + ("(修订)" if prev else ""),
                                       state["question"][:26], t0)}

    def review(state: AgentState) -> Dict:
        """事后评审：基于【执行结果】判断 SQL 是否回答了问题。

        与旧版"事前审 SQL 写法"的关键区别：这里 review 发生在 execute 之后，
        Critic 手里有 question + SQL + schema + **执行结果**，可以判断"跑出来的
        数据到底有没有回答用户问题"，而不是凭写法风格瞎改。
        """
        t0 = time.time()
        rev = llm.review_sql(state["question"], state.get("current_sql", ""),
                             schema_text, exec_result=state.get("exec_result"))
        return {"review_pass": bool(rev.get("pass", True)),
                "review_feedback": rev.get("feedback", ""),
                "review_rounds": state.get("review_rounds", 0) + 1,
                "agent_trace": _logged(state, "ReviewAgent", "结果评审(事后)",
                                       f"pass={bool(rev.get('pass', True))} issues={len(rev.get('issues', []))}", t0)}

    def execute(state: AgentState) -> Dict:
        t0 = time.time()
        r = _execute(sb, state.get("current_sql", ""))
        return {"exec_result": r,
                "agent_trace": _logged(state, "ExecutorAgent", "只读沙箱执行",
                                       f"ok={r['ok']} rows={len(r['rows'])}", t0)}

    def diagnose(state: AgentState) -> Dict:
        t0 = time.time()
        # exec_result 里 error 可能是 None（例如"结果不匹配"但无报错信息），
        # 而 LLMProvider.diagnose_error 约定 error 为 str —— 先归一化，
        # 否则 mock/真实实现里对 error 做切片都会 TypeError。
        err = state["exec_result"].get("error") or \
            state["exec_result"].get("reason") or "result_mismatch"
        diag = llm.diagnose_error(state.get("current_sql", ""), str(err), schema_text)
        return {"error_diagnosis": diag, "repairs": state.get("repairs", 0) + 1,
                "agent_trace": _logged(state, "DiagnoseAgent", "报错诊断", (diag or "")[:45], t0)}

    def validate(state: AgentState) -> Dict:
        t0 = time.time()
        valid, reason = _check(sb, llm, state, gold_sql, gold_result)
        # 金标准失败时把原因如实写进 terminate_reason，便于评测统计与排查
        if valid:
            term = "ok"
        elif reason == "gold_failed":
            term = "gold_failed"   # 本题不可判定：既非对也非模型错
        else:
            term = "mismatch"
        return {"final_sql": state.get("current_sql", ""), "final_valid": valid,
                "terminate_reason": term,
                "agent_trace": _logged(state, "ValidatorAgent", "结果校验",
                                       f"valid={valid} reason={reason}", t0)}

    def give_up(state: AgentState) -> Dict:
        return {"final_sql": state.get("current_sql", ""), "final_valid": False,
                "terminate_reason": "max_retry"}

    # ---------- 条件分支 ----------
    def after_route(state: AgentState) -> str:
        return "plan" if state.get("route") == "complex" else "write"

    def after_review(state: AgentState) -> str:
        """事后评审后的分支：
        - 通过 → validate（结果被 Critic 认可）
        - 不通过且未达轮次上限 → rewrite（按反馈改写后重新执行）
        - 不通过且达轮次上限 → 转入 diagnose 自愈（或 give_up）
        """
        if state.get("review_pass"):
            return "validate"
        if state.get("review_rounds", 0) < max_review_round:
            return "rewrite"
        if state.get("repairs", 0) >= max_repair_round:
            return "give_up"
        return "diagnose"

    def after_execute(state: AgentState) -> str:
        valid, reason = _check(sb, llm, state, gold_sql, gold_result)
        if valid:
            return "validate"
        # 金标准自己跑不出来 → 本题不可判定，直接进 validate 如实记录，
        # 不要去 diagnose 白白修复（修复也救不了一个坏掉的金标准）。
        if reason == "gold_failed":
            return "validate"
        # 执行失败 → 走 diagnose 自愈（Critic 帮不上忙，因为没有结果可看）
        if not state["exec_result"].get("ok"):
            if state.get("repairs", 0) >= max_repair_round:
                return "give_up"
            return "diagnose"
        # 执行成功但结果不对 → 若开了 Critic 且未达评审轮次，交给 Critic 看结果判断
        if state.get("use_critic") and state.get("review_rounds", 0) < max_review_round:
            return "review"
        if state.get("repairs", 0) >= max_repair_round:
            return "give_up"
        return "diagnose"

    # ---------- 组装图 ----------
    g = StateGraph(AgentState)
    g.add_node("route", route)
    g.add_node("plan", plan)
    g.add_node("write", write)
    g.add_node("review", review)
    g.add_node("rewrite", write)          # 复用写手节点，按评审反馈修订
    g.add_node("execute", execute)
    g.add_node("diagnose", diagnose)
    g.add_node("validate", validate)
    g.add_node("give_up", give_up)

    g.add_edge(START, "route")
    g.add_conditional_edges("route", after_route, {"plan": "plan", "write": "write"})
    g.add_edge("plan", "write")
    # 写完直接执行（事前评审已移除，Critic 改为事后看执行结果）
    g.add_edge("write", "execute")
    g.add_conditional_edges("execute", after_execute,
                            {"validate": "validate", "review": "review",
                             "diagnose": "diagnose", "give_up": "give_up"})
    g.add_conditional_edges("review", after_review,
                            {"validate": "validate", "rewrite": "rewrite",
                             "diagnose": "diagnose", "give_up": "give_up"})
    g.add_edge("rewrite", "execute")    # 评审→改写→重新执行（改写后的 SQL 需重跑才能再审）
    g.add_edge("diagnose", "write")     # 自愈：诊断后写手修复（复用 write，previous_try 带 error）
    g.add_edge("validate", END)
    g.add_edge("give_up", END)

    return g.compile()


def initial_state(question: str, db_id: str, metric_constraint: str = "",
                  use_critic: bool = False, max_repair_round: int = 3,
                  max_review_round: int = 2, dialect_hint: str = "") -> AgentState:
    return {"question": question, "db_id": db_id, "route": "", "current_sql": "",
            "repairs": 0, "max_repair_round": max_repair_round, "review_rounds": 0,
            "max_review_round": max_review_round, "use_critic": use_critic,
            "metric_constraint": metric_constraint, "dialect_hint": dialect_hint,
            "review_pass": True,
            "review_feedback": "", "final_valid": False, "terminate_reason": "",
            "agent_trace": []}


def run_graph(sb: SqlSandbox, schema: Dict, llm, question: str, db_id: str,
              gold_sql: Optional[str] = None, max_repair_round: int = 3,
              use_critic: bool = False, max_review_round: int = 2,
              metric_constraint: str = "", use_schema_link: bool = False,
              dialect_hint: str = "") -> Dict:
    """用 LangGraph StateGraph 引擎跑一条题，返回与 pipeline.run_question 一致的字段。"""
    if use_schema_link:
        from sqlpa.agents.schema_linker import link
        lk = link(question, schema)
        schema_text = lk["schema_text"]
    else:
        from sqlpa.llm.base import build_schema_text
        lk = None
        schema_text = build_schema_text(schema)

    g = build_graph(sb, schema, llm, gold_sql=gold_sql, max_repair_round=max_repair_round,
                    use_critic=use_critic, max_review_round=max_review_round,
                    schema_text=schema_text)
    st = initial_state(question, db_id, metric_constraint=metric_constraint,
                       use_critic=use_critic, max_repair_round=max_repair_round,
                       max_review_round=max_review_round, dialect_hint=dialect_hint)
    out = g.invoke(st)

    at = list(out.get("agent_trace", []))
    if use_schema_link and lk:
        at.insert(0, {"agent": "SchemaLinkerAgent", "role": "schema裁剪",
                      "detail": f"列 {lk['orig_cols']}->{lk['kept_cols']} (reduced={lk['reduced']})",
                      "ms": 0})
    attempts = sum(1 for a in at if a["agent"].startswith("SQLWriterAgent"))
    trace = [f"{a['agent']}|{a['role']}: {a['detail']}" for a in at]
    from sqlpa.graph.pipeline import PipelineResult
    return PipelineResult(
        question=question, route=out.get("route", ""),
        planned=(out.get("route") == "complex"), attempts=attempts,
        repairs=out.get("repairs", 0), final_sql=out.get("final_sql", ""),
        exec_result=out.get("exec_result", {"ok": False, "sql": "", "rows": [], "columns": []}),
        final_valid=bool(out.get("final_valid")),
        terminate_reason=out.get("terminate_reason", ""),
        gold_failed=(out.get("terminate_reason") == "gold_failed"),
        trace=trace, agent_trace=at)
