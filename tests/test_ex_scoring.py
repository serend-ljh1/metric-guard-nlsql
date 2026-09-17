"""EX 判定口径测试（回归）：金标准执行失败不得被判为"正确"。

背景（真实缺陷）：修复前 `execution_match([], [])` 返回 True，而金标准 SQL
在自己沙箱里执行失败时 rows 恰好是空列表——于是"预测也失败"的错答案会被判成
正确，**系统性虚高 EX**。本文件把这个口径钉死，防止回归。
"""
from __future__ import annotations

from sqlpa.eval.metrics import execution_match, gold_match


# ---------------- gold_match：金标准失败必须算错 ----------------

def test_gold_failed_is_never_a_match():
    """金标准执行失败 → 无论预测返回什么，都必须判为不匹配。"""
    matched, reason = gold_match([], False, [])
    assert matched is False, "金标准失败 + 预测空结果 被误判为正确（这就是原缺陷）"
    assert reason == "gold_failed"


def test_gold_failed_with_nonempty_pred_still_false():
    matched, reason = gold_match([], False, [(1, "x")])
    assert matched is False
    assert reason == "gold_failed"


def test_gold_ok_and_equal_is_match():
    matched, reason = gold_match([(1, "a"), (2, "b")], True, [(2, "b"), (1, "a")])
    assert matched is True, "行序不同但集合相同应判为一致"
    assert reason == "ok"


def test_gold_ok_but_different_is_mismatch():
    matched, _ = gold_match([(1, "a")], True, [(9, "z")])
    assert matched is False


def test_both_empty_but_gold_ok_is_match():
    """金标准执行成功但确实返回空集（如 WHERE 无命中）→ 预测也空，应判为一致。"""
    matched, reason = gold_match([], True, [])
    assert matched is True
    assert reason == "ok"


# ---------------- 底层 execution_match 行为未变（保持纯集合语义）----------------

def test_execution_match_keeps_set_semantics():
    """execution_match 本身仍是纯比较器；调用方必须自己传 gold_valid。"""
    assert execution_match([], []) is True          # 这就是需要 gold_match 包一层的原因
    assert execution_match([(1,)], [(1,), (1,)]) is True   # 行去重
    assert execution_match([(1,)], [(2,)]) is False


# ---------------- 路由/引擎层：整条链路也要正确 ----------------

def test_pipeline_marks_gold_failed_when_gold_sql_is_broken(mini_db):
    """金标准 SQL 有语法错时，pipeline 必须判 final_valid=False 且标记 gold_failed。"""
    from sqlpa.data.schema_extractor import extract_from_sqlite
    from sqlpa.graph.pipeline import run_question
    from sqlpa.llm.mock_llm import MockLLM
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

    q = "How many singers do we have?"
    good = "SELECT count(*) FROM singer"
    broken_gold = "SELECT count(*) FROM table_that_does_not_exist"

    sb = SqlSandbox(mini_db, ExecConfig(max_rows=2000))
    schema = extract_from_sqlite(mini_db, "mini").to_dict()
    # MockLLM 让写手直接产出"正确"的 SQL——但因为金标准坏了，仍必须判为不可判定
    llm = MockLLM(answer_key={q: good})
    res = run_question(q, "mini", schema, sb, llm, gold_sql=broken_gold,
                       max_repair_round=0)
    assert res.gold_failed is True, "金标准执行失败未被识别"
    assert res.final_valid is False, "金标准失败却把候选判为有效（原缺陷）"


def test_pipeline_gold_failed_false_when_gold_is_healthy(mini_db):
    from sqlpa.data.schema_extractor import extract_from_sqlite
    from sqlpa.graph.pipeline import run_question
    from sqlpa.llm.mock_llm import MockLLM
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

    q = "How many singers do we have?"
    gold = "SELECT count(*) FROM singer"
    sb = SqlSandbox(mini_db, ExecConfig(max_rows=2000))
    schema = extract_from_sqlite(mini_db, "mini").to_dict()
    llm = MockLLM(answer_key={q: gold})
    res = run_question(q, "mini", schema, sb, llm, gold_sql=gold, max_repair_round=0)
    assert res.gold_failed is False
    assert res.final_valid is True


def test_langgraph_marks_gold_failed(mini_db):
    """LangGraph 引擎同样口径（未安装 langgraph 时跳过）。"""
    import pytest

    pytest.importorskip("langgraph")
    from sqlpa.data.schema_extractor import extract_from_sqlite
    from sqlpa.graph.langgraph_graph import run_graph
    from sqlpa.llm.mock_llm import MockLLM
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

    q = "How many singers do we have?"
    good = "SELECT count(*) FROM singer"
    broken_gold = "SELECT count(*) FROM table_that_does_not_exist"

    sb = SqlSandbox(mini_db, ExecConfig(max_rows=2000))
    schema = extract_from_sqlite(mini_db, "mini").to_dict()
    llm = MockLLM(answer_key={q: good})
    res = run_graph(sb, schema, llm, q, "mini", gold_sql=broken_gold, max_repair_round=0)
    assert res.gold_failed is True
    assert res.final_valid is False


def test_runner_counts_gold_failures(mini_db):
    """runner 必须把金标准失败的题计为错，并在汇总里单独报数。"""
    from sqlpa.data.loader import Benchmark, Question
    from sqlpa.eval.runner import run_benchmark
    from sqlpa.llm.mock_llm import MockLLM

    q = "How many singers do we have?"
    good = "SELECT count(*) FROM singer"
    bm = Benchmark(db_id="mini", db_path=str(mini_db),
                   questions=[Question(id=0, db_id="mini", question=q,
                                       gold_sql="SELECT count(*) FROM nope_table")])
    llm = MockLLM(answer_key={q: good})
    s = run_benchmark(bm, llm, max_repair_round=0)
    assert s.n == 1
    assert s.gold_failed == 1, "金标准失败未计入统计"
    assert s.gold_failed_rate == 1.0
    assert s.ex == 0.0, "金标准失败时 EX 必须为 0，不能因'空 vs 空'得 1"
