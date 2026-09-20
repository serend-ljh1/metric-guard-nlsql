"""
sqlpa.business.compiler
=======================
语义层编译器：把「指标 + 维度 + 过滤」规格**确定性**编译成 SQL。

产品定位（本模块是主路径，不是兜底）：
    自然语言 → 意图解析 → 【QuerySpec】→ 本编译器 → SQL
命中语义层时，**LLM 完全不在 SQL 生成路径上**：公式来自配置、SQL 由代码拼装。
好处是可审计、零口径漂移、零 token、毫秒级，且结果可标注"口径已认证"。

只有语义层覆盖不到的问题（口径外）才降级到多 Agent 自由生成。

支持的派生指标（derived）：
  - ratio@a/b  ：两个基础指标之比（如 freight_rate = 运费/GMV，各算各的、再相除）
  - share      ：占整体比例（当前口径值 / 同期不带分组的总值）

注意与旧 `assembler.py` 的关系：assembler 是早期最小实现（仅供离线兜底），
本模块是其超集并成为主路径；assembler 保留以兼容既有调用与测试。
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .metric_config import BusinessConfig, Metric
from .sqlgen import JoinInjectionError, inject_dim_joins, tables_of

# 时间粒度：把基础维度 dt 按粒度转成对应 SQL 表达式
TIME_GRAINS: Dict[str, str] = {
    "day": "DATE(o.order_purchase_timestamp)",
    "week": "strftime('%Y-W%W', o.order_purchase_timestamp)",
    "month": "strftime('%Y-%m', o.order_purchase_timestamp)",
    "quarter": ("strftime('%Y', o.order_purchase_timestamp) || '-Q' || "
                "CAST((CAST(strftime('%m', o.order_purchase_timestamp) AS INTEGER) + 2) / 3 AS INTEGER)"),
}


class CompileError(ValueError):
    """规格不合法（不支持的指标/维度组合等）→ 上层据此走降级或拒绝。"""


# JOIN 子句里跟在表名后的"非别名"关键字（用于区分 `JOIN t ON ...` 与 `JOIN t x ON ...`）
_JOIN_STOPWORDS = {"on", "using", "where", "group", "order", "left", "right", "inner",
                   "outer", "join", "and", "or", "limit", "having", "as"}


@dataclass
class QuerySpec:
    """语义层查询规格：意图解析的输出，编译器的输入。"""
    metric: str
    dims: List[str] = field(default_factory=list)
    filters: List[Tuple[str, object]] = field(default_factory=list)
    time_grain: Optional[str] = None   # day/week/month/quarter；仅当 dims 含 dt 时生效
    top: int = 0                       # >0 时按指标降序取前 N


@dataclass
class CompiledQuery:
    """编译产物：SQL + 可追溯的口径信息（产品要展示给人看的就是这些）。"""
    sql: str
    metric_key: str
    metric_name: str
    metric_expr: str
    dims: List[str]
    filters: Dict[str, str]
    sources: List[str]              # 用到的表，产品上展示"数据来源"
    derived: Optional[Dict] = None  # 派生指标信息（ratio/share）
    certified: bool = True          # 口径来自配置 → 已认证
    owner: str = ""
    version: str = ""

    def to_dict(self) -> Dict:
        return {
            "sql": self.sql, "metric": self.metric_key, "metric_name": self.metric_name,
            "metric_expr": self.metric_expr, "dims": self.dims, "filters": self.filters,
            "sources": self.sources, "derived": self.derived,
            "certified": self.certified, "owner": self.owner, "version": self.version,
        }


# ---------------------------------------------------------------- 基础工具

def _quote(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _render_value(value) -> str:
    if isinstance(value, (list, tuple)):
        return "(" + ",".join(_quote(x) for x in value) + ")"
    return _quote(value)


def _first_of_month(d: datetime.date) -> datetime.date:
    return d.replace(day=1)


def resolve_time_range(spec: str, now: Optional[datetime.date] = None) -> Tuple[str, str]:
    """把时间语义（本月/上月/最近N天/YYYY-MM）转成 SQL 字面量 (start, end)。"""
    now = now or datetime.date.today()
    s = str(spec).strip()
    m = re.fullmatch(r"(\d{4})[年\-](\d{1,2})月?", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        start = datetime.date(y, mo, 1)
        end = _first_of_month(datetime.date(y + (mo == 12), (mo % 12) + 1, 1))
        return _quote(start.isoformat()), _quote(end.isoformat())
    if "上月" in s or "上个月" in s:
        fm = _first_of_month(now)
        return _quote(_first_of_month(fm - datetime.timedelta(days=1)).isoformat()), _quote(fm.isoformat())
    if "本周" in s:
        start = now - datetime.timedelta(days=now.weekday())
        return _quote(start.isoformat()), _quote((start + datetime.timedelta(days=7)).isoformat())
    if "上周" in s:
        start = now - datetime.timedelta(days=now.weekday() + 7)
        return _quote(start.isoformat()), _quote((start + datetime.timedelta(days=7)).isoformat())
    if "本月" in s or "这个月" in s:
        fm = _first_of_month(now)
        return _quote(fm.isoformat()), _quote(_first_of_month(fm + datetime.timedelta(days=31)).isoformat())
    mm = re.search(r"(\d+)\s*天", s)
    if mm:
        n = int(mm.group(1))
        return _quote((now - datetime.timedelta(days=n)).isoformat()), _quote(now.isoformat())
    start = now - datetime.timedelta(days=30)
    return _quote(start.isoformat()), _quote(now.isoformat())


def render_filter(cfg: BusinessConfig, ftype: str, value) -> str:
    tmpl = cfg.filter_templates.get(ftype)
    if not tmpl:
        raise CompileError(f"未知过滤类型: {ftype}")
    if ftype == "time_range":
        start, end = resolve_time_range(value)
        return tmpl.format(start=start, end=end)
    return tmpl.format(value=_render_value(value))


def dimension_sql(cfg: BusinessConfig, dim: str, time_grain: Optional[str] = None) -> str:
    """维度 -> SQL 片段。dt 维度支持按时间粒度改写。"""
    if dim == "dt" and time_grain:
        expr = TIME_GRAINS.get(time_grain)
        if not expr:
            raise CompileError(f"不支持的时间粒度: {time_grain}")
        return expr
    d = cfg.dimensions.get(dim)
    if not d:
        raise CompileError(f"未知维度: {dim}")
    return d.sql_fragment


def parse_derived(metric_key: str) -> Optional[Dict]:
    """解析派生指标语法：ratio@分子/分母 或 share@基础指标。"""
    if metric_key.startswith("ratio@"):
        body = metric_key[len("ratio@"):]
        if "/" not in body:
            raise CompileError(f"ratio 语法应为 ratio@分子/分母，收到 {metric_key}")
        a, b = body.split("/", 1)
        return {"kind": "ratio", "numerator": a.strip(), "denominator": b.strip()}
    if metric_key.startswith("share@"):
        return {"kind": "share", "base": metric_key[len("share@"):].strip()}
    return None


def _tables_of(m: Metric) -> List[str]:
    """（保留给外部调用的兼容别名）见 sqlgen.tables_of。"""
    return tables_of(m)


def _check_support(m: Metric, dim_keys: Sequence[str], filter_types: Sequence[str],
                   cfg: BusinessConfig) -> None:
    """**编译期**校验维度/过滤是否在该指标声明的支持范围内。

    历史缺陷：支持度只在 matcher 层校验，编译器一律放行。于是一旦有别的入口
    （指标编辑、派生指标、直接调用 API/脚本）绕过 matcher，就会编译出
    `cancellation_rate + category` 这种没注入 products JOIN 的 SQL：编译成功、
    执行报 "no such column: p"，甚至（如 aov+category）执行成功却给出未认证口径。
    约束必须落在编译器上，否则"认证"只是入口层的君子协定。
    """
    bad: List[str] = []
    for d in dim_keys:
        if m.support_dims and d not in m.support_dims:
            dname = cfg.dimensions[d].name if d in cfg.dimensions else d
            bad.append(f"{m.name} 不支持按「{dname}」统计")
    for ft in filter_types:
        if m.support_filters and ft not in m.support_filters:
            bad.append(f"{m.name} 不支持「{ft}」过滤")
    if bad:
        raise CompileError("；".join(bad))


def _inject(cfg: BusinessConfig, m: Metric, dim_keys: Sequence[str],
            filter_types: Sequence[str]) -> str:
    """注入 JOIN，并把 sqlgen 的注入失败翻译成编译错误（对上层是同一类拒绝）。"""
    try:
        return inject_dim_joins(cfg, m, dim_keys, filter_types)
    except JoinInjectionError as e:
        raise CompileError(str(e)) from e


def _apply_row_filters(join_sql: str, from_clause: str,
                       row_filters: Sequence[str]) -> List[str]:
    """把行级过滤谓词并入 WHERE，并**校验谓词的表确实在查询里**（否则拒绝）。

    行级过滤只能收紧权限、不能失效。谓词引用了未加入的表时（例如某指标口径里没有
    order_items），若放任不管就会生成一条"过滤条件引用未知别名"的 SQL —— 要么报错，
    要么（更糟）在别的方言里被当成字符串而静默失效，用户于是看到本不该看到的全量数据。
    """
    clauses = [c.strip() for c in (row_filters or []) if str(c).strip()]
    if not clauses:
        return []
    from .permissions import missing_row_filter_aliases
    missing = missing_row_filter_aliases(f"{from_clause} {join_sql}", clauses)
    if missing:
        raise CompileError(
            f"行级权限无法生效：过滤条件引用了查询中不存在的表别名 {missing}。"
            f"该指标的取数范围里没有这些表，为避免越权返回全量数据，本次请求被拒绝。")
    return clauses


# 标准维度 JOIN 池：维度所需的表如果不在指标自带的 JOIN 里，就自动补上。
# 这样"支持某维度"与"能查出该维度"不会再脱节（配置声明支持 → 编译器保证可执行）。
def compile_spec(cfg: BusinessConfig, spec: QuerySpec,
                 row_filters: Sequence[str] = ()) -> CompiledQuery:
    """把 QuerySpec 确定性编译成 SQL。不合法则抛 CompileError（上层据此降级/拒绝）。

    row_filters：调用方角色的**行级过滤谓词**（RLS-lite）。注入前会校验谓词引用的别名
    确实出现在 SQL 里；引用了不存在的表 → 明确拒绝，绝不"过滤失效但照样返回全量"。
    """
    derived = parse_derived(spec.metric)
    if derived:
        return _compile_derived(cfg, spec, derived, row_filters)
    m = cfg.metrics.get(spec.metric)
    if not m:
        raise CompileError(f"未知指标: {spec.metric}")

    dim_keys = [d for d in (spec.dims or []) if d in cfg.dimensions]
    if len(dim_keys) != len(spec.dims or []):
        unknown = [d for d in (spec.dims or []) if d not in cfg.dimensions]
        raise CompileError(f"未知维度: {unknown}")

    filter_types = [ft for ft, _v in (spec.filters or [])]
    _check_support(m, dim_keys, filter_types, cfg)

    dim_sqls = [dimension_sql(cfg, d, spec.time_grain if d == "dt" else None) for d in dim_keys]
    select_parts = [f"{m.metric_expr} AS {m.key}"]
    select_parts += [f"{frag} AS {d}" for d, frag in zip(dim_keys, dim_sqls)]

    where_parts = [m.where_core] if m.where_core and m.where_core not in ("", "1=1") else []
    for ftype, value in (spec.filters or []):
        where_parts.append(render_filter(cfg, ftype, value))
    join_sql = _inject(cfg, m, dim_keys, filter_types)
    where_parts += _apply_row_filters(join_sql, m.from_clause, row_filters)

    sql = _build_sql(select_parts, m.from_clause, join_sql, where_parts,
                     dim_sqls, order_expr=m.metric_expr, top=spec.top)
    return CompiledQuery(
        sql=sql, metric_key=m.key, metric_name=m.name, metric_expr=m.metric_expr,
        dims=dim_keys, filters={ft: str(v) for ft, v in (spec.filters or [])},
        sources=tables_of(m), owner=m.owner, version=m.version,
    )


def _join_alias(clause: str) -> Optional[str]:
    """取一段 JOIN 子句的别名（支持派生表 `JOIN (SELECT ...) t`）。"""
    m = re.match(r"(?:LEFT\s+|INNER\s+)?JOIN\s*", clause or "", re.I)
    if not m:
        return None
    body = clause[m.end():]
    if body.startswith("("):
        depth = 0
        for i, ch in enumerate(body):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    am = re.match(r"\s+(?:AS\s+)?([a-zA-Z_][\w]*)", body[i + 1:], re.I)
                    return am.group(1).lower() if am else None
        return None
    tm = re.match(r"([a-zA-Z_][\w]*)", body)
    if not tm:
        return None
    table = tm.group(1).lower()
    am = re.match(r"\s+(?:AS\s+)?([a-zA-Z_][\w]*)", body[tm.end():], re.I)
    if am and am.group(1).lower() not in _JOIN_STOPWORDS:
        return am.group(1).lower()
    return table


def _split_joins(text: str) -> List[str]:
    """按 JOIN 边界切出各条 JOIN 子句（深度感知，支持派生表与嵌套括号）。

    旧实现用一条正则匹配 `JOIN <表> [别名] ON ...`，遇到派生表（`JOIN (SELECT ...) r`）
    会整段匹配失败 → 子句被静默丢弃 → ratio 合并 JOIN 时丢表。
    """
    out: List[str] = []
    for m in re.finditer(r"\bJOIN\b", text or "", re.I):
        start = m.start()
        pre = re.search(r"(LEFT\s+|INNER\s+)?$", (text or "")[:start], re.I)
        if pre:
            start = pre.start()
        i, depth = m.end(), 0
        while i < len(text):
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth == 0 and re.match(r"\bJOIN\b", text[i:], re.I):
                break
            i += 1
        out.append(text[start:i].strip())
    return out


def _merge_joins(*clauses: str) -> str:
    """把多段 JOIN 子句**按表别名去重**后合并。

    不能只按"子句文本"去重：两个指标可能各自写了 `oi` 的 JOIN（条件写法不同），
    文本去重会留下两条 `JOIN order_items oi`，导致 `ambiguous column name`。
    以别名为键去重才是正确的合并单位；派生表同样按别名处理。

    **同名别名但来源不同 → 明确拒绝**：例如 `avg_review` 的 `r` 是订单粒度派生表、
    `review_count` 的 `r` 是 reviews 表，合并后只能留一个，另一个指标的列会凭空消失
    （历史上表现为执行期 `no such column: r.review_id`）。这种"合并即失真"必须拦在编译期。
    """
    seen: Dict[str, str] = {}
    order: List[str] = []
    for clause in clauses:
        for part in _split_joins(clause or ""):
            alias = _join_alias(part)
            if not alias:
                continue
            prev = seen.get(alias)
            if prev is None:
                seen[alias] = part
                order.append(alias)
            elif re.sub(r"\s+", " ", prev).strip().lower() != \
                    re.sub(r"\s+", " ", part).strip().lower():
                raise CompileError(
                    f"两个指标对别名「{alias}」定义了不同的 JOIN 来源（{prev[:60]}… vs "
                    f"{part[:60]}…），无法在同一查询内合并；请改用同源指标或各自的派生定义")
    return " ".join(seen[a] for a in order)


def _MergeProxy(a: Metric, b: Metric, ja: str, jb: str):
    """把两个指标的 JOIN 合并成一个"查询上下文"（供 ratio 使用）。"""
    class _P:
        pass
    p = _P()
    p.from_clause = a.from_clause
    p.join_clause = _merge_joins(ja, jb)
    return p


def _build_sql(select_parts: Sequence[str], from_clause: str, join_clause: str,
               where_parts: Sequence[str], dim_sqls: Sequence[str],
               order_expr: str = "", top: int = 0) -> str:
    group_sql = ("GROUP BY " + ", ".join(dim_sqls)) if dim_sqls else ""
    order_sql = f"ORDER BY {order_expr} DESC" if (order_expr and dim_sqls) else ""
    limit_sql = f"LIMIT {int(top)}" if top else ""
    parts = [
        "SELECT " + ", ".join(select_parts),
        from_clause,
        (join_clause or "").strip(),
        ("WHERE " + " AND ".join(where_parts)) if where_parts else "",
        group_sql,
        order_sql,
        limit_sql,
    ]
    return "\n".join(p for p in parts if p).strip()


def _compile_derived(cfg: BusinessConfig, spec: QuerySpec, derived: Dict,
                     row_filters: Sequence[str] = ()) -> CompiledQuery:
    """派生指标编译：ratio（两基础指标相除）与 share（占整体比例）。"""
    dim_keys = [d for d in (spec.dims or []) if d in cfg.dimensions]
    if len(dim_keys) != len(spec.dims or []):
        raise CompileError(f"未知维度: {[d for d in (spec.dims or []) if d not in cfg.dimensions]}")
    dim_sqls = [dimension_sql(cfg, d, spec.time_grain if d == "dt" else None) for d in dim_keys]
    filter_types = [ft for ft, _v in (spec.filters or [])]

    if derived["kind"] == "ratio":
        a = cfg.metrics.get(derived["numerator"])
        b = cfg.metrics.get(derived["denominator"])
        if not a or not b:
            raise CompileError(f"ratio 引用了未知指标: {derived}")
        # 支持度校验必须覆盖派生指标的两个操作数，否则"未认证组合"会从派生入口溜进来。
        _check_support(a, dim_keys, filter_types, cfg)
        _check_support(b, dim_keys, filter_types, cfg)
        # 两个基础指标必须能落在同一个查询上下文里：把两者所需的 JOIN 合并。
        if a.from_clause != b.from_clause:
            raise CompileError("ratio 的分子分母主表不同，无法在同一查询内相除")
        # 同源不仅要看主表，还要看**行级口径（where_core）**：把两个 where 直接 AND
        # 会让分子分母同时被对方的口径框住。实例：gmv 的 where 是 `status != canceled`、
        # order_count 的是 `status = delivered`，AND 之后 GMV 被悄悄改成"仅已送达"，
        # 编译成功、执行成功、数字错——正是本项目要消灭的那类口径漂移。
        if (a.where_core or "1=1").strip() != (b.where_core or "1=1").strip():
            raise CompileError(
                f"ratio 的分子分母行级口径不同（{a.key}: {a.where_core or '1=1'} / "
                f"{b.key}: {b.where_core or '1=1'}），不能直接相除")
        base = _MergeProxy(a, b, _inject(cfg, a, dim_keys, filter_types),
                           _inject(cfg, b, dim_keys, filter_types))
        key = f"{a.key}_per_{b.key}"
        expr = (f"CASE WHEN ({b.metric_expr}) IS NULL OR ({b.metric_expr}) = 0 "
                f"THEN NULL ELSE ({a.metric_expr}) * 1.0 / ({b.metric_expr}) END")
        where_parts = []
        for m in (a, b):
            if m.where_core and m.where_core not in ("", "1=1"):
                where_parts.append(m.where_core)
        for ftype, value in (spec.filters or []):
            where_parts.append(render_filter(cfg, ftype, value))
        # 行级过滤同时作用于分子分母（同一查询上下文，两侧都收紧才不出漏）
        where_parts += _apply_row_filters(base.join_clause, a.from_clause, row_filters)
        select_parts = [f"{expr} AS {key}"] + [f"{frag} AS {d}" for d, frag in zip(dim_keys, dim_sqls)]
        sql = _build_sql(select_parts, a.from_clause, base.join_clause, where_parts,
                         dim_sqls, order_expr=expr, top=spec.top)
        return CompiledQuery(
            sql=sql, metric_key=key, metric_name=f"{a.name} / {b.name}",
            metric_expr=expr, dims=dim_keys,
            filters={ft: str(v) for ft, v in (spec.filters or [])},
            sources=sorted(set(tables_of(a)) | set(tables_of(b))),
            derived={"kind": "ratio", "numerator": a.key, "denominator": b.key,
                     "numerator_name": a.name, "denominator_name": b.name},
            owner=a.owner, version=a.version,
        )

    # share：当前分组值 / 同口径不分组的整体值（子查询保证口径一致）
    base = cfg.metrics.get(derived["base"])
    if not base:
        raise CompileError(f"share 引用了未知指标: {derived['base']}")
    if not dim_keys:
        raise CompileError("share 需要至少一个维度")
    _check_support(base, dim_keys, filter_types, cfg)
    where_parts = [base.where_core] if base.where_core and base.where_core not in ("", "1=1") else []
    for ftype, value in (spec.filters or []):
        where_parts.append(render_filter(cfg, ftype, value))
    # 分母子查询是不分组的整体值：只需注入过滤条件所需的 JOIN（不能带维度 JOIN，
    # 否则分母会被维度行数放大）；过滤若引用 c./p. 而不注入就会执行期报错。
    sub_join = _inject(cfg, base, [], filter_types)
    # 行级过滤必须同时进主查询与分母子查询，否则"占比"会拿全量做分母（越权泄露总量）
    rf_main_join = _inject(cfg, base, dim_keys, filter_types)
    where_parts = where_parts + _apply_row_filters(rf_main_join, base.from_clause, row_filters)
    rf_sub = _apply_row_filters(sub_join, base.from_clause, row_filters)
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    rf_sub_where = (" WHERE " + " AND ".join(rf_sub)) if rf_sub else ""
    total_sub = (f"(SELECT {base.metric_expr} {base.from_clause} "
                 f"{sub_join}{where_sql}{rf_sub_where})")
    expr = (f"CASE WHEN {total_sub} IS NULL OR {total_sub} = 0 THEN NULL "
            f"ELSE {base.metric_expr} * 1.0 / {total_sub} END")
    key = f"{base.key}_share"
    select_parts = [f"{expr} AS {key}"] + [f"{frag} AS {d}" for d, frag in zip(dim_keys, dim_sqls)]
    sql = _build_sql(select_parts, base.from_clause,
                     _inject(cfg, base, dim_keys, filter_types), where_parts,
                     dim_sqls, order_expr=expr, top=spec.top)
    return CompiledQuery(
        sql=sql, metric_key=key, metric_name=f"{base.name}占比", metric_expr=expr,
        dims=dim_keys, filters={ft: str(v) for ft, v in (spec.filters or [])},
        sources=tables_of(base),
        derived={"kind": "share", "base": base.key, "base_name": base.name},
        owner=base.owner, version=base.version,
    )
