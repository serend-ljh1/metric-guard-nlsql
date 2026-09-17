"""
sqlpa.business.permissions
==========================
业务护栏（P1）：表列权限白名单 + 敏感字段掩码。

职责（在组装SQL之后、执行/返回之前做静态校验）：
  - check_access: 解析 SQL 引用的 表.列，逐条核对角色白名单，未授权直接拦截。
  - mask_result:  对结果中的敏感列做掩码（如 138****1234）。

⚠️ 两处历史缺陷（记录在此，避免再犯）：
  1) 只匹配 `别名.列` 形式 → `SELECT customer_phone FROM customers` 这类
     **非限定列名**完全绕过列白名单；
  2) 掩码按**输出列名**匹配 → `SELECT customer_zip_code_prefix AS zip`
     把原始值原样返回（列名一改就掩不到）。
  现在两者都改为"解析到真实来源列"再判定；解析不出来时按保守策略处理
  （宁可多掩，不可漏掩）。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

_SQL_KEYWORDS = {
    "select", "from", "where", "group", "order", "limit", "on", "left", "right",
    "inner", "outer", "join", "as", "and", "or", "not", "by", "having", "union",
    "distinct", "case", "when", "then", "else", "end", "asc", "desc", "count",
    "sum", "avg", "min", "max", "coalesce", "cast", "round",
}


# ---------------------------------------------------------------- 解析辅助

def _alias_map(sql: str) -> Dict[str, str]:
    """from/join 处把 别名->表名 的映射建出来。 o->orders, oi->order_items ..."""
    mapping: Dict[str, str] = {}
    for m in re.finditer(
            r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)(?:\s+(?:AS\s+)?([a-zA-Z_][\w]*))?", sql, re.I):
        table, alias = m.group(1), m.group(2)
        if table.lower() in _SQL_KEYWORDS:
            continue
        if alias and alias.lower() in _SQL_KEYWORDS:
            alias = None
        mapping[(alias or table).lower()] = table
    return mapping


def _qualified_refs(sql: str) -> List[Tuple[str, str]]:
    """提取 (别名, 列) 形式的引用。"""
    return [(m.group(1), m.group(2))
            for m in re.finditer(r"\b([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)", sql)]


def _schema_tables(schema: Optional[Dict]) -> Dict[str, List[str]]:
    """把 schema 归一成 {表名小写: [列名小写...]}。

    兼容两种形态：
      - {"tables": [{"name":..., "columns":[{"name":...} | "col"]}, ...]}
      - {"表名": ["列1","列2"]}
    """
    out: Dict[str, List[str]] = {}
    if not schema:
        return out
    tables = schema.get("tables")
    if isinstance(tables, list):
        for t in tables:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "").lower()
            if not name:
                continue
            cols: List[str] = []
            for c in (t.get("columns") or []):
                cname = c.get("name") if isinstance(c, dict) else c
                if cname:
                    cols.append(str(cname).lower())
            out[name] = cols
        return out
    for k, v in schema.items():
        if isinstance(v, (list, tuple)):
            out[str(k).lower()] = [str(c).lower() for c in v if isinstance(c, str)]
    return out


def _known_tables(amap: Dict[str, str], schema_cols: Dict[str, List[str]]) -> List[str]:
    seen = list(amap.values()) + list(schema_cols.keys())
    return list(dict.fromkeys(t for t in seen if t))


def _unqualified_columns(sql: str, tables: List[str],
                         schema_cols: Dict[str, List[str]]) -> List[Tuple[Optional[str], str]]:
    """找出没有表别名前缀的列引用，并解析其归属表。

    归属规则：该列名在候选表里**唯一**出现才认；多个表都有同名列时返回 None
    （归属不明），由上层按保守策略处理，避免误拦正常查询。
    """
    cleaned = re.sub(r"'[^']*'", "''", sql)          # 去字符串字面量
    qualified_cols = {m.group(2).lower()
                      for m in re.finditer(r"\b([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)", cleaned)}
    out: List[Tuple[Optional[str], str]] = []
    for m in re.finditer(r"\b([a-zA-Z_][\w]*)\b", cleaned):
        col = m.group(1).lower()
        if col in _SQL_KEYWORDS or col in qualified_cols or col in tables:
            continue
        owners = [t for t in tables if col in schema_cols.get(t, [])]
        if not owners:
            continue
        out.append((owners[0] if len(owners) == 1 else None, col))
    return out


def _select_items(sql: str) -> List[str]:
    """粗略切出 SELECT 与 FROM 之间的选择列表项（按顶层逗号切分）。"""
    cleaned = re.sub(r"'[^']*'", "''", sql)
    m = re.search(r"\bselect\b(.*?)\bfrom\b", cleaned, re.I | re.S)
    if not m:
        return []
    items, depth, cur = [], 0, []
    for ch in m.group(1):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur))
    return [i.strip() for i in items if i.strip()]


# ---------------------------------------------------------------- 权限校验

def check_access(role: str, perms: Dict, sql: str,
                 schema: Optional[Dict] = None) -> List[str]:
    """返回未授权访问清单（空=通过）。

    schema 可选：传入后可解析**非限定列名**归属哪张表，堵住
    `SELECT customer_phone FROM customers` 绕过列白名单的缺口。
    """
    acfg = (perms.get("roles") or {}).get(role)
    if not acfg:
        return [f"未配置角色 {role} 的权限"]
    allowed_tables = acfg.get("allowed_tables")
    allowed_cols = acfg.get("allowed_columns", {})
    if allowed_tables == "*" and allowed_cols == "*":
        return []  # 管理员全放行

    amap = _alias_map(sql)
    refs = _qualified_refs(sql)
    tables = _known_tables(amap, _schema_tables(schema))
    schema_cols = _schema_tables(schema)
    bad: List[str] = []

    # 1) 表级
    for alias, _col in refs:
        table = amap.get(alias.lower(), alias)
        if allowed_tables != "*" and table not in (allowed_tables or []):
            bad.append(f"角色 {role} 无权访问表 {table}")

    # 2) 限定列（原有行为）
    if allowed_cols != "*":
        for alias, col in refs:
            table = amap.get(alias.lower(), alias)
            if allowed_tables != "*" and table not in (allowed_tables or []):
                continue
            cols = allowed_cols.get(table)
            if cols is not None and col not in cols:
                bad.append(f"角色 {role} 无权访问字段 {table}.{col}")

        # 3) 非限定列（新增：修复绕过缺口）
        if schema_cols:
            for table, col in _unqualified_columns(sql, tables, schema_cols):
                if table is None:
                    continue  # 归属不明 → 不误拦，交由保守掩码兜底
                cols = allowed_cols.get(table)
                if cols is not None and col not in cols:
                    bad.append(f"角色 {role} 无权访问字段 {table}.{col}")

    return list(dict.fromkeys(bad))


# ---------------------------------------------------------------- 结果掩码

def _mask_value(v):
    if v is None:
        return v
    s = str(v)
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 7:
        return digits[:3] + "****" + digits[-3:]
    return "***"


def sensitive_output_positions(sql: str, headers: Sequence[str], perms: Dict,
                               schema: Optional[Dict] = None) -> Set[int]:
    """算出**结果集中哪些列位置**来源于敏感字段（按来源判定，不看输出列名）。

    这样 `SELECT customer_zip_code_prefix AS zip` 也能被正确掩码。
    返回 0-based 位置集合。
    """
    sensitive = perms.get("sensitive_columns") or {}
    if not sensitive:
        return set()
    flat = {str(c).lower() for cols in sensitive.values() for c in (cols or [])}

    amap = _alias_map(sql)
    schema_cols = _schema_tables(schema)
    tables = _known_tables(amap, schema_cols)

    def _owner_of(col: str) -> Optional[str]:
        owners = [t for t in tables if col in schema_cols.get(t, [])]
        return owners[0] if len(owners) == 1 else None

    items = _select_items(sql)
    if not items:
        # 解析不出选择列表 → 退化为按输出列名匹配
        return {i for i, h in enumerate(headers) if str(h).lower() in flat}

    positions: Set[int] = set()
    for idx, item in enumerate(items):
        if idx >= len(headers):
            break
        expr = re.split(r"\s+as\s+", item, flags=re.I)[0].strip()
        hit = False
        for al, col in re.findall(r"\b([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)\b", expr):
            table = amap.get(al.lower(), al)
            if col.lower() in {c.lower() for c in (sensitive.get(table) or [])}:
                hit = True
        if not hit:
            for tok in re.findall(r"\b([a-zA-Z_][\w]*)\b", expr):
                low = tok.lower()
                if low in _SQL_KEYWORDS:
                    continue
                table = _owner_of(low)
                names = (sensitive.get(table) or []) if table else \
                    [c for cols in sensitive.values() for c in (cols or [])]
                if low in {str(c).lower() for c in names}:
                    hit = True
                    break
        if hit:
            positions.add(idx)
    return positions


def mask_result(headers: List[str], rows: Sequence[Sequence],
                sensitive: Dict[str, List[str]],
                sql: Optional[str] = None,
                perms: Optional[Dict] = None,
                schema: Optional[Dict] = None) -> List[list]:
    """按敏感字段掩码结果。

    传 sql/perms 时按**列来源**判定敏感性（可覆盖 AS 别名绕过）；
    不传时保持原有"按输出列名匹配"的行为（向后兼容）。
    """
    if sql is not None:
        use_perms = perms if perms is not None else {"sensitive_columns": sensitive}
        pos = sensitive_output_positions(sql, headers, use_perms, schema)
        if not pos:
            return [list(r) for r in rows]
        return [[_mask_value(v) if i in pos else v for i, v in enumerate(row)]
                for row in rows]

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
