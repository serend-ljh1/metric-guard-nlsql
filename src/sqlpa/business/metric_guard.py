"""
sqlpa.business.metric_guard
===========================
业务模式「注入 + 校验」护栏（防口径幻觉）：
  - build_constraint: 把权威"指标公式/join/过滤"作为硬约束喂给 Writer(LLM)。
  - verify_formula : 校验生成的 SQL 的**结果列**是否就是配置表达式，被改则拒绝。

这解决"LLM 生成 vs 配置拼"的矛盾：LLM 负责搭查询结构，业务层负责"给公式 + 校验公式没被改"。

校验方式（2026-09 由"子串包含"升级为"结构绑定"）：
  旧实现是 `表达式 in SQL` 的子串判定，只要把表达式留在 SQL 的任意位置即可绕过：
    - `SELECT SUM(oi.price) AS decoy, SUM(oi.price)*0.001 AS gmv`（诱饵列 + 改系数）
    - `SELECT 42 AS gmv /* SUM(oi.price) */`（塞进注释）
    - `SELECT 'SUM(oi.price)' AS note, 1 AS gmv`（塞进字符串字面量）
  以上三种实测都能"通过"，也就是说防篡改只是装饰。
  现在改为：切出 SELECT 列表、逐项拿"表达式 + 别名"，要求**口径列（别名 = 指标 key）
  的表达式与配置逐字等价**。表达式被替换、被包装、只出现在注释/字符串/WHERE 里，
  都会因为"结果列对不上"而被拦截。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .metric_config import BusinessConfig


def formula_of(cfg: BusinessConfig, metric_key: str) -> str:
    return cfg.metrics[metric_key].metric_expr


def build_constraint(cfg: BusinessConfig, metric_key: str,
                     dims: List[str], filters: List[Tuple[str, object]]) -> str:
    """构造给 Writer(LLM) 的"业务指标硬约束"文本。"""
    m = cfg.metrics[metric_key]
    dim_names = ", ".join(cfg.dimensions[d].name for d in dims if d in cfg.dimensions) or "不分组"
    lines = [
        f"指标: {m.name}",
        f"计算表达式(必须在 SELECT 中【原样使用】，不得改动): {m.metric_expr}",
        f"数据来源/join: {m.from_clause}",
        f"基础过滤: {m.where_core}",
        f"分组维度: {dim_names}",
    ]
    if filters:
        lines.append("用户过滤: " + "; ".join(f"{t}={v}" for t, v in filters))
    return "\n".join(lines)


def _norm(s: str) -> str:
    """归一化：去空白 + 小写（只做词法等价，不做语义等价）。"""
    return re.sub(r"\s+", "", s or "").lower()


def _strip_comments(sql: str) -> str:
    """去掉 SQL 注释：注释里的表达式不算"使用了表达式"。"""
    sql = re.sub(r"/\*.*?\*/", " ", sql or "", flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def _select_items(sql: str) -> List[str]:
    """切出 SELECT 与**顶层 FROM** 之间的选择列表项。

    扫描时同时跟踪**括号深度**与**字符串字面量**：
      - 顶层 from 才算选择列表的结束（派生指标的选择项里含子查询 `(SELECT ... FROM ...)`，
        用正则 `select(.*?)from` 会在内层 FROM 处截断，导致 share/ratio 的口径列被判错）；
      - 字面量里的逗号（如 `IN ('a','b')`）不能当分隔符，且字面量要**原样保留**——
        口径表达式里的 `status='canceled'` 也是被校验的一部分。
    """
    m = re.search(r"\bselect\b", sql or "", re.I)
    if not m:
        return []
    rest = sql[m.end():]

    # 第一遍：找到顶层 FROM，截出选择列表主体
    body_chars: List[str] = []
    depth, quote, i = 0, None, 0
    while i < len(rest):
        ch = rest[i]
        if quote:
            body_chars.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            body_chars.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and re.match(r"\bfrom\b", rest[i:], re.I):
            break
        body_chars.append(ch)
        i += 1
    body = "".join(body_chars)

    # 第二遍：按顶层逗号切项
    items, depth, cur, quote = [], 0, [], None
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    items.append("".join(cur))
    return [i.strip() for i in items if i.strip()]


def _split_alias(item: str) -> Tuple[str, Optional[str]]:
    """把选择项拆成 (表达式, 别名)。别名缺失时返回 None。"""
    m = re.match(r"^(.*?)\s+as\s+([a-zA-Z_][\w]*)\s*$", item, re.I | re.S)
    if m:
        return m.group(1).strip(), m.group(2)
    return item.strip(), None


def verify_formula(sql: str, metric_expr: str, key: str = "") -> List[str]:
    """校验 SQL 的**结果列**是否就是配置口径。返回问题列表(空=通过)。

    key 为该指标在配置里的 key（编译器会把口径列命名为 `AS <key>`）。给了 key 就要求
    "别名 = key 的那一列"表达式等价；否则退化为"任意结果列等价"（更宽松，仅供无 key 的老调用）。
    """
    if not sql:
        return ["生成为空"]
    if not metric_expr:
        return ["指标表达式为空"]

    cleaned = _strip_comments(sql)
    items = _select_items(cleaned)
    target = _norm(metric_expr)

    if not items:
        # 切不出选择列表（极端写法）→ 保守拒绝，而不是回退到子串判定放行
        return [f"无法解析生成 SQL 的结果列，拒绝按配置口径放行：{sql[:80]}"]

    for item in items:
        expr_part, alias = _split_alias(item)
        if key:
            if not alias or alias.lower() != key.lower():
                continue
        if _norm(expr_part) == target:
            return []
    where = f"别名 {key} 的结果列" if key else "任一结果列"
    return [f"生成 SQL 的{where}不是配置口径「{metric_expr}」"
            f"（表达式被替换/包装，或只出现在注释、字符串、过滤条件里），已拦截（防口径篡改）"]
