"""多 Agent 分析编排（档3 主角）测试。

锁定一条完整链路：RouterAgent 意图分流 → MetricMatcher 定位指标 →
ExecutorAgent 确定性取数（口径已认证）→ AttributionAgent 归因拆解 →
ConclusionAgent 输出「诊断结论+依据+建议」→ DecisionAgent 决策收口（告警+HITL 工单）。
并验证**流式事件**被逐一发射（session_start / agent_start / agent_step / agent_done / done）。

全部离线：MockLLM 兜底，SQLite 走文件库（归因并行拆解依赖文件路径开独立连接）。
"""
from __future__ import annotations

import sqlite3

from sqlpa.analysis.orchestrator import run_analysis
from sqlpa.business.metric_config import load_config
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

CFG = load_config()


class _MockLLM:
    """最小 Mock：complete 返回固定人话结论，保证离线可跑。"""
    def complete(self, prompt):
        return "GMV 下跌主要来自 SP 州订单减少，建议核查 SP 履约与促销节奏。"


def _make_db(tmpdir) -> str:
    p = tmpdir / "an.db"
    con = sqlite3.connect(p)
    con.executescript("""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','2026-08-01','delivered'),('o2','c1','2026-08-05','delivered'),
        ('o3','c2','2026-09-01','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """)
    con.commit()
    con.close()
    return str(p)


def _sb(db_path):
    return SqlSandbox(db_path, ExecConfig.from_settings(max_rows=2000))


def test_run_analysis_full_chain(tmpdir_clean):
    db_path = _make_db(tmpdir_clean)
    events = []

    def emit(p):
        events.append(p)

    result = run_analysis(
        "本月 GMV 为什么跌？", CFG, _sb(db_path), db_path, _MockLLM(),
        role="analyst", alert_threshold_pct=0.0, emit=emit,
    )

    # 事件序列完整：session_start → Router → MetricMatcher → Executor → Attribution → Conclusion → Decision → done
    types = [e["type"] for e in events]
    assert "session_start" in types
    for t in ("agent_start", "agent_step", "agent_done"):
        assert t in types
    assert "done" in types
    names = [e.get("name") for e in events if e.get("type") in ("agent_start", "agent_done")]
    for expect in ("RouterAgent", "MetricMatcher", "ExecutorAgent",
                   "AttributionAgent", "ConclusionAgent", "DecisionAgent"):
        assert expect in names, f"缺少 {expect} 的 Agent 事件"

    # 最终聚合里断言结论/依据/动作/决策/可视化全部就位
    assert result["ok"] is True
    assert result["route"] == "analyze"
    assert result["metric"] == "gmv"
    assert result["certified"] is True            # 语义层确定性编译，口径已认证
    assert result["conclusion"], "应有诊断结论文本"
    assert isinstance(result["evidence"], list) and result["evidence"], "应有结构化依据"
    assert "action" in result["actions"]          # 建议动作
    assert result["decision"]["alert"] is True    # 阈值 0.0 → 波动必超阈值 → 告警
    assert result["decision"]["channel"] == "hitl"
    assert result["decision"]["hitl_id"], "应写入 HITL 工单"
    assert result["decision"]["owner"] == "数据组-财务线"

    # 可视化数据：瀑布 / 主因占比 / 下钻 / 因子结构齐全
    chart = result["chart"]
    assert set(chart.keys()) >= {"waterfall", "share", "tree", "factors", "summary"}
    assert chart["summary"]["is_abnormal"] is True
    assert chart["waterfall"]["labels"], "瀑布应有标签"


def test_run_analysis_normal_no_alert(tmpdir_clean):
    """无波动时：结论为正常，decision 不告警、不写 HITL。"""
    db_path = _make_db(tmpdir_clean)
    # 阈值 500% → 波动远未达到，判定为正常
    result = run_analysis(
        "本月 GMV 为什么跌？", CFG, _sb(db_path), db_path, _MockLLM(),
        role="analyst", alert_threshold_pct=5.0, emit=None,
    )
    assert result["ok"] is True
    assert result["decision"]["alert"] is False
    assert result["decision"]["hitl_id"] is None
    assert result["actions"]["is_abnormal"] is False


def test_run_analysis_unmatched_rejects(tmpdir_clean):
    """未命中口径 → RouterAgent 拒绝，无归因但给结论兜底。"""
    db_path = _make_db(tmpdir_clean)
    result = run_analysis(
        "外星人数量是多少？", CFG, _sb(db_path), db_path, _MockLLM(),
        role="analyst", emit=None,
    )
    assert result["ok"] is False
    assert result["route"] == "unmatched"
    assert result["conclusion"], "未命中也应给出兜底结论说明"


