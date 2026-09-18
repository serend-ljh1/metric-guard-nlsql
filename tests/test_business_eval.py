"""业务语义层评测回归测试（离线、确定性）。

锁定 `evaluation/eval_business.py` 依赖的核心不变量。
这些是"语义层 + 治理"这条主线的护栏，此前**完全没有测试覆盖**：
口径命中、口径外拒绝、公式防篡改、权限拦截、PII 掩码、审计留痕。
"""
from __future__ import annotations

import pytest

from sqlpa.business.metric_config import load_config
from sqlpa.business.metric_guard import formula_of, verify_formula
from sqlpa.business.permissions import check_access, mask_result
from sqlpa.business.service import answer
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

CFG = load_config()
DB = "data/olist_sample/sample.db"


@pytest.fixture
def sb(sample_db):
    return SqlSandbox(sample_db, ExecConfig.from_settings(max_rows=200))


@pytest.fixture
def schema(sample_db):
    from sqlpa.data.schema_extractor import extract_from_sqlite
    return extract_from_sqlite(sample_db, "olist").to_dict()


# ---------------- 口径内：识别正确 + 真的执行出结果 ----------------

@pytest.mark.parametrize("question,metric,dims", [
    ("各个品类的GMV", "gmv", ["category"]),
    ("每个州的GMV", "gmv", ["state"]),
    ("订单数是多少", "order_count", []),
    ("每个品类的订单数", "order_count", ["category"]),
    ("客单价是多少", "aov", []),
    ("每个州的取消率", "cancellation_rate", ["state"]),
    ("平均评分", "avg_review", []),
])
def test_in_scope_questions_are_matched_and_executed(sb, sample_db, question, metric, dims):
    a = answer(question, CFG, sb, sample_db, llm=None, role="analyst", username="t")
    assert a["matched"] is True, f"{question} 未被识别为口径内"
    assert a["metric"] == metric
    assert set(a["dims"] or []) == set(dims)
    assert a["ok"] is True, f"{question} 未执行成功"
    assert a["rows"], f"{question} 没查出任何数据"


# ---------------- 口径外：必须拒绝（不硬生成）----------------

@pytest.mark.parametrize("question", [
    "客单价按品类",          # aov 不支持 category
    "超时送达率按品类",       # late_delivery_rate 不支持 category
    "每个客服的响应时长是多少",  # 配置里没有这类指标（语义层已覆盖"平均运费"等）
    "今天天气怎么样",         # 完全无关
])
def test_out_of_scope_questions_are_rejected(sb, sample_db, question):
    a = answer(question, CFG, sb, sample_db, llm=None, role="analyst", username="t")
    assert a["matched"] is False, f"{question} 本应被拒（口径外）"
    assert a["ok"] is False
    assert a["reject"], f"{question} 被拒但没有给出原因"
    assert len(a["reject"]) >= 8, "拒绝原因过短，业务人员无法据此调整"


# ---------------- 公式防篡改：能拦什么、拦不住什么（如实锁定）----------------

def test_formula_passes_when_used_verbatim():
    expr = formula_of(CFG, "gmv")
    assert verify_formula(f"SELECT {expr} FROM orders o", expr) == []


def test_formula_blocks_replaced_metric():
    expr = formula_of(CFG, "gmv")
    assert verify_formula("SELECT COUNT(*) FROM orders", expr), "换口径未被拦截"


def test_formula_blocks_shell_query():
    expr = formula_of(CFG, "gmv")
    assert verify_formula("SELECT 1 FROM orders WHERE 1=0", expr), "空壳 SQL 未被拦截"


@pytest.mark.xfail(
    reason="已知局限：verify_formula 是子串包含判定，保留 SUM(oi.price) 再乘系数即可绕过。"
           "修复（改为表达式结构校验）后本用例应转为通过。",
    strict=False,
)
def test_formula_blocks_scaled_expression_BYPASS_KNOWN():
    """把配置公式乘上系数——当前**拦不住**，如实记录为已知缺口。"""
    expr = formula_of(CFG, "gmv")
    sql = "SELECT SUM(oi.price)*0.5 FROM orders o JOIN order_items oi ON o.order_id=oi.order_id"
    assert verify_formula(sql, expr), "私自缩放公式未被拦截"


# ---------------- 权限拦截 ----------------

def test_unqualified_sensitive_column_blocked(schema):
    bad = check_access("analyst", CFG.permissions,
                       "SELECT customer_zip_code_prefix FROM customers", schema=schema)
    assert bad, "非限定敏感列绕过了列白名单"


def test_aliased_sensitive_column_blocked(schema):
    bad = check_access("analyst", CFG.permissions,
                       "SELECT customer_zip_code_prefix AS zip FROM customers", schema=schema)
    assert bad, "别名改写绕过了列白名单"


def test_allowed_column_passes(schema):
    assert check_access("analyst", CFG.permissions,
                        "SELECT customer_city FROM customers", schema=schema) == []


def test_admin_passes_everything(schema):
    assert check_access("admin", CFG.permissions,
                        "SELECT customer_zip_code_prefix FROM customers", schema=schema) == []


# ---------------- PII 掩码 ----------------

def test_masking_covers_alias_rewrite(schema):
    sql = "SELECT customer_zip_code_prefix AS zip FROM customers"
    out = mask_result(["zip"], [("14409",)], CFG.permissions.get("sensitive_columns", {}),
                      sql=sql, perms=CFG.permissions, schema=schema)
    assert out[0][0] != "14409", "别名后未掩码"


def test_masking_leaves_normal_columns_alone(schema):
    sql = "SELECT customer_city FROM customers"
    out = mask_result(["customer_city"], [("sao paulo",)],
                      CFG.permissions.get("sensitive_columns", {}),
                      sql=sql, perms=CFG.permissions, schema=schema)
    assert out[0][0] == "sao paulo"


# ---------------- 审计留痕 ----------------

def test_every_query_is_audited(sb, sample_db):
    from sqlpa.business import storage
    q = "各个品类的GMV"
    answer(q, CFG, sb, sample_db, llm=None, role="analyst", username="audit_t")
    rows = storage.list_audit(limit=200)
    assert any(r.get("user_input") == q for r in rows), "取数未写入审计"
