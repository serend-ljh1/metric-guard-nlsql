"""
sqlpa.business.metric_matcher
=============================
MetricMatcher Agent：把业务人员的自然语言问题，识别为【指标 + 维度 + 过滤】。

分层：
  - 首选 LLM 意图识别（调用 llm.complete，从配置里的合法指标/维度/过滤去选）。
  - 失败/离线(Mock) 时用确定性关键词兜底。
  - 无论哪种，都会做「指标-维度-过滤」支持度校验，不支持则直接拒绝并给业务提示，
    不进入昂贵的下游 Agent 链路（节省 token/耗时）。

该层"只识别意图"，不写任何业务公式；公式由 assembler 从配置读取。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .metric_config import BusinessConfig, load_config, metric_summary, validate_support


@dataclass
class MatchResult:
    matched: bool
    metric_key: str = ""
    metric_name: str = ""
    dims: List[str] = field(default_factory=list)
    filters: List[Tuple[str, object]] = field(default_factory=list)
    method: str = ""                 # llm | keyword
    reject_reasons: List[str] = field(default_factory=list)
    raw: Dict = field(default_factory=dict)


_METRIC_KW = [
    ("gmv", ["gmv", "成交", "销售额", "总额", "gmv"]),
    ("order_count", ["订单数", "订单量", "多少单", "订单数量"]),
    ("aov", ["客单价", "单均", "平均每单", "aov"]),
    ("cancellation_rate", ["取消", "退货", "退款", "取消率", "退货率"]),
    ("late_delivery_rate", ["超时", "准时", "延迟送达", "晚到", "配送超时"]),
    ("avg_review", ["评分", "评价", "星级", "好评", "满意度"]),
]
_DIM_KW = [
    ("category", ["品类", "类别", "类目", "分类"]),
    ("state", ["州", "省", "省份"]),
    ("dt", ["按天", "每日", "每天"]),
]


def _first_hit(text: str, table) -> Optional[str]:
    for key, kws in table:
        if any(k.lower() in text.lower() for k in kws):
            return key
    return None


def _keyword_match(question: str) -> Tuple[str, List[str], List[Tuple[str, object]]]:
    mk = _first_hit(question, _METRIC_KW) or ""
    dims = []
    for dk, kws in _DIM_KW:
        if any(k in question for k in kws):
            dims.append(dk)
    filters: List[Tuple[str, object]] = []
    m = re.search(r"(上个月|上月|本月|这个月|最近\s*\d+\s*天|最近\d+天|\d{4}年\d{1,2}月|\d{4}-\d{1,2})", question)
    if m:
        filters.append(("time_range", m.group(1)))
    if "取消" in question and "品类" in question:
        pass  # 品类已作为 dim
    return mk, dims, filters


def _llm_match(cfg: BusinessConfig, question: str, llm) -> Dict:
    prompt = (
        "你是业务取数助手。把用户问题解析为指标+维度+过滤。\n"
        f"{metric_summary(cfg)}\n"
        "过滤类型只能是: " + ", ".join(cfg.filter_templates.keys()) + "\n"
        "只输出 JSON，格式: {\"metric\":\"<指标key>\",\"dims\":[\"<维度key>\"],"
        "\"filters\":[{\"type\":\"<过滤类型>\",\"value\":\"<值>\"}]}。\n"
        f"用户问题：{question}"
    )
    text = llm.complete(prompt)
    try:
        text = text.strip().strip("`").removeprefix("json").removeprefix("JSON")
        obj = json.loads(text)
    except Exception:
        return {}
    return obj


def match(question: str, cfg: BusinessConfig | None = None, llm=None) -> MatchResult:
    cfg = cfg or load_config()
    dims: List[str] = []
    filters: List[Tuple[str, object]] = []
    metric_key = ""
    method = ""

    # ---- 尝试 LLM 识别 ----
    if llm is not None:
        obj = _llm_match(cfg, question, llm)
        if obj and obj.get("metric") in cfg.metrics:
            metric_key = obj["metric"]
            dims = [d for d in obj.get("dims", []) if d in cfg.dimensions]
            for f in obj.get("filters", []) or []:
                ft = f.get("type")
                if ft in cfg.filter_templates:
                    filters.append((ft, f.get("value", "")))
            method = "llm"

    # ---- 关键词兜底 ----
    if not metric_key:
        mk, kw_dims, kw_filters = _keyword_match(question)
        metric_key = mk
        dims = kw_dims or dims
        filters = kw_filters or filters
        method = "keyword"

    if not metric_key:
        return MatchResult(matched=False, method=method,
                           reject_reasons=["未识别到业务指标(支持: " +
                                           ",".join(cfg.metrics.keys()) + ")"])

    # ---- 支持度校验（关键：不支持直接拦截，不进下游）----
    bad = validate_support(cfg, metric_key, dims, [f[0] for f in filters])
    m = cfg.metrics[metric_key]
    if bad:
        return MatchResult(matched=False, metric_key=metric_key,
                           metric_name=m.name, dims=dims, filters=filters,
                           method=method, reject_reasons=bad)
    return MatchResult(matched=True, metric_key=metric_key, metric_name=m.name,
                       dims=dims, filters=filters, method=method)
