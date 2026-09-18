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
from .sqlgen import inject_dim_joins, tables_of

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


# 标准维度 JOIN 池：维度所需的表如果不在指标自带的 JOIN 里，就自动补上。
# 这样"支持某维度"与"能查出该维度"不会再脱节（配置声明支持 → 编译器保证可执行）。
def compile_spec(cfg: BusinessConfig, spec: QuerySpec) -> CompiledQuery:
    """把 QuerySpec 确定性编译成 SQL。不合法则抛 CompileError（上层据此降级/拒绝）。"""
    derived = parse_derived(spec.metric)
    if derived:
        return _compile_derived(cfg, spec, derived)
    m = cfg.metrics.get(spec.metric)
    if not m:
        raise CompileError(f"未知指标: {spec.metric}")

    dim_keys = [d for d in (spec.dims or []) if d in cfg.dimensions]
    if len(dim_keys) != len(spec.dims or []):
        unknown = [d for d in (spec.dims or []) if d not in cfg.dimensions]
        raise CompileError(f"未知维度: {unknown}")

    dim_sqls = [dimension_sql(cfg, d, spec.time_grain if d == "dt" else None) for d in dim_keys]
    select_parts = [f"{m.metric_expr} AS {m.key}"]
    select_parts += [f"{frag} AS {d}" for d, frag in zip(dim_keys, dim_sqls)]

    where_parts = [m.where_core] if m.where_core and m.where_core not in ("", "1=1") else []
    for ftype, value in (spec.filters or []):
        where_parts.append(render_filter(cfg, ftype, value))

    sql = _build_sql(select_parts, m.from_clause,
                     inject_dim_joins(cfg, m, dim_keys), where_parts,
                     dim_sqls, order_expr=m.metric_expr, top=spec.top)
    return CompiledQuery(
        sql=sql, metric_key=m.key, metric_name=m.name, metric_expr=m.metric_expr,
        dims=dim_keys, filters={ft: str(v) for ft, v in (spec.filters or [])},
        sources=tables_of(m), owner=m.owner, version=m.version,
    )


def _merge_joins(*clauses: str) -> str:
    """把多段 JOIN 子句**按表别名去重**后合并。

    不能只按"子句文本"去重：两个指标可能各自写了 `oi` 的 JOIN（条件写法不同），
    文本去重会留下两条 `JOIN order_items oi`，导致 `ambiguous column name`。
    以别名为键去重才是正确的合并单位。
    """
    seen: Dict[str, str] = {}
    order: List[str] = []
    for clause in clauses:
        text = clause or ""
        for part in re.findall(
                r"(?:LEFT\s+|INNER\s+)?JOIN\s+[a-zA-Z_][\w]*(?:\s+(?:AS\s+)?[a-zA-Z_][\w]*)?\s+ON\s+[^;]*?"
                r"(?=(?:\s+(?:LEFT\s+|INNER\s+)?JOIN\s)|$)", text, re.I):
            part = part.strip()
            m = re.match(r"(?:LEFT\s+|INNER\s+)?JOIN\s+([a-zA-Z_][\w]*)(?:\s+(?:AS\s+)?([a-zA-Z_][\w]*))?",
                         part, re.I)
            if not m:
                continue
            alias = (m.group(2) or m.group(1)).lower()
            if alias not in seen:
                seen[alias] = part
                order.append(alias)
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


def _compile_derived(cfg: BusinessConfig, spec: QuerySpec, derived: Dict) -> CompiledQuery:
    """派生指标编译：ratio（两基础指标相除）与 share（占整体比例）。"""
    dim_keys = [d for d in (spec.dims or []) if d in cfg.dimensions]
    if len(dim_keys) != len(spec.dims or []):
        raise CompileError(f"未知维度: {[d for d in (spec.dims or []) if d not in cfg.dimensions]}")
    dim_sqls = [dimension_sql(cfg, d, spec.time_grain if d == "dt" else None) for d in dim_keys]

    if derived["kind"] == "ratio":
        a = cfg.metrics.get(derived["numerator"])
        b = cfg.metrics.get(derived["denominator"])
        if not a or not b:
            raise CompileError(f"ratio 引用了未知指标: {derived}")
        # 两个基础指标必须能落在同一个查询上下文里：把两者所需的 JOIN 合并。
        # 不用"from_clause 字符串相等"来判断同源（两指标的 from 常都是 'FROM orders o'，
        # 这种判断既漏检又脆弱）；合并 JOIN 后由 SQL 引擎裁定别名是否齐备，
        # 缺表会以 DB 错误暴露，而不是产出悄悄算错的口径。
        if a.from_clause != b.from_clause:
            raise CompileError("ratio 的分子分母主表不同，无法在同一查询内相除")
        base = _MergeProxy(a, b, inject_dim_joins(cfg, a, dim_keys),
                           inject_dim_joins(cfg, b, dim_keys))
        key = f"{a.key}_per_{b.key}"
        expr = (f"CASE WHEN ({b.metric_expr}) IS NULL OR ({b.metric_expr}) = 0 "
                f"THEN NULL ELSE ({a.metric_expr}) * 1.0 / ({b.metric_expr}) END")
        where_parts = []
        for m in (a, b):
            if m.where_core and m.where_core not in ("", "1=1"):
                where_parts.append(m.where_core)
        for ftype, value in (spec.filters or []):
            where_parts.append(render_filter(cfg, ftype, value))
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
    where_parts = [base.where_core] if base.where_core and base.where_core not in ("", "1=1") else []
    for ftype, value in (spec.filters or []):
        where_parts.append(render_filter(cfg, ftype, value))
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    total_sub = (f"(SELECT {base.metric_expr} {base.from_clause} "
                 f"{(base.join_clause or '').strip()}{where_sql})")
    expr = (f"CASE WHEN {total_sub} IS NULL OR {total_sub} = 0 THEN NULL "
            f"ELSE {base.metric_expr} * 1.0 / {total_sub} END")
    key = f"{base.key}_share"
    select_parts = [f"{expr} AS {key}"] + [f"{frag} AS {d}" for d, frag in zip(dim_keys, dim_sqls)]
    sql = _build_sql(select_parts, base.from_clause,
                     inject_dim_joins(cfg, base, dim_keys), where_parts,
                     dim_sqls, order_expr=expr, top=spec.top)
    return CompiledQuery(
        sql=sql, metric_key=key, metric_name=f"{base.name}占比", metric_expr=expr,
        dims=dim_keys, filters={ft: str(v) for ft, v in (spec.filters or [])},
        sources=tables_of(base),
        derived={"kind": "share", "base": base.key, "base_name": base.name},
        owner=base.owner, version=base.version,
    )
