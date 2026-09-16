"""
sqlpa.business.permissions
==========================
业务护栏（P1）：表列权限白名单 + 敏感字段掩码。

职责（在组装SQL之后、执行/返回之前做静态校验）：
  - check_access: 解析 SQL 引用的 表.列，逐条核对角色白名单，未授权直接拦截。
  - mask_result:  对结果中含敏感字段的列做掩码（如 138****1234）。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence


def _alias_map(sql: str) -> Dict[str, str]:
    """from/jon 处把 别名->表名 的映射建出来。 o->orders, oi->order_items ..."""
    mapping: Dict[str, str] = {}
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)(?:\s+(?:AS\s+)?([a-zA-Z_][\w]*))?", sql, re.I):
        table, alias = m.group(1), m.group(2)
        if table.lower() in ("select", "where", "group", "order", "limit", "on", "left", "right", "inner", "join"):
            continue
        mapping[(alias or table).lower()] = table
    return mapping


def _column_refs(sql: str) -> List[tuple]:
    """提取 (别名.列) 引用。"""
    return [(m.group(1), m.group(2)) for m in re.finditer(r"\b([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)", sql)]


def check_access(role: str, perms: Dict, sql: str) -> List[str]:
    """返回未授权访问清单（空=通过）。"""
    acfg = (perms.get("roles") or {}).get(role)
    if not acfg:
        return [f"未配置角色 {role} 的权限"]
    allowed_tables = acfg.get("allowed_tables")
    allowed_cols = acfg.get("allowed_columns", {})
    if allowed_tables == "*" and allowed_cols == "*":
        return []  # 管理员全放行
    amap = _alias_map(sql)
    bad = []
    for alias, col in _column_refs(sql):
        table = amap.get(alias.lower(), alias)
        if allowed_tables != "*" and table not in (allowed_tables or []):
            bad.append(f"角色 {role} 无权访问表 {table}")
            continue
        if allowed_cols != "*":
            cols = allowed_cols.get(table)
            if cols is not None and col not in cols:
                bad.append(f"角色 {role} 无权访问字段 {table}.{col}")
    return bad


def _mask_value(v) -> str:
    if v is None:
        return v
    s = str(v)
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 7:
        return digits[:3] + "****" + digits[-3:]
    return "***"


def mask_result(headers: List[str], rows: Sequence[Sequence],
                sensitive: Dict[str, List[str]]) -> List[list]:
    """按 {表名: [敏感列]} 掩码结果。这里按列名直配（简化：sensitive_columns 为列名集合）。"""
    sen = set()
    for cols in sensitive.values():
        sen.update(cols)
    masked = []
    for row in rows:
        newrow = []
        for h, v in zip(headers, row):
            newrow.append(_mask_value(v) if h in sen else v)
        masked.append(newrow)
    return masked
