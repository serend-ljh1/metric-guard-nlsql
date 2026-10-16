"""语义层关系建模配置化测试：JOIN 池 / 基数 / 依赖声明在 business_config.yaml。

回归目标：维度/过滤所需的表不再由代码里的**硬编码池**决定，而由配置 `relations` 段声明；
换域只改 YAML、不改代码。三条断言对应三种失败形态：
  - 配置声明的依赖（p 依赖 oi）必须被按依赖补齐，而不是报缺表；
  - 配置**未声明**的关系被要求注入时，明确报错并指向配置（历史缺陷：静默丢表 → 执行期报错）；
  - 一份自定义的 `relations` 能真正驱动注入（证明是配置在起作用，不是代码兜底）。
"""
from __future__ import annotations

import pytest

from sqlpa.business.compiler import QuerySpec, compile_spec
from sqlpa.business.metric_config import (
    BusinessConfig, Dimension, Metric, load_config,
)
from sqlpa.business.sqlgen import JoinInjectionError, inject_dim_joins, relation_pool


def test_olist_relations_declared_and_match_pool():
    """Olist 域的 relations 必须完整声明四条关系，且基数/依赖是**配置里的语义**。"""
    cfg = load_config()
    pool, deps = relation_pool(cfg)
    assert set(pool) == {"c", "oi", "p", "r"}
    assert pool["c"] == ("customers",
                         "LEFT JOIN customers c ON o.customer_id=c.customer_id", "state")
    assert deps == {"p": ("oi",)}
    assert cfg.relations["p"]["cardinality"] == "many_to_one"
    assert cfg.relations["oi"]["cardinality"] == "one_to_many"
    assert cfg.relations["r"]["cardinality"] == "one_to_one"


def test_dependency_injected_from_config():
    """category 维度需要 p，而 p 依赖 oi —— 二者都按配置声明被注入，且 oi 在前。"""
    cfg = load_config()
    cq = compile_spec(cfg, QuerySpec(metric="gmv", dims=["category"]))
    flat = " ".join(cq.sql.split())
    assert "JOIN order_items oi" in flat and "JOIN products p" in flat
    assert flat.index("order_items oi") < flat.index("products p")


def test_undeclared_relation_refuses_with_config_hint():
    """配置未声明的关系 → 明确报错并指向 `relations`（不再静默丢表）。"""
    cfg = load_config()
    del cfg.relations["c"]          # 抽掉客户关系
    with pytest.raises(JoinInjectionError, match="relations"):
        inject_dim_joins(cfg, cfg.metrics["gmv"], ["state"], [])


def test_custom_relations_drive_injection():
    """一份全新域的 relations 能驱动注入 —— 证明注入由配置决定，而非代码兜底。"""
    cfg = BusinessConfig(
        metrics={"sales_count": Metric(
            key="sales_count", name="销量", desc="", metric_expr="COUNT(*)",
            from_clause="FROM fact f", where_core="1=1",
            support_dims=["shop_name"], support_filters=[])},
        dimensions={"shop_name": Dimension("shop_name", "门店", "d.name")},
        relations={"d": {"alias": "d", "table": "dim_shop",
                         "join": "LEFT JOIN dim_shop d ON f.shop_id=d.shop_id",
                         "serves": ["shop_name"], "cardinality": "many_to_one"}},
    )
    cq = compile_spec(cfg, QuerySpec(metric="sales_count", dims=["shop_name"]))
    assert "LEFT JOIN dim_shop d ON f.shop_id=d.shop_id" in cq.sql
    assert "d.name AS shop_name" in cq.sql
