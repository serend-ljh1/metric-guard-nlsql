"""QuerySpec 表达力扩展测试：算子化过滤 / 集合 / 区间 / HAVING / 否定形态。

这批测试的立场：**表达能力与守卫必须联动**。
  - 新增的算子只能来自配置白名单（取值走字面量转义，算子走白名单）——意图层/LLM
    都无法把任意 SQL 片段送进查询；
  - 一旦某个句式能被表达，原来"一律拒答"的守卫就必须放行；表达不了时**仍必须拒答**。

用迷你两表库（`singer`/`concert`）做端到端验证，不依赖任何外部数据。
"""
from __future__ import annotations

import sqlite3

import pytest

from sqlpa.business import metric_matcher as mm
from sqlpa.business.compiler import (
    CompileError, QuerySpec, compile_spec, parse_derived, render_filter,
)
from sqlpa.business.metric_config import (
    BusinessConfig, Dimension, Metric, load_config,
)
from sqlpa.business.sqlgen import aliases_needed_for_filters


def _cfg() -> BusinessConfig:
    """迷你域：singer(国家/名字) + concert(年份)，覆盖比较/集合/区间/否定/HAVING。"""
    return BusinessConfig(
        metrics={
            "singer_count": Metric(
                key="singer_count", name="歌手数", desc="", metric_expr="COUNT(*)",
                from_clause="FROM singer s", where_core="1=1",
                support_dims=["country"],
                support_filters=["country", "year_not", "year"]),
            "concert_count": Metric(
                key="concert_count", name="演出场次", desc="", metric_expr="COUNT(*)",
                from_clause="FROM singer s", join_clause="JOIN concert c ON c.singer_id=s.singer_id",
                where_core="1=1", support_dims=["name"], support_filters=["year"]),
        },
        dimensions={
            "country": Dimension("country", "国家", "s.country"),
            "name": Dimension("name", "歌手", "s.name"),
            "year": Dimension("year", "年份", "c.year"),
        },
        filter_templates={
            "country": "s.country {op} {value}",
            "year": "c.year {op} {value}",
            # 自包含的反连接子查询：模板自己定义 c2，不需要外部注入 JOIN
            "year_not": "s.singer_id NOT IN (SELECT c2.singer_id FROM concert c2 WHERE c2.year = {value})",
        },
        filter_ops={
            "country": {"eq": "=", "ne": "!=", "in": "IN", "nin": "NOT IN"},
            "year": {"eq": "=", "lt": "<", "gt": ">", "gte": ">=", "between": "BETWEEN", "in": "IN"},
        },
        metric_ops={"eq": "=", "gt": ">", "gte": ">=", "between": "BETWEEN"},
        matcher_keywords={
            "metrics": {"singer_count": ["how many singers"],
                        "concert_count": ["how many concerts"]},
            "dimensions": {"country": ["by country"], "name": ["per singer"]},
            "filter_values": {"country": ["US", "UK"]},
            "filter_multi": {"country": "or"},
            "numeric_filters": {"year": {"cues": {"lt": ["before"], "gt": ["after"],
                                                  "between": ["between"]}}},
            "having_filters": {"metric": {"entity_dim": "name",
                                          "cues": {"gte": ["at least"], "gt": ["more than"]}}},
            "negated_filters": {
                "cues": ["not", "never", "other than"],
                "country": {"variants": [{"filter": "year_not", "requires": []}]},
            },
        },
    )


# ---------------------------------------------------------------- 算子渲染

def test_scalar_filter_still_compiles_as_equality():
    """向后兼容：未声明算子白名单的过滤类型仍只允许等值，SQL 形态与旧版一致。"""
    cfg = load_config()                                   # Olist 默认域
    assert render_filter(cfg, "state", "SP") == "c.customer_state = 'SP'"
    with pytest.raises(CompileError, match="不支持算子"):
        render_filter(cfg, "state", {"op": "ne", "value": "SP"})


def test_declared_operator_renders_from_whitelist():
    """算子名 → SQL 记号只能来自配置白名单；数值不加引号（跨方言类型安全）。"""
    c = _cfg()
    assert render_filter(c, "year", {"op": "lt", "value": 2024}) == "c.year < 2024"
    assert render_filter(c, "country", "US") == "s.country = 'US'"
    assert render_filter(c, "country", {"op": "ne", "value": "US"}) == "s.country != 'US'"


