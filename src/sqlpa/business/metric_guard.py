"""
sqlpa.business.metric_guard
===========================
业务模式「注入 + 校验」护栏（防口径幻觉）：
  - build_constraint: 把权威"指标公式/join/过滤"作为硬约束喂给 Writer(LLM)。
  - verify_formula : 校验生成的 SQL 是否仍按配置使用该表达式，被改则拒绝。

这解决"LLM 生成 vs 配置拼"的矛盾：LLM 负责搭查询结构，业务层负责"给公式 + 校验公式没被改"。
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

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
    return re.sub(r"\s+", "", s).lower()


def verify_formula(sql: str, metric_expr: str) -> List[str]:
    """校验生成的 SQL 是否仍按配置公式计算。返回问题列表(空=通过)。"""
    if not sql:
        return ["生成为空"]
    if _norm(metric_expr) in _norm(sql):
        return []
    return [f"生成的SQL未按配置使用指标表达式「{metric_expr}」，已拦截(防口径篡改)"]
