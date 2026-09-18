"""
sqlpa.business.sqlgen
=====================
语义层共用的 SQL 生成工具：**维度所需的 JOIN 注入**。

为什么单独成模块：编译（compiler.py）与异常归因（attribution.py）都需要
"按所需维度补齐 JOIN"。此前编译器里实现了、归因里没有，于是归因按 category
拆解时会生成一条引用了不存在表别名（p.）的 SQL，被 except 吞掉后表现为
"该维度没波动"——**静默错误最危险**。抽成共享模块后不会再有第二处遗漏。
"""
from __future__ import annotations

import re
from typing import Dict, List, Sequence, Set, Tuple

_SQL_KEYWORDS = {
    "on", "where", "left", "right", "inner", "outer", "join", "as", "and", "or",
    "group", "order", "by", "having", "limit", "using", "cross", "full", "select",
    "from", "distinct", "case", "when", "then", "else", "end", "where",
}

# 别名 -> (表名, JOIN 子句, 该别名服务于哪个维度族)
DIM_JOIN_POOL: Dict[str, Tuple[str, str, str]] = {
    "c": ("customers", "LEFT JOIN customers c ON o.customer_id=c.customer_id", "state"),
    "p": ("products", "LEFT JOIN products p ON oi.product_id=p.product_id", "category"),
    "r": ("reviews", "LEFT JOIN reviews r ON o.order_id=r.order_id", "review"),
}


def present_names(from_clause: str, join_clause: str) -> Set[str]:
    """收集 SQL 里已可用的标识符：**表名与别名都算**。

    不能只收表名：JOIN 池按**别名**（c/p/r）索引，只拿表名判断会既漏判又重复注入。
    """
    text = f"{from_clause or ''} {join_clause or ''}"
    names: Set[str] = set()
    for m in re.finditer(
            r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)(?:\s+(?:AS\s+)?([a-zA-Z_][\w]*))?", text, re.I):
        table, alias = m.group(1), m.group(2)
        if table:
            names.add(table.lower())
        if alias and alias.lower() not in _SQL_KEYWORDS:
            names.add(alias.lower())
    return names


def aliases_needed_for_dims(cfg, dim_keys: Sequence[str]) -> Set[str]:
    """某个维度清单需要在 SQL 里出现的表别名。"""
    needed: Set[str] = set()
    for d in dim_keys:
        dim = cfg.dimensions.get(d)
        frag = getattr(dim, "sql_fragment", "") if dim else ""
        for mm in re.finditer(r"\b([a-zA-Z_][\w]*)\.", frag):
            needed.add(mm.group(1).lower())
    return needed


def inject_dim_joins(cfg, metric, dim_keys: Sequence[str]) -> str:
    """返回"指标自带 JOIN + 维度所需 JOIN"的完整 join_clause（去重）。"""
    from_clause = metric.from_clause or ""
    base_join = (getattr(metric, "join_clause", "") or "").strip()
    present = present_names(from_clause, base_join)
    joins: List[str] = [base_join] if base_join else []
    for alias in sorted(aliases_needed_for_dims(cfg, dim_keys)):
        if alias in present:
            continue
        spec = DIM_JOIN_POOL.get(alias)
        if not spec:
            continue
        table, clause, _src = spec
        # products 依赖 oi 别名；没有 oi 就无法注入该维度
        if alias == "p" and "oi" not in present:
            continue
        joins.append(clause)
        present.add(alias)
        present.add(table)
    return " ".join(j for j in joins if j)


def metric_source(cfg, metric, dim_keys: Sequence[str] = ()) -> str:
    """指标的数据来源 = from_clause + 维度所需 JOIN。归因/编译统一走这里。"""
    jc = inject_dim_joins(cfg, metric, dim_keys)
    parts = [metric.from_clause or ""]
    if jc:
        parts.append(jc)
    return "\n".join(p for p in parts if p)


def tables_of(metric) -> List[str]:
    """指标用到的表名（产品上展示"数据来源"）。"""
    text = f"{metric.from_clause or ''} {getattr(metric, 'join_clause', '') or ''}"
    return list(dict.fromkeys(re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", text, re.I)))