def test_run_analysis_session_memory(tmpdir_clean):
    """会话级工作记忆：第一轮存主因建议，第二轮追问沿记忆续钻。"""
    db_path = _make_db(tmpdir_clean)
    memory = {}
    # 第一轮：正常分析，应写入 last_drill
    r1 = run_analysis("本月 GMV 为什么跌？", CFG, _sb(db_path), db_path,
                      _MockLLM(), role="analyst", alert_threshold_pct=0.0,
                      emit=None, memory=memory)
    assert r1["ok"] is True
    assert "last_drill" in memory, "第一轮应把主因建议写入会话记忆"
    assert memory["last_drill"]["metric"] == "gmv"
    assert memory["last_drill"]["dim"] in ("state", "category")

    # 第二轮：追问语气 → 应沿记忆续钻，不因新会话而丢失上下文
    r2 = run_analysis("那 state 的 SP 呢？", CFG, _sb(db_path), db_path,
                      _MockLLM(), role="analyst", alert_threshold_pct=0.0,
                      emit=None, memory=memory)
    assert r2["ok"] is True

    # 未传 memory 的独立调用不受影响（向后兼容）
    r3 = run_analysis("本月 GMV 为什么跌？", CFG, _sb(db_path), db_path,
                      _MockLLM(), role="analyst", alert_threshold_pct=0.0,
                      emit=None)
    assert r3["ok"] is True


# ---------------- 回归：失败必须报"数据不足"，不能被伪装成"正常波动" ----------------

def _make_db_without_current_period(tmpdir) -> str:
    """只有 2026-08 的数据：查"本月"（相对当前日期）时当期/上期都取不到 → 归因失败。"""
    p = tmpdir / "stale.db"
    con = sqlite3.connect(p)
    con.executescript("""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP');
      INSERT INTO orders VALUES ('o1','c1','2026-08-01','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100);
      INSERT INTO products VALUES ('p1','alimentos');
    """)
    con.commit()
    con.close()
    return str(p)


def test_attribution_failure_is_not_reported_as_normal(tmpdir_clean):
    """**回归**：归因查询失败 ≠ 波动正常。

    修复前：归因返回 ok=False 时，结论仍走 `is_abnormal` 分支，输出
    "当期 None、上期 None，波动 +0.0%，未超过告警阈值，属于正常波动，无需干预"，
    decision 也报 alert=False —— 把一次**分析失败**伪装成一个**结论**。
    这是最危险的一类错：用户会因此不去查一个可能真实存在的问题。
    """
    db_path = _make_db_without_current_period(tmpdir_clean)
    result = run_analysis("本月 GMV 为什么跌？", CFG, _sb(db_path), db_path,
                          _MockLLM(), role="analyst", emit=None)

    att = result["attribution"]
    assert att and att.get("ok") is False, "本用例前提是归因失败"
    # 1) 结论必须明说数据不足，且不得出现"正常波动/无需干预"这类结论性表述
    text = result["conclusion"]
    assert "数据不足" in text, f"应报数据不足，实际：{text}"
    for banned in ("正常波动", "无需干预", "未超过告警阈值"):
        assert banned not in text, f"失败被伪装成结论（出现「{banned}」）：{text}"
    # 2) 置信度是显式三态，而不是被默认成 normal
    assert result["actions"]["confidence"] == "insufficient"
    assert result["actions"]["is_abnormal"] is None
    # 3) 决策既不能说"告警"也不能说"无需告警"，而是"无法判定"
    assert result["decision"]["inconclusive"] is True
    assert result["decision"]["alert"] is None
    assert result["decision"]["hitl_id"] is None
    assert "数据不足" in result["decision"]["reason"]
    # 4) 依据里带一条"数据不足"，而不是拿 None 当基准数
    assert any(e["type"] == "数据不足" for e in result["evidence"])
    assert not any(e["type"] == "基准波动" for e in result["evidence"])


def test_analysis_failure_does_not_ask_llm_for_conclusion(tmpdir_clean):
    """数据不足时不该调结论 LLM：没有依据可依据，只会生成幻觉结论。"""
    db_path = _make_db_without_current_period(tmpdir_clean)
    calls = []

    class CountingLLM:
        def complete(self, prompt):
            calls.append(prompt)
            return "GMV 因为 SP 州履约问题大幅下滑。"

    result = run_analysis("本月 GMV 为什么跌？", CFG, _sb(db_path), db_path,
                          CountingLLM(), role="analyst", emit=None)
    text = result["conclusion"]
    assert "数据不足" in text
    assert "SP 州履约问题" not in text, "数据不足时不得采用 LLM 编造的结论"
    assert not any("你是数据分析结论 Agent" in p for p in calls), \
        "数据不足时不应调用结论 Agent 的 LLM"


