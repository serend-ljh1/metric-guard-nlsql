"""
sqlpa.graph.pipeline
====================
确定性参考编排器：把"路由 → Schema → 规划 → 生成 → 沙箱执行 → 诊断 → 修复 → 校验"
的多 Agent 控制流显式建模，且**离线可运行**（配合 MockLLM 演示链路、真实 LLM 时即真跑）。

它与 LangGraph 生产版结构一一对应；这里用一段显式流程把"自愈闭环 + 重试护栏"跑出来，
便于在没有 API Key 的环境下验证编排逻辑正确。

⚠️ 说明：本模块决定"怎么调度"，不决定"SQL 对不对"。SQL 质量完全取决于注入的 LLM。
      因此 EX 准确率只能在接真实 LLM 后评测，本模块只负责流程正确性。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Dict, List, Optional

from sqlpa.agents.router import route_decision, route_with_llm_fallback
from sqlpa.config import get as cfg_get
from sqlpa.eval.metrics import execution_match, gold_match
from sqlpa.llm.base import build_schema_text
from sqlpa.sandbox.sql_executor import SqlSandbox


@dataclass
class PipelineResult:
    question: str
    route: str
    planned: bool
    attempts: int
    repairs: int
    final_sql: str
    exec_result: Dict
    final_valid: bool
    terminate_reason: str
    gold_failed: bool = False  # 金标准 SQL 在本沙箱执行失败 → 本题不可判定（必须单独统计）
    trace: List[str] = field(default_factory=list)
    agent_trace: List[Dict] = field(default_factory=list)  # [{agent, role, detail, ms}]

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items()}


def _trace(agent_trace: List[Dict], agent: str, role: str, detail: str, t0: float) -> None:
    """记录一个"agent 动作":名字/角色/干了啥/耗时(毫秒)。"""
    agent_trace.append({"agent": agent, "role": role, "detail": detail,
                        "ms": int((time.time() - t0) * 1000)})


def _run_sql(sb: SqlSandbox, sql: str) -> Dict:
    r = sb.execute(sql)
    return {"sql": sql, "ok": r.ok, "reason": r.reason, "error": r.error,
            "rows": r.rows, "columns": r.columns}


def run_question(question: str, db_id: str, schema: Dict, sb: SqlSandbox,
                 llm, gold_sql: Optional[str] = None,
                 max_repair_round: Optional[int] = None,
                 want_repair_demo: Optional[str] = None,
                 use_critic: bool = False,
                 max_review_round: int = 2,
                 metric_constraint: str = "",
                 use_schema_link: bool = False,
                 dialect_hint: str = "") -> PipelineResult:
    """对单条问题跑一遍完整链路，返回可观测结果。

    want_repair_demo: 若传入某条问题的"故意错误 SQL"，首次会先生成它来触发修复循环。
    use_critic: 是否启用事后 Critic 评审（执行成功但结果不对时，让 Critic 看执行结果判断是否回答了问题）。
    use_schema_link: 是否启用 Schema-Linker（列级裁剪 schema 上下文，省token/降噪）。
    dialect_hint: 目标数据库方言提示（业务模式接 MySQL/PostgreSQL 时注入）。
    """
    agent_trace: List[Dict] = []
    # ---- Schema-Linker：可选的列级 schema 裁剪 ----
    if use_schema_link:
        from sqlpa.agents.schema_linker import link
        t0 = time.time()
        lk = link(question, schema)
        schema_text = lk["schema_text"]
        _trace(agent_trace, "SchemaLinkerAgent", "schema裁剪",
               f"列 {lk['orig_cols']}->{lk['kept_cols']} (reduced={lk['reduced']})", t0)
    else:
        schema_text = build_schema_text(schema)
    if max_repair_round is None:
        max_repair_round = int(cfg_get("pipeline.max_repair_round", 3))
    t0 = time.time()
    # 确定性路由 +（可选，settings.yaml 开关）LLM 二次判断兜底。
    # 评测/测试可通过 ROUTE_LLM_FALLBACK 环境变量或显式参数关闭以保证可复现。
    import os as _os
    _env = _os.environ.get("SQLPA_ROUTE_LLM_FALLBACK")
    _enabled = None if _env is None else (_env.strip().lower() in ("1", "true", "yes"))
    rt = route_with_llm_fallback(question, schema, llm=llm, schema_text=schema_text,
                                 enabled=_enabled)
    route = rt["decision"]
    _trace(agent_trace, "RouterAgent", "难度路由",
           f"判定={route} (涉及 {rt['n_tables']} 表, logic={rt['logic_hit']}"
           + (", LLM兜底=是" if rt.get("llm_fallback") else "") + ")", t0)
    trace = [f"route={route} ({rt['n_tables']} tables, logic={rt['logic_hit']}"
             + (f", llm_fallback={rt['llm_fallback']}" if rt.get("llm_fallback") else "") + ")"]

    state = {"question": question, "db_id": db_id, "schema_text": schema_text}
    prev_try: Optional[Dict] = None
    attempts = 0
    repairs = 0
    terminate_reason = ""
    llm_error: Optional[str] = None
    gold_failed = [False]  # 用列表以便在闭包 validate() 里写回

    def execute(sql: str) -> Dict:
        t = time.time()
        r = _run_sql(sb, sql)
        _trace(agent_trace, "ExecutorAgent", "只读沙箱执行",
               f"ok={r['ok']} rows={len(r['rows'])}" + (f" err={r['error']}" if not r['ok'] else ""), t)
        return r

    def generate(previous_try: Optional[Dict] = None) -> Optional[str]:
        """调用 LLM(Writer) 生成 SQL；单次失败不抛异常，返回 None 并记录原因。"""
        nonlocal llm_error, attempts
        attempts += 1
        t = time.time()
        who = "SQLWriterAgent" + ("(修订)" if previous_try else "")
        try:
            out = llm.generate_sql(question, schema_text, plan="", previous_try=previous_try,
                                   metric_constraint=metric_constraint,
                                   dialect_hint=dialect_hint)
            _trace(agent_trace, who, "LLM生成SQL", str(question)[:26], t)
            return out
        except Exception as e:  # noqa: BLE001
            llm_error = str(e)[:140]
            _trace(agent_trace, who, "LLM生成SQL", f"失败: {llm_error}", t)
            return None

    def validate(exec_res: Dict) -> bool:
        t = time.time()
        if not exec_res["ok"]:
            _trace(agent_trace, "ValidatorAgent", "结果校验", "执行失败,不校验", t)
            return False
        # 若有 gold，用执行结果比对方可确定"语义是否对"（这才是硬判据）
        if gold_sql is not None:
            gold_res = _run_sql(sb, gold_sql)
            if not gold_res["ok"]:
                # 金标准自己都跑不出来（方言/超时/语法）→ 本题无法判定。
                # 修复前这里会用"空 vs 空"判为一致，把错答案算成对，系统性虚高 EX。
                gold_failed[0] = True
                _trace(agent_trace, "ValidatorAgent", "结果校验(VS金标准)",
                       f"金标准执行失败({gold_res.get('error') or gold_res.get('reason')}),本题判为不可判定",
                       t)
                return False
            matched, _reason = gold_match(gold_res["rows"], True, exec_res["rows"])
            _trace(agent_trace, "ValidatorAgent", "结果校验(VS金标准)",
                   f"一致={matched} 行数 gold={len(gold_res['rows'])} pred={len(exec_res['rows'])}", t)
            return matched
        v = llm.validate_semantics(question, exec_res["sql"], exec_res)
        ok = bool(v.get("valid", True))
        _trace(agent_trace, "ValidatorAgent", "语义校验", f"valid={ok}", t)
        return ok

    def fail(reason: str, resp_reason: str) -> PipelineResult:
        return PipelineResult(
            question=question, route=route, planned=(route == "complex"),
            attempts=attempts, repairs=repairs, final_sql="",
            exec_result={"sql": "", "ok": False, "reason": resp_reason,
                         "error": reason, "rows": [], "columns": []},
            final_valid=False, terminate_reason=resp_reason, gold_failed=gold_failed[0],
            trace=trace, agent_trace=agent_trace)

    # ---- 第一步：Supervisor 派发 → Writer 生成 SQL ----
    t0 = time.time()
    _trace(agent_trace, "SupervisorAgent", "任务派发",
           f"基于路由({route}) 分发给下游 Agent", t0)
    sql = generate(None)
    if sql is None:
        trace.append(f"LLM 生成失败: {llm_error}")
        return fail(f"LLM 生成失败: {llm_error}", "llm_error")
    if want_repair_demo is not None:
        sql = want_repair_demo  # 用于演示自愈：首次强制错误

    exec_res = execute(sql)
    trace.append(f"attempt1: ok={exec_res['ok']} err={exec_res['error']}")

    review_rounds = 0

    # ---- 自愈循环：事后评审(Critic看结果) → Diagnose → Writer(修复) → Executor → Validator ----
    while not (exec_res["ok"] and validate(exec_res)):
        # 执行成功但结果不对，且开了 Critic 且未达评审轮次 → 让 Critic 看结果判断
        if (exec_res["ok"] and use_critic and review_rounds < max_review_round):
            review_rounds += 1
            t0 = time.time()
            rev = llm.review_sql(question, sql, schema_text, exec_res)
            _trace(agent_trace, "ReviewAgent", "结果评审(事后)",
                   f"pass={rev.get('pass')} issues={len(rev.get('issues', []))} {str(rev.get('feedback'))[:28]}", t0)
            if rev.get("pass"):
                # Critic 认可结果，但 validate 不通过（评测模式金标准不匹配）→ 不再评审，走 diagnose
                pass
            else:
                sql = generate({"feedback": rev.get("feedback", ""), "sql": sql})
                if sql is None:
                    trace.append(f"评审修订阶段 LLM 失败: {llm_error}")
                    return fail(f"评审修订阶段 LLM 失败: {llm_error}", "llm_error")
                exec_res = execute(sql)
                trace.append(f"attempt{attempts}: ok={exec_res['ok']} err={exec_res['error']}")
                continue  # 重新校验

        if repairs >= max_repair_round:
            terminate_reason = "max_retry"
            trace.append(f"达到最大修复轮次({max_repair_round})，终止")
            _trace(agent_trace, "SupervisorAgent", "护栏终止",
                   f"达到 max_repair={max_repair_round}", time.time())
            break
        repairs += 1
        t0 = time.time()
        # error 可能是 None（例如"结果不匹配"但执行本身没报错），而 LLMProvider
        # 约定 error 为 str —— 归一化后再传，否则实现里对 error 切片会 TypeError。
        err = exec_res.get("error") or exec_res.get("reason") or "result_mismatch"
        diag = llm.diagnose_error(sql, str(err), schema_text)
        _trace(agent_trace, "DiagnoseAgent", "报错诊断",
               (diag or "")[:45], t0)
        trace.append(f"repair{repairs}: {diag}")
        sql = generate({"sql": sql, "error": exec_res.get("error")})
        if sql is None:
            trace.append(f"修复阶段 LLM 失败: {llm_error}")
            return fail(f"修复阶段 LLM 失败: {llm_error}", "llm_error")
        exec_res = execute(sql)
        trace.append(f"attempt{attempts}: ok={exec_res['ok']} err={exec_res['error']}")

    final_valid = bool(exec_res["ok"]) and validate(exec_res)
    if final_valid and not terminate_reason:
        terminate_reason = "ok"
    return PipelineResult(
        question=question, route=route, planned=(route == "complex"),
        attempts=attempts, repairs=repairs, final_sql=exec_res["sql"],
        exec_result=exec_res, final_valid=final_valid,
        terminate_reason=terminate_reason, gold_failed=gold_failed[0],
        trace=trace, agent_trace=agent_trace)