def test_undeclared_or_malicious_operator_is_rejected():
    """未在白名单里的算子是**硬拒绝**，绝不拼进 SQL（算子和取值一样是注入面）。"""
    c = _cfg()
    for op in ("like", "1=1 OR 1=1 --", "'; DROP TABLE singer; --"):
        with pytest.raises(CompileError, match="不支持算子"):
            render_filter(c, "country", {"op": op, "value": "US"})


def test_list_value_renders_as_in_and_rejects_eq_operator():
    c = _cfg()
    assert render_filter(c, "country", ["US", "UK"]) == "s.country IN ('US','UK')"
    # 模板没有 {op} 却给了比较算子 → 运算符无处安放，必须拒绝而不是丢弃
    cfg2 = load_config()
    with pytest.raises(CompileError):
        render_filter(cfg2, "state", {"op": "lt", "value": "SP"})


def test_between_needs_exactly_two_bounds():
    c = _cfg()
    assert render_filter(c, "year", {"op": "between", "value": [2024, 2025]}) == \
        "c.year BETWEEN 2024 AND 2025"
    with pytest.raises(CompileError, match="两个边界值"):
        render_filter(c, "year", {"op": "between", "value": [2024]})


def test_operator_value_is_still_escaped_as_literal():
    """换算子不等于换安全模型：取值仍走字面量转义。"""
    c = _cfg()
    out = render_filter(c, "country", {"op": "ne", "value": "US' OR '1'='1"})
    assert out == "s.country != 'US'' OR ''1''=''1'"


# ---------------------------------------------------------------- HAVING

def test_having_compiles_and_requires_aggregate_metric():
    c = _cfg()
    cq = compile_spec(c, QuerySpec(metric="concert_count", dims=["name"],
                                  having=[("gt", 1)]))
    assert "HAVING COUNT(*) > 1" in " ".join(cq.sql.split())
    # 非聚合指标 + HAVING → 拒绝（SQL 里 HAVING 不能引用非聚合表达式）
    bad = BusinessConfig(metrics={**c.metrics,
                                 "plain": Metric(key="plain", name="非聚合", desc="",
                                                 metric_expr="s.singer_id",
                                                 from_clause="FROM singer s", where_core="1=1",
                                                 support_dims=[], support_filters=[])},
                       dimensions=c.dimensions, filter_templates=c.filter_templates,
                       filter_ops=c.filter_ops, metric_ops=c.metric_ops)
    with pytest.raises(CompileError, match="聚合"):
        compile_spec(bad, QuerySpec(metric="plain", dims=[], having=[("gt", 1)]))


def test_having_operator_whitelist_and_derived_rejection():
    c = _cfg()
    with pytest.raises(CompileError, match="HAVING 不支持算子"):
        compile_spec(c, QuerySpec(metric="concert_count", dims=["name"], having=[("like", 1)]))
    c2 = dict(c.derived_metrics) if isinstance(c.derived_metrics, dict) else {}
    cfg2 = load_config()
    assert parse_derived("ratio@freight_cost/gmv")
    with pytest.raises(CompileError, match="派生指标"):
        compile_spec(cfg2, QuerySpec(metric="ratio@freight_cost/gmv", dims=["state"],
                                     having=[("gt", 1)]))


# ---------------------------------------------------------------- 子查询过滤模板

def test_subquery_filter_template_needs_no_join_injection():
    """模板自带子查询时，其内部别名不应被当成"需要注入的外部别名"。"""
    c = _cfg()
    # 只应报出**外部**别名 `s`；模板自带的 `c2` 不能要求注入
    assert aliases_needed_for_filters(c, ["year_not"]) == {"s"}
    cq = compile_spec(c, QuerySpec(metric="singer_count", filters=[("year_not", 2024)]))
    assert "NOT IN (SELECT c2.singer_id FROM concert c2 WHERE c2.year = 2024)" in cq.sql


def test_anti_join_negation_end_to_end(mini_db):
    """端到端：否定形态必须返回**补集**，且与手写 SQL 完全一致。"""
    c = _cfg()
    cq = compile_spec(c, QuerySpec(metric="singer_count", filters=[("year_not", 2024)]))
    conn = sqlite3.connect(mini_db)
    try:
        got = conn.execute(cq.sql).fetchall()
        want = conn.execute("SELECT COUNT(*) FROM singer WHERE singer_id NOT IN "
                            "(SELECT singer_id FROM concert WHERE year = 2024)").fetchall()
    finally:
        conn.close()
    assert got == want == [(1,)]        # Alice/Bob 都参加过 2024，只剩 Cindy


