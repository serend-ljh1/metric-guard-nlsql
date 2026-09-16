"""
tests/test_engine.py
====================
多 Agent 引擎链路测试（无需 API Key / 无需本地 Spider 数据，CI 可复现）：
  - EX 度量自洽（金标准作为预测 → 恒真）
  - 引擎链路走通（Router/SQLWriter/Review/Validator 编排可见）
  - Writer↔Critic 评审闭环存在且可运行
"""
import pytest

from sqlpa.data.schema_extractor import extract_from_sqlite
from sqlpa.eval.metrics import execution_match
from sqlpa.graph.pipeline import run_question
from sqlpa.llm.mock_llm import MockLLM
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

Q = "How many singers do we have?"
GOLD = "SELECT count(*) FROM singer"


def test_ex_self_consistent(mini_db):
    """金标准 SQL 作为预测 → EX 恒真，证明执行+度量管线正确。"""
    sb = SqlSandbox(mini_db, ExecConfig(max_rows=2000))
    r = sb.execute(GOLD)
    assert execution_match(r.rows, r.rows)


def test_pipeline_multiagent_trace(mini_db):
    sb = SqlSandbox(mini_db, ExecConfig(max_rows=2000))
    schema = extract_from_sqlite(mini_db, "mini").to_dict()
    llm = MockLLM(answer_key={Q: GOLD})
    res = run_question(Q, "mini", schema, sb, llm, gold_sql=GOLD,
                       max_repair_round=3, use_critic=True)
    assert res.final_valid
    agents = {a["agent"] for a in res.agent_trace}
    assert {"RouterAgent", "SQLWriterAgent", "ReviewAgent", "ValidatorAgent"} <= agents


def test_langgraph_engine_runs(mini_db):
    """LangGraph StateGraph 引擎可运行（未安装则跳过）。"""
    pytest.importorskip("langgraph")
    from sqlpa.graph.langgraph_graph import run_graph
    sb = SqlSandbox(mini_db, ExecConfig(max_rows=2000))
    schema = extract_from_sqlite(mini_db, "mini").to_dict()
    llm = MockLLM(answer_key={Q: GOLD})
    res = run_graph(sb, schema, llm, Q, "mini", gold_sql=GOLD,
                    max_repair_round=1, use_critic=True)
    assert res.final_valid
