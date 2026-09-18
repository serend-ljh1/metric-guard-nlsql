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
    time_grain: Optional[str] = None  # day/week/month/quarter（仅当 dims 含 dt）


_METRIC_KW = [
    # 规模类
    ("gmv", ["gmv", "成交", "销售额", "总额", "交易额"]),
    ("gross_revenue", ["总营收", "含运费", "营收"]),
    ("item_sales", ["商品销售额", "商品金额"]),
    ("avg_freight", ["平均运费", "单均运费", "运费均值"]),   # 需早于/长于 freight_cost
    ("freight_cost", ["运费总额", "运费合计", "物流总额", "运费", "物流费", "物流成本"]),
    ("aov", ["客单价", "单均", "平均每单", "aov"]),
    # 订单类
    ("order_count", ["订单数", "订单量", "多少单", "订单数量"]),
    ("paid_order_count", ["已支付订单", "支付订单"]),
    ("canceled_order_count", ["取消订单数", "取消的单"]),
    ("item_count", ["商品件数", "件数", "销量"]),
    # 客户类
    ("customer_count", ["下单客户", "客户数", "买家数"]),
    ("unique_customer_count", ["独立买家", "去重买家", "购买用户数"]),
    ("items_per_order", ["每单件数", "客单结构"]),
    # 商品 / 卖家类
    ("product_count", ["商品数", "在售商品", "sku"]),
    ("seller_count", ["卖家", "商家数", "供应商数"]),
    # 风险类
    ("cancellation_rate", ["取消率", "退货", "退款"]),
    ("late_delivery_rate", ["超时送达", "延迟送达", "晚到", "配送超时"]),
    ("on_time_delivery_rate", ["准时", "按时送达"]),
    ("avg_delivery_days", ["履约天数", "平均送达", "配送时长", "交付周期"]),
    # 满意度类
    ("avg_review", ["评分", "星级", "满意度"]),
    ("positive_review_rate", ["好评率", "好评"]),
    ("negative_review_rate", ["差评率", "差评"]),
    ("review_count", ["评价数", "评价条数"]),
]
_DIM_KW = [
    ("category", ["品类", "类别", "类目", "分类"]),
    ("state", ["州", "省", "省份"]),
    ("status", ["订单状态", "状态"]),
    ("dt", ["按天", "每日", "每天", "按月", "每月", "按周", "每周", "趋势"]),
]

# 时间粒度关键词 -> compiler.TIME_GRAINS 的键
_GRAIN_KW = [
    ("month", ["按月", "每月", "月度"]),
    ("week", ["按周", "每周", "周度"]),
    ("quarter", ["按季", "每季", "季度"]),
    ("day", ["按天", "每日", "每天", "日度"]),
]

# 派生指标关键词（必须**先于**基础指标匹配，否则"运费占比"会被当成"运费"）
_DERIVED_KW = [
    ("ratio@freight_cost/gmv", ["运费占比", "运费率", "物流成本占比"]),
    ("ratio@gmv/item_count", ["件单价", "平均件价"]),
    ("share@canceled_order_count", ["取消占比", "取消订单占比"]),
]


def _extract_grain(text: str) -> Optional[str]:
    for grain, kws in _GRAIN_KW:
        if any(k in text for k in kws):
            return grain
    return None


def _first_hit(text: str, table) -> Optional[str]:
    """把问题里的指标词解析为指标 key。

    规则：**最长匹配优先**（同长度取表中靠前者）。
    为什么不能只取"第一个命中"：宽泛词会抢走具体词——例如 "总额" 让 "运费总额"
    被判成 GMV；而 "平均运费" 又必须优先于 "运费"。按匹配长度打分可同时解决这两类
    冲突，比人工维护关键词顺序更稳、也更好解释。
    """
    t = text.lower()
    best, best_len = None, 0
    for key, kws in table:
        for kw in kws:
            k = kw.lower()
            if k in t and len(k) > best_len:
                best, best_len = key, len(k)
    return best


def _keyword_match(question: str) -> Tuple[str, List[str], List[Tuple[str, object]]]:
    mk = (_first_hit(question, _DERIVED_KW) or _first_hit(question, _METRIC_KW) or "")
    dims = []
    for dk, kws in _DIM_KW:
        if any(k in question for k in kws):
            dims.append(dk)
    filters: List[Tuple[str, object]] = []
    m = re.search(r"(上个月|上月|本月|这个月|最近\s*\d+\s*天|最近\d+天|\d{4}年\d{1,2}月|\d{4}-\d{1,2})", question)
    if m:
        filters.append(("time_range", m.group(1)))
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
        got = (obj or {}).get("metric", "")
        # 允许基础指标与派生指标（ratio@a/b、share@a）
        if got and (got in cfg.metrics or got.startswith(("ratio@", "share@"))):
            metric_key = got
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
                                           ",".join(cfg.metrics.keys()) + ")"],
                           time_grain=_extract_grain(question))

    grain = _extract_grain(question)

    # ---- 派生指标：不走 validate_support（它按基础指标的支持度校验）----
    if metric_key.startswith(("ratio@", "share@")):
        from .compiler import parse_derived
        try:
            parse_derived(metric_key)          # 语法自检
        except Exception as e:                 # noqa: BLE001
            return MatchResult(matched=False, metric_key=metric_key, method=method,
                               reject_reasons=[f"派生指标语法错误: {e}"], time_grain=grain)
        if metric_key.startswith("share@") and not dims:
            return MatchResult(matched=False, metric_key=metric_key, method=method,
                               reject_reasons=["占比类指标需要指定分组维度（如：按品类）"],
                               time_grain=grain)
        return MatchResult(matched=True, metric_key=metric_key,
                           metric_name=_derived_name(cfg, metric_key), dims=dims,
                           filters=filters, method=method, time_grain=grain)

    # ---- 支持度校验（关键：不支持直接拦截，不进下游）----
    bad = validate_support(cfg, metric_key, dims, [f[0] for f in filters])
    m = cfg.metrics[metric_key]
    if bad:
        return MatchResult(matched=False, metric_key=metric_key,
                           metric_name=m.name, dims=dims, filters=filters,
                           method=method, reject_reasons=bad, time_grain=grain)
    return MatchResult(matched=True, metric_key=metric_key, metric_name=m.name,
                       dims=dims, filters=filters, method=method, time_grain=grain)


def _derived_name(cfg: BusinessConfig, metric_key: str) -> str:
    """给派生指标一个人类可读的名字（优先取配置里的 name）。"""
    for d in (getattr(cfg, "derived_metrics", None) or {}).values():
        if d.get("key") == metric_key:
            return d.get("name", metric_key)
    return metric_key
