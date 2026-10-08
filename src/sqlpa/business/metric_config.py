"""
sqlpa.business.metric_config
============================
加载业务语义层配置（业务词典 / 指标 / 维度 / 过滤模板），并提供查询辅助。

角色：这是"业务口径"的权威来源。LLM 只做意图识别，业务公式一律从这读取。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

_DEFAULT = Path(__file__).resolve().parent / "business_config.yaml"


@dataclass
class Metric:
    key: str
    name: str
    desc: str
    metric_expr: str
    from_clause: str
    where_core: str
    support_dims: List[str] = field(default_factory=list)
    support_filters: List[str] = field(default_factory=list)
    owner: str = ""
    version: str = "v1"
    # 可选的额外 JOIN 子句：把 join 从 from_clause 里拆出来，便于编译器
    # 组合 ratio/share 等派生指标时判断"两个基础指标是否同源"。
    join_clause: str = ""
    # 该指标在维度上是否**可加**（分段变化之和 == 总变化）。
    #   True  → 允许用"占波动 X%"表述维度贡献；
    #   False → 比率/均值类，分段 delta 之和天然不等于总变化（用 percentages 就是错的）；
    #   None  → 未显式声明，按表达式启发式推断（含 "/" 或 AVG( 视为不可加）。
    # 归因层还有一次**数据侧守恒自检**兜底：即使声明为可加，只要分段之和对不上总变化，
    # 也会自动降级为"仅供参照"，不会把错误占比写进结论与报告。
    additive: Optional[bool] = None
    # 比率类指标的**权重指标**（用于 rate/mix 量价分解）：应填"该比率的分母"对应指标，
    # 例如 aov = GMV/paid_order_count → weight_metric=paid_order_count。
    # 归因层会先用它**重建总值**校验：对不上就拒绝分解（避免给出看似精致的错结论）。
    weight_metric: str = ""

    def to_dict(self) -> dict:
        return self.__dict__


@dataclass
class Dimension:
    key: str
    name: str
    sql_fragment: str


@dataclass
class BusinessConfig:
    alias: Dict[str, Dict] = field(default_factory=dict)
    metrics: Dict[str, Metric] = field(default_factory=dict)
    dimensions: Dict[str, Dimension] = field(default_factory=dict)
    filter_templates: Dict[str, str] = field(default_factory=dict)
    permissions: Dict = field(default_factory=dict)
    # 派生指标（ratio/share 等）：由 compiler 在编译期展开，配置只声明定义
    derived_metrics: Dict[str, Dict] = field(default_factory=dict)
    # 关键词兜底匹配词表（可选）。声明后**整体替换**内置中文词表，
    # 使非中文语料/其它业务域不必改代码即可接入确定性匹配路径。
    # 结构: {metrics:{key:[kw..]}, derived:{key:[kw..]}, dimensions:{key:[kw..]},
    #        grains:{day|week|month|quarter:[kw..]}}
    matcher_keywords: Dict = field(default_factory=dict)
    # 过滤算子白名单：{过滤类型: {算子名: SQL 记号}}。
    # 规格里只出现算子名，SQL 记号只能来自这里 —— 算子与取值一样走白名单，
    # 绝不把规格里的字符串直接拼进 SQL。未声明的过滤类型只允许等值。
    filter_ops: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # HAVING（对本指标聚合值做后置筛选）的算子白名单，同上。
    metric_ops: Dict[str, str] = field(default_factory=dict)

    def derived_key(self, d: Dict) -> str:
        """把派生指标定义转成 compiler 能解析的 key（ratio@a/b 或 share@a）。"""
        kind = (d.get("kind") or "").strip()
        if kind == "ratio":
            return f"ratio@{d.get('numerator')}/{d.get('denominator')}"
        if kind == "share":
            return f"share@{d.get('base')}"
        return ""


def _resolve_filter_values(mk: Dict, value_provider) -> Dict:
    """把 `matcher_keywords.filter_values` 解析成 过滤类型 -> 值清单。

    支持三种声明：
      - `{type: [v1, v2, ...]}`                       显式枚举（适合"official"这类触发词）
      - `{type: {column: "表.列"}}`                   值域来自数仓自身的列（建层时取 distinct）
      - `{type: {column: ..., aliases: {词形: 值}}}`   词形别名（"African" -> "Africa"）
    未提供 value_provider 时，`column` 形式的条目**跳过**（宁可少一层过滤能力，
    也不要凭空编造字典外的取值 —— 值词表必须是真实数据里的值）。

    为什么需要词形别名：自然语言会用形容词而不是库里存的名词（库里是 `Africa`，
    问句写的是 `African countries`）。别名不处理就会"过滤器静默丢失" —— 查询照跑、
    结果变成全局口径，而用户以为已经按洲过滤了。别名表把这种漂移变成显式配置。
    """
    out: Dict[str, List[str]] = {}
    aliases_out: Dict[str, Dict[str, str]] = {}
    multi_out: Dict[str, str] = {}
    for ftype, spec in (mk.get("filter_values") or {}).items():
        vals: List[str] = []
        aliases: Dict[str, str] = {}
        if isinstance(spec, dict):
            if spec.get("values"):
                vals = [str(v) for v in spec["values"]]
            elif spec.get("column") and value_provider is not None:
                table, _, col = str(spec["column"]).partition(".")
                try:
                    vals = [str(v) for v in (value_provider(table, col) or []) if v not in (None, "")]
                except Exception:            # noqa: BLE001 — 取值域失败就退化为"无该过滤"
                    vals = []
            for al, canon in (spec.get("aliases") or {}).items():
                if str(canon):
                    aliases[str(al)] = str(canon)
            # `multi` 是"该取值域的多取值语义"声明，必须随解析结果一起带出去 ——
            # 否则匹配器看不到它，多取值就只能一律拒答（实测踩过：声明了 multi: or
            # 仍然被拒，因为解析器只保留了取值清单）。
            if spec.get("multi"):
                multi_out[ftype] = str(spec["multi"])
        elif isinstance(spec, (list, tuple)):
            vals = [str(v) for v in spec]
        if vals:
            out[ftype] = vals
        if aliases:
            aliases_out[ftype] = aliases
    if aliases_out:
        mk["filter_value_aliases"] = aliases_out
    if multi_out:
        mk["filter_multi"] = multi_out
    return out


def load_config(path: str | Path | None = None, value_provider=None) -> BusinessConfig:
    p = Path(path or _DEFAULT)
    data = yaml.safe_load(open(p, encoding="utf-8")) or {}
    metrics = {}
    for m in data.get("metrics", []):
        metrics[m["key"]] = Metric(key=m["key"], name=m.get("name", m["key"]),
                                   desc=m.get("desc", ""),
                                   metric_expr=m["metric_expr"],
                                   from_clause=m.get("from_clause", ""),
                                   where_core=m.get("where_core", "1=1"),
                                   support_dims=m.get("support_dims", []),
                                   support_filters=m.get("support_filters", []),
                                   owner=m.get("owner", "未指定"),
                                   version=m.get("version", "v1"),
                                   join_clause=m.get("join_clause", ""),
                                   additive=(None if m.get("additive") is None
                                             else bool(m.get("additive"))),
                                   weight_metric=(m.get("weight_metric") or "").strip())
    dims = {d["key"]: Dimension(key=d["key"], name=d.get("name", d["key"]),
                                sql_fragment=d["sql_fragment"])
            for d in data.get("dimensions", [])}
    mk = dict(data.get("matcher_keywords") or {})
    if mk.get("filter_values"):
        mk["filter_values"] = _resolve_filter_values(mk, value_provider)
    cfg = BusinessConfig(alias=data.get("business_alias", {}),
                         metrics=metrics, dimensions=dims,
                         filter_templates=data.get("filter_templates", {}),
                         permissions=data.get("permissions", {}),
                         matcher_keywords=mk,
                         filter_ops=data.get("filter_ops", {}) or {},
                         metric_ops=data.get("metric_ops", {}) or {})
    for d in (data.get("derived_metrics") or []):
        k = cfg.derived_key(d)
        if k:
            cfg.derived_metrics[k] = d
    return cfg


def metric_summary(cfg: BusinessConfig) -> str:
    """把可用指标+支持维度/过滤转成给 LLM 的上下文。"""
    lines = ["可用的业务指标："]
    for m in cfg.metrics.values():
        lines.append(f"  - {m.key}（{m.name}）: {m.desc}；"
                     f"支持维度[{','.join(m.support_dims)}] "
                     f"支持过滤[{','.join(m.support_filters)}]")
    for k, d in (cfg.derived_metrics or {}).items():
        lines.append(f"  - {k}（{d.get('name')}）: {d.get('desc','')}  [派生指标]")
    lines.append("可用维度: " + ", ".join(f"{d.key}({d.name})" for d in cfg.dimensions.values()))
    return "\n".join(lines)


def validate_support(cfg: BusinessConfig, metric_key: str,
                     dims: List[str], filters: List[str]) -> List[str]:
    """校验指标是否支持所要求的维度/过滤，返回不支持项列表（空=合法）。"""
    m = cfg.metrics.get(metric_key)
    if not m:
        return [f"未知指标 {metric_key}"]
    bad = []
    for d in dims:
        if d not in m.support_dims:
            dname = cfg.dimensions[d].name if d in cfg.dimensions else d
            bad.append(f"{m.name} 不支持按「{dname}」统计")
    for f in filters:
        if f not in m.support_filters:
            bad.append(f"{m.name} 不支持「{f}」过滤")
    return bad
