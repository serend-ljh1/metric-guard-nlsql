"""行级权限（RLS-lite）回归。

规则：行级过滤**只能收紧、不能失效**。谓词引用的表不在该指标取数范围时，
必须编译期拒绝，而不是"过滤没生效但照样返回全量"。此前系统只做到表/列白名单，
做不到"某商家只能看自己的订单"。
"""
from __future__ import annotations

import sqlite3

import pytest

from sqlpa.business.compiler import CompileError, QuerySpec, compile_spec
from sqlpa.business.metric_config import load_config
from sqlpa.business.permissions import missing_row_filter_aliases, row_filter_clauses
from sqlpa.business.service import answer
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

CFG = load_config()
SELLER_FILTER = "oi.seller_id = '6560211a19b47992c3666cc44a7e94c0'"


# ---------------------------------------------------------------- 配置读取

def test_row_filter_clauses_reads_role_config():
    assert row_filter_clauses("seller", CFG.permissions) == [SELLER_FILTER]
    assert row_filter_clauses("analyst", CFG.permissions) == []
    assert row_filter_clauses("admin", CFG.permissions) == []
    assert row_filter_clauses("nobody", CFG.permissions) == []


def test_missing_alias_detection():
    sql = "SELECT SUM(oi.price) FROM orders o JOIN order_items oi ON o.order_id=oi.order_id"
    assert missing_row_filter_aliases(sql, [SELLER_FILTER]) == []
    assert missing_row_filter_aliases(sql, ["c.customer_state = 'SP'"]) == ["c"]


# ---------------------------------------------------------------- 注入与拒绝

def test_row_filter_is_injected_into_compiled_sql(tmpdir_clean, sample_db):
    cq = compile_spec(CFG, QuerySpec(metric="gmv", dims=["state"]),
                      row_filters=[SELLER_FILTER])
    assert "oi.seller_id" in cq.sql
    sb = SqlSandbox(sample_db, ExecConfig(max_rows=2000))
    assert sb.execute(cq.sql).ok
    # 未带过滤的同一指标（对照）
    plain = compile_spec(CFG, QuerySpec(metric="gmv", dims=["state"]))
    assert "oi.seller_id" not in plain.sql


def test_row_filter_fails_closed_when_table_absent():
    """谓词引用的表不在该指标取数范围 → 编译期拒绝（否则等于没有行级权限）。"""
    with pytest.raises(CompileError) as e:
        compile_spec(CFG, QuerySpec(metric="cancellation_rate", dims=[]),
                     row_filters=[SELLER_FILTER])
    assert "行级权限无法生效" in str(e.value)


def test_row_filter_applies_to_share_subquery():
    """share 的分母子查询也必须带行级过滤，否则占比会拿全量当分母。"""
    cq = compile_spec(CFG, QuerySpec(metric="share@canceled_order_count", dims=["state"]),
                      row_filters=[SELLER_FILTER])
    assert cq.sql.count("oi.seller_id") >= 2, cq.sql


# ---------------------------------------------------------------- 端到端

def test_seller_role_sees_only_own_rows(sample_db):
    """同一问题、同一库，seller 角色拿到的 GMV 必须小于 analyst（行级过滤真的生效）。

    说明：过滤用的 seller_id 从**测试切片库**里动态取（样本库只含最早 1200 单，
    写死 Olist 头部商家会命中 0 行，那是切片问题而非权限问题）。
    """
    from dataclasses import replace

    conn = sqlite3.connect(sample_db)
    sellers = [r[0] for r in conn.execute(
        "SELECT seller_id FROM order_items GROUP BY seller_id "
        "ORDER BY COUNT(*) DESC").fetchall()]
    assert sellers, "样本库应含 order_items"
    flt = f"oi.seller_id = '{sellers[0]}'"
    perms = {**CFG.permissions, "roles": {**CFG.permissions["roles"]}}
    perms["roles"]["seller"] = {**CFG.permissions["roles"]["seller"], "row_filters": [flt]}
    cfg2 = replace(CFG, permissions=perms)

    sb = SqlSandbox(sample_db, ExecConfig(max_rows=2000))
    seller = answer("各个品类的GMV", cfg2, sb, sample_db, llm=None, role="seller",
                    username="s1")
    analyst = answer("各个品类的GMV", CFG, sb, sample_db, llm=None, role="analyst",
                     username="a1")
    assert seller["ok"] and analyst["ok"]
    assert seller["applied_row_filters"] == [flt]
    assert analyst["applied_row_filters"] == []
    s_total = sum(r[0] for r in seller["rows"] if isinstance(r[0], (int, float)))
    a_total = sum(r[0] for r in analyst["rows"] if isinstance(r[0], (int, float)))
    assert s_total > 0, "头部商家在切片里应有数据"
    if len(sellers) > 1:
        assert s_total < a_total, (s_total, a_total)


def test_seller_role_rejected_when_metric_cannot_be_scoped(sample_db):
    """该指标无法施加行级过滤时，seller 角色得到明确拒绝而不是全量数据。"""
    sb = SqlSandbox(sample_db, ExecConfig(max_rows=2000))
    res = answer("取消率是多少", CFG, sb, sample_db, llm=None, role="seller",
                 username="s1")
    assert res["ok"] is False
    assert "行级权限" in (res.get("reject") or "")