def test_conclusion_prompt_carries_the_real_question(tmpdir_clean):
    """**回归**：结论 Agent 的 prompt 必须带用户原话。

    修复前该行拼的是 `str(routing.get('res', {}).__class__)`，实际发出
    "用户问题：<class 'sqlpa.business.metric_matcher.MatchResult'>" ——
    模型既不知道用户问了什么，也就无法判断该突出哪个主因。
    """
    db_path = _make_db(tmpdir_clean)
    captured = {}

    class SpyLLM:
        def complete(self, prompt):
            if "你是数据分析结论 Agent" in prompt:
                captured["prompt"] = prompt
                return "GMV 下滑，主因在 SP 州。"
            return '{"metric":"gmv","dims":["state"],"filters":[{"type":"time_range","value":"本月"}]}'

    result = run_analysis("本月 GMV 为什么跌？", CFG, _sb(db_path), db_path,
                          SpyLLM(), role="analyst", alert_threshold_pct=0.0, emit=None,
                          confirm_metric="gmv")
    assert "prompt" in captured, "应走到结论 Agent（归因需成功）"
    p = captured["prompt"]
    assert "本月 GMV 为什么跌？" in p, "prompt 里必须含用户原话"
    assert "MatchResult" not in p, "prompt 里不得出现对象类型（旧 bug）"
    # 结论必须只依据给定证据（prompt 里要有这条约束）
    assert "不得引入依据中没有的数字或维度" in p
    assert result["conclusion"] == "GMV 下滑，主因在 SP 州。"


def test_conclusion_reference_verification(tmpdir_clean):
    """**新增**：结论引用校验（"事实-证据"握手）。

    ConclusionAgent 返回里必须带 traceability；校验器确定性判定证据是否自洽：
    - 正常归因（波动%与上下期一致）→ verified=True；
    - change_pct 与上下期明显不符 → verified=False（幻觉/数据错被拦下）；
    - 因子分解 share 不守恒 → verified=False。
    """
    from sqlpa.analysis.orchestrator import _verify_references

    good = {"ok": True, "current_total": 80.0, "previous_total": 150.0,
            "change_pct": -0.46666666,
            "top_contributors": [{"key": "SP", "pct_of_change": 2.1429}]}
    t = _verify_references(good, None, None)
    assert t["verified"] is True
    assert any("基准波动自洽" in c for c in t["checks"])

    # 波动%与上下期对不上 → 校验不过（结论不能被采信）
    bad = {"ok": True, "current_total": 80.0, "previous_total": 150.0,
           "change_pct": 0.5, "top_contributors": [{"key": "SP", "pct_of_change": 0.5}]}
    assert _verify_references(bad, None, None)["verified"] is False

    # 因子分解 share 合计 ≠1 → 校验不过
    fz_noise = {"factors": [{"label": "订单量", "share": 0.6}, {"label": "客单价", "share": 0.7}]}
    assert _verify_references({}, fz_noise, None)["verified"] is False

    # 端到端：结论节点返回带 traceability 字段
    from sqlpa.analysis.orchestrator import run_analysis
    db_path = _make_db(tmpdir_clean)
    result = run_analysis("本月 GMV 为什么跌？", CFG, _sb(db_path), db_path,
                          _MockLLM(), role="analyst", alert_threshold_pct=0.0, emit=None)
    trace = result.get("traceability")
    assert isinstance(trace, dict), f"结论应带 traceability，实际：{result.get('traceability')}"
    assert "verified" in trace and isinstance(trace["verified"], bool)


