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
    "oi": ("order_items", "LEFT JOIN order_items oi ON o.order_id=oi.order_id", "items"),
    "p": ("products", "LEFT JOIN products p ON oi.product_id=p.product_id", "category"),
    "r": ("reviews", "LEFT JOIN reviews r ON o.order_id=r.order_id", "review"),
}

# 注入依赖：要注入 p 必须先在 SQL 里出现 oi（p 的 ON 条件引用 oi.product_id）。
_JOIN_DEPS: Dict[str, Tuple[str, ...]] = {"p": ("oi",)}


class JoinInjectionError(ValueError):
    """维度/过滤所需的表别名无法满足时抛出。

    历史缺陷：这里曾经 `continue` 静默跳过注入，于是 SQL 里出现
    `p.product_category_name` 却没有 `products` JOIN —— 编译期不报错，
    执行期报 "no such column: p"，再被上层 except 吞掉变成"该维度没波动"。
    静默错误最危险，因此现在改为**明确失败**。
    """


def _derived_table_aliases(text: str) -> Set[str]:
    """找出派生表（子查询 JOIN）的别名：`JOIN (SELECT ...) t ON ...`。

    为什么需要：派生表的别名前面是 `)`，普通 `JOIN <ident>` 正则抓不到，
    于是编译器会以为"表不存在"而重复注入同名 JOIN → `ambiguous column name`。
    """
    out: Set[str] = set()
    for m in re.finditer(r"(?:FROM|JOIN)\s*\(", text or "", re.I):
        i = m.end() - 1
        depth = 0
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        am = re.match(r"\s+(?:AS\s+)?([a-zA-Z_][\w]*)", text[i + 1:], re.I)
        if am and am.group(1).lower() not in _SQL_KEYWORDS:
            out.add(am.group(1).lower())
    return out


def present_names(from_clause: str, join_clause: str) -> Set[str]:
    """收集 SQL 里已可用的标识符：**表名与别名都算**（含派生表别名）。

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
    # 派生表内部的表名（如子查询里的 reviews）也要算作"已存在"，避免重复注入
    for dm in re.finditer(r"\(\s*SELECT\b(.*?)\)\s*(?:AS\s+)?[a-zA-Z_][\w]*", text, re.I | re.S):
        for tm in re.finditer(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", dm.group(1), re.I):
            names.add(tm.group(1).lower())
    names |= _derived_table_aliases(text)
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


def _self_defined_aliases(template: str) -> Set[str]:
    """模板**自己**引入的别名（子查询里的 FROM/JOIN 别名）。

    为什么需要：过滤模板可以是自包含的子查询，例如
      `language_excl: "co.Code NOT IN (SELECT cl2.CountryCode FROM countrylanguage cl2
                        WHERE cl2.Language = {value})"`
    这里 `cl2` 是模板自己定义的。若把它当成"外部需要的别名"就会去 JOIN 池里找
    `cl2` → 找不到 → 抛 JoinInjectionError → 一个本来完全自洽的过滤条件被拒绝。
    """
    out: Set[str] = set()
    for m in re.finditer(
            r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)(?:\s+(?:AS\s+)?([a-zA-Z_][\w]*))?", template or "",
            re.I):
        table, alias = m.group(1), m.group(2)
        if table:
            out.add(table.lower())
        if alias and alias.lower() not in _SQL_KEYWORDS:
            out.add(alias.lower())
    return out


def aliases_needed_for_filters(cfg, filter_types: Sequence[str]) -> Set[str]:
    """某个过滤条件清单需要在 SQL 里出现的表别名。

    历史缺陷：过滤模板只用 dims 注入 JOIN，于是 `gmv` 加一个 state 过滤会生成
    `WHERE c.customer_state = 'SP'` 却没有 `customers` JOIN —— 编译期不报错、
    执行期报 "no such column: c.customer_state"。过滤器与维度一样需要 JOIN。

    模板自带的子查询别名要排除（见 `_self_defined_aliases`）。
    """
    needed: Set[str] = set()
    for ft in filter_types or ():
        tmpl = (getattr(cfg, "filter_templates", {}) or {}).get(ft, "") or ""
        own = _self_defined_aliases(tmpl)
        for mm in re.finditer(r"\b([a-zA-Z_][\w]*)\.", tmpl):
            alias = mm.group(1).lower()
            if alias in own:
                continue
            needed.add(alias)
    return needed


def needed_aliases(cfg, dim_keys: Sequence[str] = (),
                   filter_types: Sequence[str] = ()) -> Set[str]:
    """维度 + 过滤共同需要的表别名。"""
    return aliases_needed_for_dims(cfg, dim_keys) | \
        aliases_needed_for_filters(cfg, filter_types)


def inject_dim_joins(cfg, metric, dim_keys: Sequence[str] = (),
                     filter_types: Sequence[str] = ()) -> str:
    """返回"指标自带 JOIN + 维度/过滤所需 JOIN"的完整 join_clause（去重、按依赖排序）。

    依赖顺序：p 依赖 oi，因此需要 p 时会先把 oi 注入进来（旧实现直接跳过 p）。
    无法满足的别名 **不再静默跳过**，而是抛 JoinInjectionError（调用方转成编译错误）。
    """
    from_clause = metric.from_clause or ""
    base_join = (getattr(metric, "join_clause", "") or "").strip()
    present = present_names(from_clause, base_join)
    joins: List[str] = [base_join] if base_join else []

    pending = set(needed_aliases(cfg, dim_keys, filter_types))
    # 已可用（在 from/自带 join 里）的别名直接从待办里去掉；表名命中同理。
    pending = {a for a in pending if a not in present}

    while pending:
        changed = False
        for alias in sorted(pending):          # sorted() 取快照，循环内改 pending 是安全的
            spec = DIM_JOIN_POOL.get(alias)
            if not spec:
                raise JoinInjectionError(
                    f"无法为维度/过滤注入所需表别名「{alias}」：不在 JOIN 池 "
                    f"{sorted(DIM_JOIN_POOL)} 中，请补配置或去掉该维度/过滤")
            missing = [d for d in _JOIN_DEPS.get(alias, ()) if d not in present]
            if missing:
                # 先把依赖排进待办（依赖也要是池里的别名，否则下一轮报错），本轮跳过
                for d in missing:
                    if d not in pending:
                        pending.add(d)
                        changed = True
                continue
            table, clause, _src = spec
            joins.append(clause)
            present.add(alias)
            present.add(table)
            pending.discard(alias)
            changed = True
        if not changed:
            raise JoinInjectionError(
                f"JOIN 依赖无法满足（循环依赖？）：待注入 {sorted(pending)}")
    return " ".join(j for j in joins if j)


def metric_source(cfg, metric, dim_keys: Sequence[str] = (),
                  filter_types: Sequence[str] = ()) -> str:
    """指标的数据来源 = from_clause + 维度/过滤所需 JOIN。归因/编译统一走这里。"""
    jc = inject_dim_joins(cfg, metric, dim_keys, filter_types)
    parts = [metric.from_clause or ""]
    if jc:
        parts.append(jc)
    return "\n".join(p for p in parts if p)


def tables_of(metric) -> List[str]:
    """指标用到的表名（产品上展示"数据来源"）。"""
    text = f"{metric.from_clause or ''} {getattr(metric, 'join_clause', '') or ''}"
    return list(dict.fromkeys(re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", text, re.I)))
