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


def load_config(path: str | Path | None = None) -> BusinessConfig:
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
                                   support_filters=m.get("support_filters", []))
    dims = {d["key"]: Dimension(key=d["key"], name=d.get("name", d["key"]),
                                sql_fragment=d["sql_fragment"])
            for d in data.get("dimensions", [])}
    return BusinessConfig(alias=data.get("business_alias", {}),
                          metrics=metrics, dimensions=dims,
                          filter_templates=data.get("filter_templates", {}),
                          permissions=data.get("permissions", {}))


def metric_summary(cfg: BusinessConfig) -> str:
    """把可用指标+支持维度/过滤转成给 LLM 的上下文。"""
    lines = ["可用的业务指标："]
    for m in cfg.metrics.values():
        lines.append(f"  - {m.key}（{m.name}）: {m.desc}；"
                     f"支持维度[{','.join(m.support_dims)}] "
                     f"支持过滤[{','.join(m.support_filters)}]")
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