def test_analysis_is_langgraph_orchestrated(tmpdir_clean):
    """**新增**：分析编排是**真 LangGraph** 构建的（StateGraph 节点+条件边，而非手写 if/return）。

    验证「多智能体 langgraph 编排」在代码里落地，而不只是 README 里的说法：
    - `_build_analysis_graph` 返回真实的可调用 CompiledStateGraph；
    - 图里有 6 个 Agent 节点 + 2 条条件边（Router 分流、Executor 分流）；
    - 一条端到端分析确实由该图 invoke 驱动并产出结论。
    """
    from sqlpa.analysis.orchestrator import _build_analysis_graph
    from sqlpa.analysis.orchestrator import _Emitter

    _emit = _Emitter(lambda p: None)
    graph = _build_analysis_graph(CFG, None, "n/a", _MockLLM(), "analyst", None,
                                  0.0, None, {}, _emit, "本月 GMV 为什么跌？")
    graph_name = type(graph).__name__
    assert graph_name in ("CompiledStateGraph", "CompiledGraph"), \
        f"应返回 LangGraph 编译图，实际类型：{graph_name}"

    # 通过 LangGraph 自身的图结构接口确认 6 个 Agent 节点都在图上
    g = graph.get_graph()
    node_names = set(g.nodes.keys())
    assert {"router", "executor", "attribution", "conclusion", "decision",
            "unmatched", "executor_reject", "confirm"} <= node_names, \
        f"缺少 LangGraph 节点，实际有：{sorted(node_names)}"

    # 端到端：同一图驱动完整分析并产出结论（验证状态流收敛正确）
    db_path = _make_db(tmpdir_clean)
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox
    sb = SqlSandbox(db_path, ExecConfig.from_settings(max_rows=2000))
    g2 = _build_analysis_graph(CFG, sb, db_path, _MockLLM(), "analyst", None,
                               0.0, None, {}, _Emitter(lambda p: None), "本月 GMV 为什么跌？")
    state = g2.invoke({"routing": {}})
    assert "reject" not in state or state.get("reject") is None
    assert state["routing"]["route"] == "analyze"
    assert state["conclusion"]["text"]
    assert state["decision"]["alert"] is True


# ---------------- 口径确认门（HITL 前置）：LLM 推断口径需人工确认 ----------------

class _JsonMetricLLM:
    """返回合法指标 JSON：让 match 走 method="llm" 分支（LLM 推断口径）。"""
    def complete(self, prompt):
        return '{"metric":"gmv","dims":[],"filters":[]}'


def test_confirm_gate_llm_inferred_metric_requires_confirmation(tmpdir_clean):
    """LLM 推断口径且未确认 → 停在确认门：不执行取数/归因，返回待确认载荷。"""
    db_path = _make_db(tmpdir_clean)
    events = []

    def emit(p):
        events.append(p)

    result = run_analysis("本月 GMV 波动大，为什么跌？", CFG, _sb(db_path), db_path,
                          _JsonMetricLLM(), role="analyst", alert_threshold_pct=0.0,
                          emit=emit)
    # 待确认：ok=False、need_confirm=True、无取数/归因/结论
    assert result["ok"] is False
    assert result["need_confirm"] is True
    assert result["route"] == "confirm"
    assert result["metric"] == "gmv"
    assert "GMV" in result["metric_name"]
    assert result["conclusion"] == ""
    # 给前端确认卡的回显：proposed 口径 + 可选候选口径
    assert result["proposed"]["metric"] == "gmv"
    assert isinstance(result["candidates"], list) and result["candidates"]
    keys = [c["key"] for c in result["candidates"]]
    assert "gmv" in keys
    # 流式事件里带 confirm_required，且绝不该出现 Executor/Attribution（没执行）
    assert any(e["type"] == "confirm_required" for e in events)
    names = [e.get("name") for e in events if e.get("type") == "agent_start"]
    assert "ExecutorAgent" not in names, "口径未确认就不应执行取数"
    assert "AttributionAgent" not in names, "口径未确认就不应跑归因"


def test_confirm_gate_confirmed_metric_runs(tmpdir_clean):
    """用户在确认窗口选定口径（confirm_metric）→ 确定性放行并完成分析。"""
    db_path = _make_db(tmpdir_clean)
    result = run_analysis("本月 GMV 波动大，为什么跌？", CFG, _sb(db_path), db_path,
                          _JsonMetricLLM(), role="analyst", alert_threshold_pct=0.0,
                          emit=None, confirm_metric="gmv")
    assert result["ok"] is True
    assert result["route"] == "analyze"
    assert result["metric"] == "gmv"
    assert result.get("need_confirm", False) is not True
    # 确认后走确定性执行，方法与指标都已锁定
    assert result["certified"] is True


def test_confirm_gate_keyword_method_auto_runs(tmpdir_clean):
    """确定性关键词命中（method=keyword）→ 无论如何都直接放行，不打扰用户确认。"""
    db_path = _make_db(tmpdir_clean)
    # _MockLLM 返回非 JSON → metric_matcher 落到 keyword 兜底，method="keyword"
    result = run_analysis("本月 GMV 为什么跌？", CFG, _sb(db_path), db_path,
                          _MockLLM(), role="analyst", alert_threshold_pct=0.0, emit=None)
    assert result["ok"] is True
    assert result.get("need_confirm", False) is not True
    assert result["route"] == "analyze"