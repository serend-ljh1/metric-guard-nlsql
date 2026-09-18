"""语义层编译器测试（离线、确定性）。

锁定"架构反转"的核心契约：命中语义层时 SQL 由**代码确定性编译**，
并且配置里声明支持的维度/派生指标都必须真能编译出可执行 SQL。
"""
from __future__ import annotations

import pytest

from sqlpa.business.compiler import (
    CompileError,
    QuerySpec,
    compile_spec,
    dimension_sql,
    parse_derived,
    resolve_time_range,
)
from sqlpa.business.metric_config import load_config
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

CFG = load_config()
DB = "data/olist_sample/sample.db"


@pytest.fixture
def sb(sample_db):
    return SqlSandbox(sample_db, ExecConfig.from_settings(max_rows=200))


# ---------------- 基础编译 ----------------

def test_basic_compile_uses_config_formula():
    cq = compile_spec(CFG, QuerySpec(metric="gmv", dims=["category"]))
    assert CFG.metrics["gmv"].metric_expr in cq.sql
    assert cq.certified is True
    assert "order_items" in cq.sources


def test_compile_rejects_unknown_metric():
    with pytest.raises(CompileError):
        compile_spec(CFG, QuerySpec(metric="no_such_metric"))


def test_compile_rejects_unknown_dim():
    with pytest.raises(CompileError):
        compile_spec(CFG, QuerySpec(metric="gmv", dims=["no_such_dim"]))


# ---------------- 维度所需 JOIN 自动注入 ----------------

def test_dim_join_injected_for_category():
    """gmv 自身不带 products JOIN，按 category 分组时须自动注入。"""
    cq = compile_spec(CFG, QuerySpec(metric="gmv", dims=["category"]))
    assert "products p" in cq.sql


def test_dim_join_injected_for_state():
    cq = compile_spec(CFG, QuerySpec(metric="gmv", dims=["state"]))
    assert "customers c" in cq.sql


def test_no_duplicate_join_injection():
    """指标已自带 customers JOIN 时不得重复注入（否则列名歧义）。"""
    cq = compile_spec(CFG, QuerySpec(metric="customer_count", dims=["state"]))
    assert cq.sql.count("JOIN customers c") == 1


# ---------------- 全组合可执行性（配置声明必须与可执行一致）----------------

def test_every_supported_metric_dim_combo_executes(sb):
    """配置里声明支持的每个「指标×维度」都必须能编译并执行成功。"""
    failures = []
    for key, m in CFG.metrics.items():
        for dims in [[]] + [[d] for d in m.support_dims]:
            try:
                cq = compile_spec(CFG, QuerySpec(metric=key, dims=dims))
                r = sb.execute(cq.sql)
                if not r.ok:
                    failures.append((key, dims, r.error or r.reason))
            except Exception as e:  # noqa: BLE001
                failures.append((key, dims, f"{type(e).__name__}: {e}"))
    assert not failures, f"以下组合无法执行：{failures[:8]}"


# ---------------- 时间粒度 ----------------

@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter"])
def test_time_grains_compile_and_execute(sb, grain):
    cq = compile_spec(CFG, QuerySpec(metric="gmv", dims=["dt"], time_grain=grain))
    assert sb.execute(cq.sql).ok


def test_unknown_time_grain_rejected():
    with pytest.raises(CompileError):
        compile_spec(CFG, QuerySpec(metric="gmv", dims=["dt"], time_grain="hourly"))


def test_dimension_sql_grain_only_applies_to_dt():
    day = dimension_sql(CFG, "dt", "day")
    month = dimension_sql(CFG, "dt", "month")
    assert day != month
    assert "strftime" in month


# ---------------- 派生指标 ----------------

def test_parse_derived_ratio_and_share():
    assert parse_derived("ratio@a/b") == {"kind": "ratio", "numerator": "a", "denominator": "b"}
    assert parse_derived("share@a") == {"kind": "share", "base": "a"}
    assert parse_derived("gmv") is None


def test_ratio_compiles_and_executes(sb):
    cq = compile_spec(CFG, QuerySpec(metric="ratio@freight_cost/gmv", dims=["state"]))
    r = sb.execute(cq.sql)
    assert r.ok and r.rows
    assert cq.derived["kind"] == "ratio"


def test_ratio_merges_joins_across_metrics(sb):
    """分子分母各自的 JOIN 会被合并，因此同主表、不同 JOIN 的指标也能相除。

    （早前以为"来源不同就该拒绝"，实测两个指标都以 orders 为主表，合并 JOIN 后
    是合法查询——拒绝反而会误伤合理用法。）
    """
    cq = compile_spec(CFG, QuerySpec(metric="ratio@avg_review/gmv"))
    assert "reviews r" in cq.sql and "order_items oi" in cq.sql
    assert sb.execute(cq.sql).ok


def test_ratio_rejects_different_main_table():
    """主表不同的指标无法在同一查询内相除 —— 必须明确拒绝，不能静默算错。"""
    from dataclasses import replace

    fake = replace(CFG.metrics["gmv"], from_clause="FROM order_items oi2")
    cfg2 = load_config()
    cfg2.metrics["fake_metric"] = fake
    with pytest.raises(CompileError):
        compile_spec(cfg2, QuerySpec(metric="ratio@fake_metric/gmv"))


def test_share_requires_dimension():
    with pytest.raises(CompileError):
        compile_spec(CFG, QuerySpec(metric="share@canceled_order_count", dims=[]))


def test_share_compiles_and_executes(sb):
    cq = compile_spec(CFG, QuerySpec(metric="share@canceled_order_count", dims=["state"]))
    assert sb.execute(cq.sql).ok


# ---------------- 过滤与时间范围 ----------------

def test_time_range_filter_renders_sql():
    cq = compile_spec(CFG, QuerySpec(metric="gmv", dims=[],
                                     filters=[("time_range", "本月")]))
    assert "order_purchase_timestamp >=" in cq.sql


def test_resolve_time_range_variants():
    s1, e1 = resolve_time_range("2026-08")
    assert s1 == "'2026-08-01'" and e1 == "'2026-09-01'"
    s2, e2 = resolve_time_range("最近7天")
    assert s2.startswith("'") and e2.startswith("'")


def test_unknown_filter_type_rejected():
    with pytest.raises(CompileError):
        compile_spec(CFG, QuerySpec(metric="gmv", filters=[("no_such_filter", "x")]))


# ---------------- 编译产物可追溯（产品要展示给人看）----------------

def test_compiled_query_exposes_provenance():
    cq = compile_spec(CFG, QuerySpec(metric="gmv", dims=["category"]))
    d = cq.to_dict()
    for k in ("sql", "metric", "metric_name", "metric_expr", "dims", "sources",
              "certified", "owner", "version"):
        assert k in d, f"编译产物缺少 {k}"
    assert d["owner"] and d["version"]      # 口径可追溯到负责人与版本
