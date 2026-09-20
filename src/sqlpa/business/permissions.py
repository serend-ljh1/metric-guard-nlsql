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
    """选择列表项（复用 metric_guard 的解析：顶层 FROM + 括号/字面量感知）。

    为什么不各写一份：`select(.*?)from` 这种正则在派生指标（选择项里含子查询）上会在
    内层 FROM 处截断，导致"口径列"解析错位——权限与掩码都建立在同一份解析上，
    两处实现分叉就会出现"一边拦一边放"。
    """
    from .metric_guard import _select_items as _parse
    return _parse(sql)


def _has_wildcard_projection(sql: str) -> bool:
    """选择列表里是否出现通配符投影（`*` / `t.*`）。

    通配符是权限/掩码的**结构性盲点**：无法在解析层枚举"到底返回了哪些列"，
    于是旧实现直接返回"没有敏感列"（fail-open），实测 `SELECT * FROM customers c`
    会把 customer_zip_code_prefix / customer_phone 原样返回。安全控件在解析不出来
    时必须**默认拒绝**，不能默认放行。
    """
    for item in _select_items(sql):
        head = re.split(r"\s+as\s+", item, flags=re.I)[0].strip()
        if head == "*" or head.endswith(".*") or re.fullmatch(r"\w+\s*\.\s*\*", head):
            return True
    return False


def _derived_alias_tables(sql: str) -> Dict[str, List[str]]:
    """派生表别名 -> 其子查询里实际引用的底层表。

    为什么需要：`JOIN (SELECT ... FROM reviews GROUP BY order_id) r` 这种派生表，
    别名前面是 `)`，`_alias_map` 抓不到 → 权限校验把 `r` 当成一张"未知表"直接拒绝
    （实测 `AVG(r.review_score)` 会被判"无权访问表 r"）。派生别名必须能解析回底层表。
    """
    out: Dict[str, List[str]] = {}
    for m in re.finditer(r"(?:FROM|JOIN)\s*\(", sql or "", re.I):
        i = m.end() - 1
        depth, start = 0, i
        while i < len(sql):
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = sql[start + 1:i]
        am = re.match(r"\s+(?:AS\s+)?([a-zA-Z_][\w]*)", sql[i + 1:], re.I)
        if not am or am.group(1).lower() in _SQL_KEYWORDS:
            continue
        tables = [t.group(1).lower()
                  for t in re.finditer(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", body, re.I)]
        out[am.group(1).lower()] = list(dict.fromkeys(tables))
    return out


def row_filter_clauses(role: str, perms: Dict) -> List[str]:
    """角色的**行级过滤**谓词（RLS-lite）。

    配置形态：
        permissions:
          roles:
            seller:
              row_filters:
                - "oi.seller_id = '6560211a19b47992c3666cc44a7e94c0'"

    诚实边界：这是**应用层**的行级过滤（把谓词注入编译后的 WHERE），不是数据库原生 RLS。
    优点是可见、可测、与口径一起进审计；缺点是绕过应用直连数据库就失效——
    生产应同时用数据库视图/RLS 策略或独立只读账号兜底。
    """
    acfg = (perms.get("roles") or {}).get(role) or {}
    raw = acfg.get("row_filters") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(x).strip() for x in raw if str(x).strip()]


def missing_row_filter_aliases(sql: str, clauses: Sequence[str]) -> List[str]:
    """找出**行过滤谓词引用了但 SQL 里不存在的别名**。

    为什么必须拦：行级过滤只能收紧不能失效。若某个指标的口径里没有该表
    （例如 cancellation_rate 不连 order_items），谓词就无法生效——此时**必须拒绝**，
    而不是悄悄返回全量数据（那正是"越权看全表"的事故形态）。
    """
    present = set(_alias_map(sql)) | {t.lower() for t in _alias_map(sql).values()}
    for t in re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", sql or "", re.I):
        present.add(t.lower())
    present |= _derived_alias_tables(sql).keys()
    bad: List[str] = []
    for clause in clauses:
        for alias in re.findall(r"\b([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)", clause):
            if alias[0].lower() not in present:
                bad.append(alias[0])
    return sorted(set(bad))


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
    derived = _derived_alias_tables(sql)
    tables = _known_tables(amap, _schema_tables(schema))
    schema_cols = _schema_tables(schema)
    bad: List[str] = []

    # 0) 通配符投影：列级白名单无法逐列核对 → 对受列限制的角色一律拒绝（fail-closed）
    if allowed_cols != "*" and _has_wildcard_projection(sql):
        bad.append(f"角色 {role} 不允许通配符投影（SELECT * 无法逐列核对权限，"
                   f"请显式列出所需列）")

    def _deny_columns(table: str, col: str) -> None:
        cols = allowed_cols.get(table)
        if cols is not None and col not in cols:
            bad.append(f"角色 {role} 无权访问字段 {table}.{col}")

    # 1) 表级
    for alias, _col in refs:
        if alias.lower() in derived:
            for t in derived[alias.lower()]:
                if allowed_tables != "*" and t not in (allowed_tables or []):
                    bad.append(f"角色 {role} 无权访问表 {t}")
            continue
        table = amap.get(alias.lower(), alias)
        if allowed_tables != "*" and table not in (allowed_tables or []):
            bad.append(f"角色 {role} 无权访问表 {table}")

    # 2) 限定列（原有行为）
    if allowed_cols != "*":
        for alias, col in refs:
            if alias.lower() in derived:
                # 派生别名：按底层表逐个核对（任一底层表禁止该列即拒绝）
                for t in derived[alias.lower()]:
                    _deny_columns(t, col)
                continue
            table = amap.get(alias.lower(), alias)
            if allowed_tables != "*" and table not in (allowed_tables or []):
                continue
            _deny_columns(table, col)

        # 3) 非限定列（新增：修复绕过缺口）
        if schema_cols:
            for table, col in _unqualified_columns(sql, tables, schema_cols):
                if table is None:
                    continue  # 归属不明 → 不误拦，交由保守掩码兜底
                _deny_columns(table, col)

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

    # 通配符投影：来源无法逐列解析 → 保守掩码"任何可能是敏感列的列"（fail-closed）。
    # 旧实现在这里返回 {0}/提前 return，等于明文放行。
    wildcard = any(re.split(r"\s+as\s+", it, flags=re.I)[0].strip() in ("*",) or
                   re.split(r"\s+as\s+", it, flags=re.I)[0].strip().endswith(".*")
                   for it in items)
    if wildcard:
        sensitive_names = {str(c).lower() for cols in sensitive.values() for c in (cols or [])}
        pos = {i for i, h in enumerate(headers) if str(h).lower() in sensitive_names}
        if not pos and sensitive_names:
            # 连输出列名都对不上（例如 schema 缺失）→ 整列掩码，宁可多掩不可漏掩
            pos = set(range(len(headers)))
        return pos

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
