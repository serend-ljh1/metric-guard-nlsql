"""
sqlpa.business.assembler
========================
确定性 SQL 组装器：把「指标(配置) + 维度 + 过滤」拼成完整 SQL。

核心价值：**业务指标的分子分母公式只来自配置文件**，LLM 只识别意图、不写公式。
本模块纯代码，无 LLM，保证口径不被篡改。
"""
from __future__ import annotations

import datetime
import re
from typing import Dict, List, Optional, Tuple

from .metric_config import BusinessConfig, Metric, Dimension


def _quote(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _render_value(value) -> str:
    """单个值 -> 等值；列表 -> IN (...)。"""
    if isinstance(value, (list, tuple)):
        return "(" + ",".join(_quote(x) for x in value) + ")"
    return _quote(value)


def _first_of_month(d: datetime.date) -> datetime.date:
    return d.replace(day=1)


def resolve_time_range(spec: str, now: Optional[datetime.date] = None) -> Tuple[str, str]:
    """把时间语义(上个月/本月/最近N天/YYYY-MM)转成 (start, end) 的SQL字面量。"""
    now = now or datetime.date.today()
    s = str(spec).strip()
    m = re.fullmatch(r"(\d{4})[年\-](\d{1,2})月?", s)
    if m:  # 指定月份：2026-08 或 2026年8月
        y, mo = int(m.group(1)), int(m.group(2))
        start = datetime.date(y, mo, 1)
        end = _first_of_month(datetime.date(y + (mo == 12), ((mo % 12) + 1), 1))
        return _quote(start.isoformat()), _quote(end.isoformat())
    if "上月" in s or "上个月" in s:
        fm = _first_of_month(now)
        start = _first_of_month(fm - datetime.timedelta(days=1))
        return _quote(start.isoformat()), _quote(fm.isoformat())
    if "本月" in s or "这个月" in s:
        fm = _first_of_month(now)
        return _quote(fm.isoformat()), _quote(_first_of_month(fm + datetime.timedelta(days=31)).isoformat())
    mm = re.search(r"(\d+)\s*天|最近(\d+)天", s)
    if mm:
        n = int(mm.group(1) or mm.group(2))
        start = now - datetime.timedelta(days=n)
        return _quote(start.isoformat()), _quote(now.isoformat())
    # 兜底：最近30天
    start = now - datetime.timedelta(days=30)
    return _quote(start.isoformat()), _quote(now.isoformat())


def render_filter(cfg: BusinessConfig, ftype: str, value) -> str:
    """把单个过滤条件渲染成 SQL 片段。"""
    tmpl = cfg.filter_templates.get(ftype)
    if not tmpl:
        raise ValueError(f"未知过滤类型: {ftype}")
    if ftype == "time_range":
        start, end = resolve_time_range(value)
        return tmpl.format(start=start, end=end)
    return tmpl.format(value=_render_value(value))


def assemble(cfg: BusinessConfig, metric_key: str,
             dims: Optional[List[str]] = None,
             filters: Optional[List[Tuple[str, object]]] = None,
             top: int = 0) -> Dict:
    """组装 SQL。返回 {sql, metric_name, dims, filters, guarded: dict}"""
    m = cfg.metrics.get(metric_key)
    if not m:
        raise ValueError(f"未知指标 {metric_key}")
    dim_keys = dims or []
    filter_pairs = filters or []

    # SELECT 字段：指标表达式 + 维度
    select_parts = [f"{m.metric_expr} AS {m.key}"]
    dim_sql = [cfg.dimensions[d].sql_fragment for d in dim_keys if d in cfg.dimensions]
    for d, frag in zip(dim_keys, dim_sql):
        select_parts.append(f"{frag} AS {d}")

    # WHERE
    where_parts = [m.where_core] if m.where_core and m.where_core not in ("", "1=1") else []
    for ftype, value in filter_pairs:
        where_parts.append(render_filter(cfg, ftype, value))
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    # GROUP BY
    group_sql = ("GROUP BY " + ", ".join(dim_sql)) if dim_sql else ""

    order_sql = f"ORDER BY {m.metric_expr} DESC" if dim_keys else ""
    limit_sql = f"LIMIT {int(top)}" if top else ""

    sql = (f"SELECT {', '.join(select_parts)}\n"
           f"{m.from_clause}\n"
           f"{where_sql}\n"
           f"{group_sql}\n"
           f"{order_sql}\n"
           f"{limit_sql}").strip()

    return {
        "sql": sql,
        "metric_name": m.name,
        "metric_expr": m.metric_expr,
        "dims": dim_keys,
        "filters": {ft: str(v) for ft, v in filter_pairs},
        "guarded": {"formula_from_config": True},  # 口径来自配置，非LLM
    }