def test_having_end_to_end(mini_db):
    c = _cfg()
    cq = compile_spec(c, QuerySpec(metric="concert_count", dims=["name"], having=[("gt", 1)]))
    conn = sqlite3.connect(mini_db)
    try:
        got = sorted(conn.execute(cq.sql).fetchall())
        want = sorted(conn.execute("SELECT COUNT(*), s.name FROM singer s "
                                   "JOIN concert c ON c.singer_id=s.singer_id "
                                   "GROUP BY s.name HAVING COUNT(*) > 1").fetchall())
    finally:
        conn.close()
    assert got == want == [(2, "Alice")]


def test_operator_filter_end_to_end(mini_db):
    c = _cfg()
    cq = compile_spec(c, QuerySpec(metric="singer_count", filters=[("country", ["US", "UK"])]))
    conn = sqlite3.connect(mini_db)
    try:
        assert conn.execute(cq.sql).fetchall() == [(3,)]
    finally:
        conn.close()


# ---------------------------------------------------------------- 匹配器

def test_multi_value_becomes_in_only_when_declared_or():
    c = _cfg()
    r = mm.match("how many singers in US and UK", c)
    assert r.matched and ("country", ["US", "UK"]) in r.filters


def test_multi_value_and_semantics_is_refused():
    """`multi: and`（如 language：一个国家多条语言行）→ 拒答，绝不按"或"作答。"""
    cfg = load_config()
    fake = BusinessConfig(
        metrics=cfg.metrics, dimensions=cfg.dimensions,
        filter_templates={"country": "s.country {op} {value}"},
        filter_ops={"country": {"eq": "=", "in": "IN"}},
        matcher_keywords={"metrics": {"gmv": ["gmv"]}, "dimensions": {},
                          "filter_values": {"country": ["US", "UK"]},
                          "filter_multi": {"country": "and"}},
    )
    r = mm.match("gmv in US and UK", fake)
    assert r.matched is False and "多个取值" in "".join(r.reject_reasons)


def test_at_least_maps_to_gte_not_gt():
    """`at least N` 是 >= N。写成 > N 会漏掉恰好等于 N 的组（外部基准上的 off-by-one）。"""
    c = _cfg()
    r = mm.match("how many concerts per singer with at least 2 concerts", c)
    assert r.matched and r.having == [("gte", 2)]
    r2 = mm.match("how many concerts per singer with more than 2 concerts", c)
    assert r2.having == [("gt", 2)]


def test_numeric_comparison_extracted_and_consumed():
    c = _cfg()
    r = mm.match("how many singers with concerts before 2024", c)
    assert r.matched and ("year", {"op": "lt", "value": 2024}) in r.filters
    r2 = mm.match("how many singers with concerts between 2024 and 2025", c)
    assert ("year", {"op": "between", "value": [2024, 2025]}) in r2.filters


def test_unconsumed_comparison_cue_still_refuses():
    """线索没有被表达成过滤条件 → 仍必须拒答（否则条件被静默忽略）。"""
    c = _cfg()
    r = mm.match("how many singers with more than the average number of concerts", c)
    assert r.matched is False
    assert "没有被解析" in "".join(r.reject_reasons) or "比较" in "".join(r.reject_reasons)


def test_negation_switches_to_declared_variant():
    c = _cfg()
    r = mm.match("how many singers that did not perform in US concerts", c)
    assert r.matched and ("year_not", "US") in r.filters


def test_negation_without_declared_variant_refuses():
    """配置里没有对应否定形态 → 拒答（不猜、不按正面版本执行）。"""
    cfg = load_config()
    fake = BusinessConfig(
        metrics=cfg.metrics, dimensions=cfg.dimensions,
        filter_templates={"state": "c.customer_state {op} {value}"},
        filter_ops={"state": {"eq": "="}},
        matcher_keywords={"metrics": {"gmv": ["gmv"]}, "dimensions": {},
                          "filter_values": {"state": ["SP"]}},
    )
    r = mm.match("gmv of customers not in SP", fake)
    assert r.matched is False
    assert "否定" in "".join(r.reject_reasons)


def test_cross_metric_having_is_refused():
    """组内筛选与返回指标不是同一个 → 拒答（否则筛选错位）。"""
    c = _cfg()
    r = mm.match("how many singers and how many concerts per singer with more than 2 concerts", c)
    assert r.matched is False
