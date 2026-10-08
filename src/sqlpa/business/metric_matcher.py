"""
sqlpa.business.metric_matcher
=============================
MetricMatcher Agent：把业务人员的自然语言问题，识别为【指标 + 维度 + 过滤】。

分层：
  - 首选 LLM 意图识别（调用 llm.complete，从配置里的合法指标/维度/过滤去选）。
  - 失败/离线(Mock) 时用确定性关键词兜底。
  - 无论哪种，都会做「指标-维度-过滤」支持度校验，不支持则直接拒绝并给业务提示，
    不进入昂贵的下游 Agent 链路（节省 token/耗时）。

该层"只识别意图"，不写任何业务公式；公式由 compiler 从配置读取并确定性编译。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .metric_config import BusinessConfig, load_config, metric_summary, validate_support


@dataclass
class MatchResult:
    matched: bool
    metric_key: str = ""
    metric_name: str = ""
    dims: List[str] = field(default_factory=list)
    filters: List[Tuple[str, object]] = field(default_factory=list)
    having: List[Tuple[str, object]] = field(default_factory=list)   # 聚合后筛选（组内）
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


def _extract_grain(text: str, table=None) -> Optional[str]:
    for grain, kws in (table or _GRAIN_KW):
        if any(k in text for k in kws):
            return grain
    return None


# 并列连接词：多指标识别按这些词切段（中英兼顾）
_CONJ_RE = re.compile(r"\b(?:and|plus|as well as|along with)\b|、|和|与|及|,|，|;|；", re.I)


def _filter_value_hits(question: str, cfg: BusinessConfig
                       ) -> Tuple[List[Tuple[str, str, Tuple[int, int]]], Dict[str, List[str]]]:
    """抽取过滤取值，返回 ([(类型, 规范取值, 出现区间)], 同类型多取值冲突表)。

    为什么需要长度优先 + 跨度互斥：同一段文字可能同时命中两个词表的取值
    （"Central Africa" 既是 region 取值、又含 continent 取值 "Africa"）。
    取最长命中并丢弃与它重叠的短命中，才能保证"值更长=更具体"的直觉成立；
    否则 `region=Central Africa` 会被错误地额外加上 `continent=Africa`，
    变成两个过滤条件相与 —— 结果集更小却看不出错。

    为什么还要返回**区间**：否定识别（"do not speak English"）必须判断否定词是否
    就在取值附近，否则一句里出现的任意 "not" 都会把无关过滤条件翻成否定形态。
    """
    mk = (cfg.matcher_keywords or {})
    fv = mk.get("filter_values") or {}
    if not fv:
        return [], {}
    aliases = mk.get("filter_value_aliases") or {}
    ql = question.lower()
    # 序号参与排序：同一段文字同时是多个词表的取值时（实测 world 库里
    # **"Caribbean" 既是 region 取值、也是 countrylanguage 里的一种语言名**），
    # 若让 ftype 参与比较就会按字母序选出 language → 过滤条件从"地区"漂成"语言"、
    # 再被 support_filters 拒掉（一个本该答对的问题变成拒答）。
    # 取值域之间的优先顺序必须是配置里声明的顺序，显式且可解释。
    cands: List[Tuple[int, int, int, int, str, str]] = []
    seq = 0
    for ftype, values in fv.items():
        if ftype not in cfg.filter_templates:
            continue
        pairs = [(str(v), str(v)) for v in (values or [])]
        pairs += [(str(al), str(canon)) for al, canon in (aliases.get(ftype) or {}).items()]
        for surface, canon in pairs:
            surface = surface.strip()
            if not surface:
                continue
            pat = r"(?<![0-9a-z])" + re.escape(surface.lower()) + r"(?![0-9a-z])"
            for m in re.finditer(pat, ql):
                cands.append((-(m.end() - m.start()), m.start(), m.end(), seq, ftype, canon))
            seq += 1
    cands.sort(key=lambda x: x[:4])
    kept: List[Tuple[str, str, Tuple[int, int]]] = []
    spans: List[Tuple[int, int]] = []
    for _neg_len, s, e, _seq, ftype, v in cands:
        same_type = [x for t, x, _sp in kept if t == ftype]
        if v in same_type:
            continue                      # 同一取值的重复出现不算"多取值"
        if any(s < e2 and s2 < e for s2, e2 in spans):
            continue          # 与已保留的更长取值重叠 -> 丢弃（更具体的那个已入选）
        spans.append((s, e))
        kept.append((ftype, str(v), (s, e)))   # 同类型多取值也全部保留（按出现顺序）
    # 冲突表：同类型出现 ≥2 个不同取值 → 调用方据此决定"能否表达成 IN"或拒答
    conflicts: Dict[str, List[str]] = {}
    for ft, v, _sp in kept:
        conflicts.setdefault(ft, [])
        if v not in conflicts[ft]:
            conflicts[ft].append(v)
    conflicts = {ft: vs for ft, vs in conflicts.items() if len(vs) > 1}
    return kept, conflicts


def _filter_values_from_text(question: str, cfg: BusinessConfig):
    """兼容形态：只返回 (取值列表, 冲突表)，区间信息内部使用。"""
    hits, conflicts = _filter_value_hits(question, cfg)
    return [(ft, v) for ft, v, _sp in hits], conflicts


_NUM = r"(\d+(?:\.\d+)?)"


def _as_number(text: str):
    f = float(text)
    return int(f) if f.is_integer() else f


def _cue_filters(question: str, spec: Dict, cfg: BusinessConfig, into: str
                 ) -> Tuple[List[Tuple[str, object]], List[Tuple[int, int]]]:
    """按配置的"线索词 + 数值"抽取比较过滤；返回 (过滤条件, 已消费区间)。

    `into="filters"` → 行级比较（WHERE）；`into="having"` → 聚合后比较（HAVING）。
    两者共用同一套抽取逻辑，因为差别只在"这个数字约束的是行还是聚合值"，
    而那正是配置里放在哪个段决定的。

    为什么必须记录"已消费区间"：核心守卫会把"否定/比较/年份区间"判为语义层表达不了的句式
    而拒答。一旦某个线索被成功表达成了过滤条件，就必须把它标记为**已消费**，
    否则守卫会把一个已经答对的问题拦掉 —— 守卫与表达能力必须联动，不能各自为政。
    """
    ql = question.lower()
    out: List[Tuple[str, object]] = []
    consumed: List[Tuple[int, int]] = []
    for ftype, fspec in (spec or {}).items():
        if into == "filters" and ftype not in cfg.filter_templates:
            continue
        ops = (fspec or {}).get("cues") or {}
        for op, cues in ops.items():
            for cue in cues or []:
                c = re.escape(str(cue).lower())
                if op == "between":
                    m = re.search(c + r"[^0-9]{0,24}?" + _NUM + r"[^0-9]{1,16}?" + _NUM, ql)
                    if not m:
                        continue
                    val: object = [_as_number(m.group(1)), _as_number(m.group(2))]
                else:
                    m = re.search(c + r"[^0-9]{0,24}?" + _NUM, ql)
                    if not m:
                        continue
                    val = _as_number(m.group(1))
                out.append((ftype, {"op": op, "value": val}))
                consumed.append(m.span())
                break
    return out, consumed


def _apply_negation(question: str, cfg: BusinessConfig,
                    filters: List[Tuple[str, object]],
                    spans: List[Tuple[str, Tuple[int, int]]]
                    ) -> Tuple[List[Tuple[str, object]], List[Tuple[int, int]]]:
    """把命中否定线索的过滤条件换成配置声明的"否定形态"过滤类型。

    返回 (新过滤条件, **新增**被消费的区间)。无变化时新增区间为空 —— 调用方直接累加，
    不要返回原始区间，否则会把 (类型, 区间) 这种复合结构混进纯区间列表里。

    为什么需要：`不使用英语的国家总人口` 与 `使用英语的国家总人口` 在规格里长得一样
    （都是 language=English），只有否定词不同。旧做法是把否定句式**整体拒答**，
    于是一大类真实业务问法（"不含运费"、"未支付订单"、"非取消订单"）永远答不了。

    换成**配置声明的独立过滤类型**（其模板是自包含的反连接子查询）之后：
      - 语义差异显式落在配置里，可审、可测、可复用；
      - 匹配器只负责"识别否定 + 选形态"，不拼 SQL；
      - 找不到对应形态时**照旧拒答**（不猜），安全边界不变。
    """
    neg = (cfg.matcher_keywords or {}).get("negated_filters") or {}
    if not neg or not filters:
        return filters, []
    ql = question.lower()
    cues = [str(c).lower() for c in (neg.get("cues") or [])]
    extra: List[Tuple[int, int]] = []
    for base_ftype, spec in neg.items():
        if base_ftype == "cues" or not isinstance(spec, dict):
            continue          # 顶层 cues 是全局线索词，不是形态定义
        variants = (spec or {}).get("variants") or []
        if not variants:
            continue
        vcues = cues + [str(c).lower() for c in ((spec or {}).get("cues") or [])]
        present = {ft: v for ft, v in filters}
        if base_ftype not in present:
            continue
        vspan = next((sp for ft, sp in spans if ft == base_ftype), None)
        hit = False
        if vspan and vcues:
            lo, hi = max(0, vspan[0] - 90), vspan[1] + 90
            window = ql[lo:hi]
            hit = any(re.search(r"(?<![0-9a-z])" + re.escape(c) + r"(?![0-9a-z])", window)
                      for c in vcues)
        if not hit:
            continue
        # 选"与当前过滤组合最贴合"的形态：requires 全部在场，且要求最多者优先
        chosen = None
        for v in sorted(variants, key=lambda x: -len(x.get("requires") or [])):
            req = list(v.get("requires") or [])
            if all(r in present for r in req):
                chosen = v
                break
        if not chosen:
            continue
        drop = {base_ftype} | set(chosen.get("requires") or [])
        filters = [(ft, val) for ft, val in filters if ft not in drop]
        filters.append((chosen["filter"], present[base_ftype]))
        # 否定词与取值附近这段文字都算"已消费"：守卫不该再因它拒答
        if vspan:
            extra.append((max(0, vspan[0] - 90), vspan[1] + 90))
    return filters, extra


def _metric_candidates(question: str, cfg: BusinessConfig) -> List[Tuple[str, int]]:
    """按"命中词长度降序（同长按表序）"给出候选指标及其命中词长度。"""
    tables = _cfg_table(cfg, "derived", _DERIVED_KW) + _cfg_table(cfg, "metrics", _METRIC_KW)
    t = question.lower()
    scored: List[Tuple[int, int, str]] = []
    order = {k: i for i, k in enumerate(dict.fromkeys([k for k, _ in tables]))}
    for key, kws in tables:
        best = 0
        for kw in kws:
            k = str(kw).lower()
            if k in t and len(k) > best:
                best = len(k)
        if best:
            scored.append((best, order.get(key, 999), key))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out: List[Tuple[str, int]] = []
    seen = set()
    for length, _o, k in scored:
        if k not in seen:
            seen.add(k)
            out.append((k, length))
    return out


def _resolve_metric_by_support(cfg: BusinessConfig, question: str,
                              dims: Sequence[str], ftypes: Sequence[str]) -> str:
    """措辞**真并列**时，用"维度/过滤落在哪张事实表"来消歧。

    动机（外部基准暴露）：`How many people live in Gelderland district?` 与
    `How many people live in Asia?` 措辞几乎一样，区别只在**过滤落在哪个列**：
    District 在 city 表、Continent 在 country 表。若只按关键词取最长命中，二者会被
    解析成同一个指标（其中一个必然算错）。

    **只允许在"命中词长度相同"的候选之间消歧**（本函数的第一道门）：
    长度不同意味着用户用的词本身更具体，换指标就等于**换了口径**。
    实测回归：`每单件数按订单状态` 的"每单件数"（4 字）比"件数"（2 字）更具体，
    若允许跨长度替换，就会被静默改答成"商品件数按订单状态" —— 用户问 A 得到 B，
    比明确拒绝危险得多。同长候选才是真正的"措辞无法区分"。

    没有候选满足时不在这里报错（仍返回最长命中的那个），让 compiler/validate_support
    给出"该指标不支持某维度"的可读拒绝。
    """
    cands = _metric_candidates(question, cfg)
    if not cands:
        return ""
    top_len = cands[0][1]
    for k, length in cands:
        if length != top_len:
            break                    # 更短=更宽泛的措辞，不许顶替
        m = cfg.metrics.get(k)
        if m is None:
            return k                      # 派生指标：支持度由其分子分母校验
        if m.support_filters and any(ft not in m.support_filters for ft in ftypes):
            continue
        if m.support_dims and any(d not in m.support_dims for d in dims):
            continue
        return k
    return cands[0][0] if cands else ""


@dataclass
class KeywordMatch:
    """关键词路径的完整解析结果（含"哪些线索已被成功表达"）。"""
    metric: str = ""
    dims: List[str] = field(default_factory=list)
    filters: List[Tuple[str, object]] = field(default_factory=list)
    having: List[Tuple[str, object]] = field(default_factory=list)
    conflicts: Dict[str, List[str]] = field(default_factory=dict)
    consumed: List[Tuple[int, int]] = field(default_factory=list)   # 已表达的线索区间


def _multi_value_ok(cfg: BusinessConfig, ftype: str) -> bool:
    """该过滤类型是否允许"多取值"（IN，或语义）。

    这是**语义判断，必须显式配置**而不是靠推断：同一个列在两种问题里含义不同。
      - `continent IN (Asia, Europe)`：一个国家只有一个洲 → 或 ✅
      - `language IN (English, Dutch)`：一个国家可以有多条语言行 → 用户说的是"两者都会"，
        是**与**语义（需要 INTERSECT/EXISTS），用 IN 会答成"会其中一种" —— 一个
        数值看着合理的错答。
    所以由配置在取值域上声明 `multi: or`（允许）或 `multi: and`（不允许，拒答）。
    """
    fv = ((cfg.matcher_keywords or {}).get("filter_values") or {}).get(ftype)
    multi = ((cfg.matcher_keywords or {}).get("filter_multi") or {}).get(ftype)
    if not fv or str(multi or "").lower() != "or":
        return False
    tmpl = cfg.filter_templates.get(ftype, "")
    return "{op}" in tmpl and "in" in ((cfg.filter_ops or {}).get(ftype) or {})


def _keyword_match(question: str, cfg: BusinessConfig | None = None) -> KeywordMatch:
    """确定性关键词解析：指标 + 维度 + 过滤（含算子/否定/多取值）+ 聚合后过滤。"""
    derived_t = _cfg_table(cfg, "derived", _DERIVED_KW)
    metric_t = _cfg_table(cfg, "metrics", _METRIC_KW)
    dim_t = _cfg_table(cfg, "dimensions", _DIM_KW)
    res = KeywordMatch()
    for dk, kws in dim_t:
        if any(k.lower() in question.lower() for k in kws):
            res.dims.append(dk)
    m = re.search(r"(上个月|上月|本月|这个月|最近\s*\d+\s*天|最近\d+天|\d{4}年\d{1,2}月|\d{4}-\d{1,2})", question)
    if m:
        res.filters.append(("time_range", m.group(1)))
        res.consumed.append(m.span())

    if cfg is not None:
        hits, conflicts = _filter_value_hits(question, cfg)
        spans = [(ft, sp) for ft, _v, sp in hits]
        # 同类型多取值 → 能表达则合并成一次 IN，不能表达则记为冲突（上层拒答）
        values: List[Tuple[str, object]] = []
        grouped: Dict[str, List[str]] = {}
        for ft, v, _sp in hits:
            if v not in grouped.setdefault(ft, []):
                grouped[ft].append(v)          # 已按出现顺序排列
        for ft, vals in grouped.items():
            if len(vals) > 1 and _multi_value_ok(cfg, ft):
                values.append((ft, list(vals)))
                conflicts.pop(ft, None)   # 已表达为 IN → 不再算冲突
            else:
                values.append((ft, vals[0]))
        res.filters += values
        res.conflicts = conflicts
        res.consumed += [sp for _ft, sp in spans]

        # 否定形态（配置声明的独立过滤类型）
        res.filters, neg_spans = _apply_negation(question, cfg, res.filters, spans)
        res.consumed += neg_spans

        # 数值比较：行级 → WHERE；聚合后 → HAVING
        mk = cfg.matcher_keywords or {}
        wf, w_spans = _cue_filters(question, mk.get("numeric_filters") or {}, cfg, "filters")
        res.filters += wf
        res.consumed += w_spans
        hf, h_spans = _cue_filters(question, mk.get("having_filters") or {}, cfg, "having")
        # HAVING 段里 ftype 固定为 `metric`：表示"筛选作用在本指标的聚合值上"。
        # 规格里 having 的形状是 (算子名, 值)，因此这里把 {"op","value"} 拆开。
        for ft, spec in hf:
            if ft != "metric" or not isinstance(spec, dict):
                continue
            res.having.append((str(spec.get("op")), spec.get("value")))
        res.consumed += h_spans
        # 聚合后筛选若无分组维度，会退化成"整表一行"，与"每组一行"的语义不符。
        # 组内筛选的分组实体由配置声明（entity_dim），显式且可审。
        if res.having:
            ed = ((mk.get("having_filters") or {}).get("metric") or {}).get("entity_dim")
            if ed and ed in cfg.dimensions and ed not in res.dims:
                res.dims.append(ed)

    res.metric = (_first_hit(question, derived_t) or "") or (
        _resolve_metric_by_support(cfg, question, res.dims, [f[0] for f in res.filters])
        if cfg is not None else _first_hit(question, metric_t) or "")
    return res


# ---- 语义层表达能力边界 ----
# 这些句式在**旧规格**（只有等值过滤）下无法表达，若仍按正面版本编译，会返回一个
# **数值完全正确、语义恰好相反**的结果（典型：「不使用英语的国家总人口」返回
# 「使用英语的国家总人口」）。这类错答比拒答危险得多 —— 用户无法从结果本身看出语义被翻转。
#
# 现在这些线索**可以被表达**（否定形态过滤 / 数值比较 / 年份区间，见配置
# `negated_filters`、`numeric_filters`），所以守卫改为**条件触发**：
# 命中的线索若没有被成功表达成过滤条件（即"未被消费"），才拒答。
# 守卫与表达能力必须联动 —— 否则扩展了能力却仍被旧守卫拦住，等于白做。
_UNSUPPORTED_CORE = [
    (r"\b(?:not|no|never|without|except|excluding|other\s+than|besides)\b|n't\b",
     "否定/排除语义（如 not / except）"),
    (r"不含|除了|排除|以外|不是|没有",
     "否定/排除语义"),
    (r"[<>]=?|!=|\bbetween\b.{0,40}\band\b|\b(?:above|below)\b|"
     r"\b(?:longer|shorter|greater|less|fewer|higher|lower|bigger|smaller|more|earlier|later)\s+than\b",
     "数值比较条件（如 > / between / more than / above）"),
    (r"大于|小于|超过|低于|高于|不少于|至少|介于",
     "数值比较条件"),
    (r"\b(?:before|after|since|until)\s+(?:1[89]\d\d|20\d\d)\b",
     "年份区间条件"),
]


def _config_unsupported(question: str, cfg: BusinessConfig) -> str:
    """域配置声明的超纲句式（不依赖是否被消费，直接拒答）。

    典型：取最大/最小者（argmax）—— 它需要返回**实体名称**，而语义层返回度量值。
    支持它意味着支持任意列投影，那会把系统变成通用查询构建器并稀释口径治理这条主线，
    因此这里明确保持"拒答"，而不是"猜一个实体名"。
    """
    for e in ((cfg.matcher_keywords or {}).get("unsupported_patterns") or []):
        if isinstance(e, dict):
            pat, reason = e.get("pattern", ""), e.get("reason", "该句式超出语义层表达能力")
        else:
            pat, reason = str(e), "该句式超出语义层表达能力"
        if pat and re.search(pat, question or "", re.I):
            return reason
    return ""


def _overlaps(span: Tuple[int, int], spans: Sequence[Tuple[int, int]]) -> bool:
    return any(span[0] < e and s < span[1] for s, e in spans)


def _unconsumed_reason(question: str, consumed: Sequence[Tuple[int, int]]) -> str:
    """核心句式守卫：命中的线索**没有被表达成过滤条件**时才拒答。"""
    for pat, reason in _UNSUPPORTED_CORE:
        for m in re.finditer(pat, question or "", re.I):
            if not _overlaps(m.span(), consumed):
                return reason
    return ""


def _metric_keys_in(segment: str, tables) -> List[str]:
    t = segment.lower()
    return [key for key, kws in tables if any(str(kw).lower() in t for kw in kws)]


def _unknown_entity(question: str, cfg: BusinessConfig) -> str:
    """句中出现"取值域之外的专有名词" → 说明过滤取值没解析出来，拒答。

    动机（外部基准暴露，最危险的一类失败）：问句写的是 `the Carribean`
    （Spider 题面里的拼写错误），而库里存的是 `Caribbean`。取值匹配失败 → 过滤器
    一个都不加 → SQL 照跑成功 → 返回**全球**总面积。结果数值本身没错、口径也没被改，
    但范围悄悄放大成了全世界，用户从结果上完全看不出来。实测就是这条让外部基准出现
    了错答（148956306.9 vs 234423.0）。

    规则：把句中的"首字母大写词组"（专有名词候选项）拿去和已知取值域比对，
    全都不认识且不是句首词 → 判定为"有取值没解析出来" → 拒答。
    为什么按**词组**比对："Central Africa" 里 "Central" 单独看不在词表里，
    按词组比对才能避免把正确答案误杀。

    只在该域声明了取值词表时生效（没有词表的域无从判断"认不认识"）。
    """
    fv = (cfg.matcher_keywords or {}).get("filter_values") or {}
    if not fv:
        return ""
    known = set()
    for ftype, values in fv.items():
        for v in values or []:
            known.add(str(v).strip().lower())
    for al in ((cfg.matcher_keywords or {}).get("filter_value_aliases") or {}).values():
        for a in al:
            known.add(str(a).strip().lower())
    if not known:
        return ""
    # 首字母大写的词组（含 "of/the" 之类的连接词，避免把 "Central Africa" 切碎）
    for m in re.finditer(r"[A-Z][\w'\-]*(?:\s+(?:of\s+|the\s+|and\s+)?[A-Z][\w'\-]*)*", question or ""):
        span = m.group(0)
        if m.start() == 0:
            continue                     # 句首大写词（What/How/Give/Find…）不算专有名词
        toks = [t.lower() for t in re.findall(r"[\w'\-]+", span)]
        if not toks:
            continue
        if any(t in known for t in toks):
            continue                     # 词组的任一部分认识 → 视为已识别
        return span
    return ""


def multi_metric_keys(question: str, cfg: BusinessConfig) -> List[str]:
    """识别"一句话里并列了多个指标"的问题，返回命中的不同指标 key。

    动机（外部基准暴露的真实缺陷）：语义层一次只编译**一个**度量。对
    "total population and maximum GNP in Asia" 这类并列问题，若只挑命中词最长
    的那个指标回答，会返回一个**只有一列、数值也不算错**的结果 —— 用户拿去用
    才发现少了一半。宁可拒绝并说明"一次只回答一个口径"，也不能给半个答案。

    实现：按并列连接词切段，段内做关键词命中；≥2 个不同指标落在**不同段**才算
    多指标。落在同一段的不算（同一段里的多个近义词/长词包含短词属正常）。
    """
    tables = _cfg_table(cfg, "derived", _DERIVED_KW) + _cfg_table(cfg, "metrics", _METRIC_KW)
    segments = [s for s in _CONJ_RE.split(question or "") if s.strip()]
    if len(segments) < 2:
        return []
    hit_segments: List[List[str]] = []
    for seg in segments:
        keys = _metric_keys_in(seg, tables)
        if keys:
            hit_segments.append(keys)
    if len(hit_segments) < 2:
        return []
    # 每个段只取"该段命中词最长"的那个指标（段内近义词不重复计数）
    picked: List[str] = []
    for seg in segments:
        keys = _metric_keys_in(seg, tables)
        if not keys:
            continue
        best = max(keys, key=lambda k: max(
            (len(str(kw)) for kk, kws in tables if kk == k for kw in kws
             if str(kw).lower() in seg.lower()), default=0))
        if best not in picked:
            picked.append(best)
    return picked if len(picked) > 1 else []


def _cfg_table(cfg: Optional[BusinessConfig], section: str, builtin):
    """取关键词表：配置里声明了该 section 就整体替换内置词表，否则用内置。

    为什么是"整体替换"而不是"合并"：合并会让新域的英文泛词（count/city）
    与其它域的中文词共存，跨域跑错配置时静默命中错误指标；
    显式替换则"配置即域"，错配会直接表现为拒答（fail-closed）。
    """
    if cfg is not None:
        section_map = (cfg.matcher_keywords or {}).get(section) or {}
        if section_map:
            out = [(k, [str(x) for x in v]) for k, v in section_map.items() if v]
            if out:
                return out
    return builtin


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
    having: List[Tuple[str, object]] = []
    consumed: List[Tuple[int, int]] = []
    metric_key = ""
    method = ""

    # ---- 多指标并列问题：确定性拒绝，绝不"只答一半" ----
    # 放在 LLM 之前：并列多指标是**问题结构**层面的歧义，任何意图识别都消不掉；
    # 先拦下来，既省一次 LLM 调用，也避免 LLM 任选其一后又被确认门放行。
    multi = multi_metric_keys(question, cfg)
    if len(multi) > 1:
        return MatchResult(matched=False, method="multi_metric",
                           reject_reasons=[
                               "本次问题并列了多个指标（" + "、".join(multi) +
                               "）：语义层一次只编译一个度量口径，"
                               "以避免返回「只答一半」的结果。请拆成单指标问题分别提问。"],
                           raw={"metrics": multi})

    # ---- 域配置声明的超纲句式（如取最大/最小者）→ 明确拒答 ----
    unsupported = _config_unsupported(question, cfg)
    if unsupported:
        return MatchResult(matched=False, method="unsupported",
                           reject_reasons=[
                               f"该问题包含{unsupported}。为避免返回语义被翻转/条件被忽略的"
                               "结果，本次拒绝回答。如需支持，请把它登记为正式口径"
                               "（新增指标或过滤类型），而不是让系统猜。"],
                           raw={"unsupported": unsupported})

    # ---- 未知专有名词：取值没解析出来 → 拒答（否则过滤静默丢失、范围被放大）----
    unknown = _unknown_entity(question, cfg)
    if unknown:
        return MatchResult(matched=False, method="unknown_entity",
                           reject_reasons=[
                               f"句中「{unknown}」不在已登记的过滤取值域内：无法确认它应当是"
                               "哪个过滤条件（可能是拼写差异或尚未登记的值）。"
                               "若照原样执行，会返回一个没有过滤条件的更大范围结果"
                               "（数值正确、范围错误），因此本次拒绝回答。"],
                           raw={"unknown_entity": unknown})

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
                    # LLM 也可以给算子（{"type","op","value"}）；算子名仍须过配置白名单，
                    # 不在白名单 → 编译期拒绝。这里原样透传，不在意图层做第二套校验。
                    if f.get("op"):
                        filters.append((ft, {"op": f["op"], "value": f.get("value")}))
                    else:
                        filters.append((ft, f.get("value", "")))
            for h in obj.get("having", []) or []:
                if isinstance(h, dict) and h.get("op"):
                    having.append((str(h["op"]), h.get("value")))
            method = "llm"

    # ---- 关键词兜底 ----
    if not metric_key:
        km = _keyword_match(question, cfg)
        metric_key = km.metric
        dims = km.dims or dims
        filters = km.filters or filters
        having = km.having or having
        consumed = km.consumed
        method = "keyword"
        if km.conflicts:
            detail = "；".join(f"{ft}={'/'.join(vs)}" for ft, vs in km.conflicts.items())
            return MatchResult(matched=False, metric_key=metric_key, method=method,
                               reject_reasons=[
                                   f"同一过滤条件出现多个取值（{detail}）：该取值域的"
                                   "多取值语义未在配置中登记为可表达（或它是「同时满足」语义）。"
                                   "为避免把范围悄悄收窄成其中一个取值，本次拒绝回答；"
                                   "请在配置里把该取值域声明为 `multi: or`（同一行只能有一个取值），"
                                   "或拆成多次提问。"],
                               raw={"filter_conflicts": km.conflicts})

    grain_table = _cfg_table(cfg, "grains", _GRAIN_KW)

    if not metric_key:
        return MatchResult(matched=False, method=method,
                           reject_reasons=["未识别到业务指标(支持: " +
                                           ",".join(cfg.metrics.keys()) + ")"],
                           time_grain=_extract_grain(question, grain_table))

    # ---- 未消费的否定/比较线索 → 拒答（确定性路径；LLM 路径由口径确认门兜底）----
    if method == "keyword":
        leftover = _unconsumed_reason(question, consumed)
        if leftover:
            return MatchResult(matched=False, metric_key=metric_key, method="unsupported",
                               reject_reasons=[
                                   f"该问题包含{leftover}，但它没有被解析成任何可执行的过滤"
                                   "条件（可能缺对应口径或取值未登记）。"
                                   "为避免返回一个条件被忽略的结果，本次拒绝回答。"],
                               raw={"unconsumed": leftover})

    # ---- HAVING 必须作用在本次返回的那个指标上 ----
    # 反例（外部基准实测）："每个政体的总人口，其中平均寿命 > 72" —— 分组后要筛选的是
    # **平均寿命**，返回的却是**总人口**。单指标规格表达不了"跨指标聚合后筛选"，
    # 若按"对本指标做 HAVING"编译，会返回一个筛选条件与被筛对象错位的结果。
    # 判据：句中出现 ≥2 个不同指标关键词、且带组内筛选 → 拒答。
    if having and method == "keyword":
        tables = _cfg_table(cfg, "derived", _DERIVED_KW) + _cfg_table(cfg, "metrics", _METRIC_KW)
        present = _metric_keys_in(question, tables)
        if len(present) > 1:
            return MatchResult(matched=False, metric_key=metric_key, method="unsupported",
                               reject_reasons=[
                                   "本次问题的「组内筛选条件」与「要返回的指标」不是同一个"
                                   f"（句中同时提到 {'、'.join(present)}）：单指标规格无法表达"
                                   "跨指标的聚合后筛选。为避免筛选错位，本次拒绝回答。"],
                               raw={"having_metrics": present})

    grain = _extract_grain(question, grain_table)

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
                           filters=filters, having=having, method=method, time_grain=grain)

    # ---- 支持度校验（关键：不支持直接拦截，不进下游）----
    bad = validate_support(cfg, metric_key, dims, [f[0] for f in filters])
    m = cfg.metrics[metric_key]
    if bad:
        return MatchResult(matched=False, metric_key=metric_key,
                           metric_name=m.name, dims=dims, filters=filters,
                           method=method, reject_reasons=bad, time_grain=grain)
    # HAVING 是对本指标聚合值的后置筛选：只对聚合型指标有意义
    if having and not re.search(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", m.metric_expr or "", re.I):
        return MatchResult(matched=False, metric_key=metric_key, metric_name=m.name,
                           dims=dims, filters=filters, method=method,
                           reject_reasons=[f"{m.name} 不是聚合指标，无法施加组内筛选(HAVING)"],
                           time_grain=grain)
    return MatchResult(matched=True, metric_key=metric_key, metric_name=m.name,
                       dims=dims, filters=filters, having=having, method=method,
                       time_grain=grain)


def _derived_name(cfg: BusinessConfig, metric_key: str) -> str:
    """给派生指标一个人类可读的名字（优先取配置里的 name）。"""
    for d in (getattr(cfg, "derived_metrics", None) or {}).values():
        if d.get("key") == metric_key:
            return d.get("name", metric_key)
    return metric_key
