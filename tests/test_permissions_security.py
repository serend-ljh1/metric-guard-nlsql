"""权限与 PII 掩码安全测试（回归）。

锁定三个历史缺口：
  1) 非限定列名绕过列白名单：`SELECT customer_zip_code_prefix FROM customers`
  2) 别名绕过结果掩码：`SELECT customer_zip_code_prefix AS zip FROM customers`
  3) API 角色越权：`/api/query` 曾无鉴权且 role 直接取自请求体
"""
from __future__ import annotations

import pytest

from sqlpa.business.metric_config import load_config
from sqlpa.business.permissions import (
    check_access,
    mask_result,
    sensitive_output_positions,
)

CFG = load_config()
PERMS = CFG.permissions
SENSITIVE = PERMS.get("sensitive_columns", {})

# 与 Olist 业务库同构的 schema（列改名/别名都在这套列上验证）
SCHEMA = {
    "db_id": "olist",
    "tables": [
        {"name": "customers", "columns": [
            {"name": "customer_id"}, {"name": "customer_unique_id"},
            {"name": "customer_zip_code_prefix"}, {"name": "customer_city"},
            {"name": "customer_state"}, {"name": "customer_phone"},
        ]},
        {"name": "orders", "columns": [
            {"name": "order_id"}, {"name": "customer_id"}, {"name": "order_status"},
        ]},
    ],
}


# ---------------- 1) 非限定列名不得绕过列白名单 ----------------

def test_qualified_sensitive_column_is_blocked():
    """带表别名的受限列必须被拦（原有正确行为）。"""
    bad = check_access("analyst", PERMS,
                       "SELECT c.customer_phone FROM customers c", schema=SCHEMA)
    assert bad, "限定形式的受限列应被拦截"


def test_unqualified_sensitive_column_is_blocked():
    """**修复点**：不带别名的受限列也必须被拦，否则可绕过列白名单。"""
    bad = check_access("analyst", PERMS,
                       "SELECT customer_phone FROM customers", schema=SCHEMA)
    assert bad, "非限定列名 customer_phone 绕过了列白名单（原缺陷）"


def test_unqualified_allowed_column_passes():
    """允许的列（不在受限名单内）不应被误拦。"""
    bad = check_access("analyst", PERMS,
                       "SELECT customer_city FROM customers", schema=SCHEMA)
    assert not bad, f"允许列被误拦: {bad}"


def test_admin_role_is_unrestricted():
    assert check_access("admin", PERMS,
                        "SELECT customer_phone FROM customers", schema=SCHEMA) == []


def test_unknown_role_is_denied():
    bad = check_access("nobody", PERMS, "SELECT customer_city FROM customers", schema=SCHEMA)
    assert bad and "未配置角色" in bad[0]


# ---------------- 2) 别名不得绕过掩码 ----------------

def test_alias_does_not_bypass_masking():
    """**修复点**：AS 改名后仍须掩码（按列来源判定，而不是按输出列名）。"""
    sql = "SELECT customer_zip_code_prefix AS zip FROM customers"
    rows = [("14409",), ("09790",)]
    out = mask_result(["zip"], rows, SENSITIVE, sql=sql, perms=PERMS, schema=SCHEMA)
    assert out[0][0] != "14409", "别名 zip 让原始邮编原样返回（原缺陷）"
    assert "****" in str(out[0][0]) or out[0][0] == "***"


def test_plain_column_is_still_masked():
    sql = "SELECT customer_zip_code_prefix FROM customers"
    out = mask_result(["customer_zip_code_prefix"], [("14409",)], SENSITIVE,
                      sql=sql, perms=PERMS, schema=SCHEMA)
    assert out[0][0] != "14409"


def test_non_sensitive_column_is_not_masked():
    sql = "SELECT customer_city FROM customers"
    out = mask_result(["customer_city"], [("sao paulo",)], SENSITIVE,
                      sql=sql, perms=PERMS, schema=SCHEMA)
    assert out[0][0] == "sao paulo", "非敏感列被误掩码"


def test_backward_compatible_name_based_masking():
    """不传 sql 时保持原有按列名掩码的行为（向后兼容）。"""
    out = mask_result(["customer_zip_code_prefix"], [("14409",)], SENSITIVE)
    assert out[0][0] != "14409"


def test_sensitive_positions_with_mixed_select():
    sql = ("SELECT c.customer_city, c.customer_zip_code_prefix AS zip, o.order_status "
           "FROM customers c JOIN orders o ON o.customer_id = c.customer_id")
    pos = sensitive_output_positions(sql, ["customer_city", "zip", "order_status"],
                                     PERMS, schema=SCHEMA)
    assert pos == {1}, f"应只掩第 2 列（索引1），实际 {pos}"


# ---------------- 3) API 角色不得来自请求体 ----------------

def test_api_role_ignores_request_body_when_no_tokens(monkeypatch):
    """未配置 token 时：忽略请求体 role，按最小权限默认角色执行。"""
    monkeypatch.delenv("SQLPA_API_TOKENS", raising=False)
    monkeypatch.delenv("SQLPA_DEFAULT_ROLE", raising=False)
    import api
    role = api._resolve_role(None, "admin")      # 请求体自称 admin
    assert role == "analyst", f"请求体 role 竟然生效了：{role}"


def test_api_requires_token_when_configured(monkeypatch):
    monkeypatch.setenv("SQLPA_API_TOKENS", "tok-admin:admin,tok-analyst:analyst")
    import api
    from fastapi import HTTPException
    # 无 token → 401
    with pytest.raises(HTTPException) as e1:
        api._resolve_role(None, "admin")
    assert e1.value.status_code == 401
    # 错误 token → 401
    with pytest.raises(HTTPException):
        api._resolve_role("wrong", "admin")
    # 正确 token → 角色取自 token，而不是请求体
    assert api._resolve_role("tok-analyst", "admin") == "analyst"
    assert api._resolve_role("tok-admin", "analyst") == "admin"


def test_api_query_endpoint_cannot_escalate_via_body(monkeypatch, sample_db):
    """端到端：请求体传 admin，也必须按 analyst 的权限执行。"""
    monkeypatch.delenv("SQLPA_API_TOKENS", raising=False)
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    import api
    from fastapi.testclient import TestClient
    c = TestClient(api.app)
    r = c.post("/api/query", json={"question": "每个客户的电话是多少", "role": "admin"})
    assert r.status_code == 200
    assert r.json().get("effective_role") == "analyst"
