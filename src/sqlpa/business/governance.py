"""
sqlpa.business.governance
=========================
治理 Agent（GovernanceAgent）：面向「指标口径治理」的轻量版。

聚焦两件事（不做元数据/血缘/数据质量等重平台能力）：
1. 口径冲突检测：扫描指标中心的指标，找出"含义相近但公式/口径不同"的指标并告警。
   这是配置化指标中心的最小治理闭环——有了指标定义（名称/公式/负责人/版本），
   才能谈冲突检测。纯启发式规则做分母，LLM 做语义增强解释。
2. 指标解释：给定一次取数命中的指标，解释"为什么用这个口径"，返回负责人与版本。
   对齐产品卖点——所有人拿到同一个数，且可追溯口径来源。

边界：不做字段级血缘、不做数据质量规则、不做工单集成。这些属重底座，MVP 不碰。
"""
from __future__ import annotations

import re
from typing import Dict, List

from sqlpa.business.metric_config import BusinessConfig

# 业务近义语料：不同 key 若命中共用别名，说明可能在表达同一件事 → 值得检查口径是否一致
_ALIAS_GROUPS = [
    {"GMV成交总额", "净成交额", "GMV"},
    {"订单数", "订单量", "有效订单数"},
    {"客单价", "平均客单价", "AOV"},
    {"取消率", "退货率", "退款率"},
]


def _normalize_expr(expr: str) -> str:
    """标准化公式：去空白、统一小写、去多余空格，便于对比口径是否同构。"""
    return re.sub(r"\s+", " ", expr.strip().lower())


def _same_family(names: list) -> List[List[str]]:
    """把指标按业务语义近义分族。命中共用别名即视为同一族。"""
    families: List[List[str]] = []
    for group in _ALIAS_GROUPS:
        family = []
        for m in names:
            for alias in group:
                if alias.lower() in str(m).lower():
                    family.append(m)
                    break
        if len(family) >= 2:
            families.append(family)
    return families


def detect_conflicts(cfg: BusinessConfig, llm=None) -> List[Dict]:
    """口径冲突检测：返回语义相近但公式不同的指标对。

    每条冲突：{"keys": [...], "names": [...], "severity": str,
               "reason": str, "exprs": {key: expr}, "versions": {key: version},
               "owners": {key: owner}}
    若传入 llm，会为每条冲突额外生成 "ai_analysis"（中文自然语言解读：为何口径
    可能不一致、风险与统一建议）。规则负责"检测+复核"，LLM 负责"解释"——呼应
    "规则+LLM 协同"。llm 缺失/失败时该字段为空串，不阻塞确定性结果。
    """
    conflicts = []
    seen = set()

    names = list(cfg.metrics.keys())

    def _llm_analyze(a: str, b: str) -> str:
        if llm is None:
            return ""
        ma, mb = cfg.metrics[a], cfg.metrics[b]
        prompt = (
            "你是指标口径治理顾问。两个指标名称语义相近，但公式与口径不同，存在潜在冲突：\n"
            f"- 「{ma.name}」= {ma.metric_expr}（负责人 {ma.owner}，版本 {ma.version}）\n"
            f"- 「{mb.name}」= {mb.metric_expr}（负责人 {mb.owner}，版本 {mb.version}）\n"
            "请用一两句中文说明：两者口径为何可能不一致、带来的业务风险、以及建议如何统一。"
        )
        try:
            return (llm.complete(prompt) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    for family in _same_family([cfg.metrics[k].name if k in cfg.metrics else k for k in names]):
        exprs = {}
        versions = {}
        owners = {}
        fam_keys = []
        for key in names:
            m = cfg.metrics[key]
            if m.name in family:
                fam_keys.append(key)
                exprs[key] = _normalize_expr(m.metric_expr)
                versions[key] = m.version
                owners[key] = m.owner
        for i in range(len(fam_keys)):
            for j in range(i + 1, len(fam_keys)):
                a, b = fam_keys[i], fam_keys[j]
                pair = tuple(sorted((a, b)))
                if pair in seen:
                    continue
                seen.add(pair)
                if exprs[a] != exprs[b]:
                    reason = (f"「{cfg.metrics[a].name}」与「{cfg.metrics[b].name}」语义相近"
                              f"但公式不一致，属典型口径冲突，需确认是否应统一。")
                    conflicts.append({
                        "keys": [a, b], "names": [cfg.metrics[a].name, cfg.metrics[b].name],
                        "severity": "warning", "reason": reason,
                        "exprs": exprs, "versions": versions, "owners": owners,
                        "ai_analysis": _llm_analyze(a, b),
                    })
    return conflicts


def explain_metric(cfg: BusinessConfig, metric_key: str) -> Dict:
    """指标解释：返回某指标的口径来源说明（表达式/负责人/版本/支持维度）。

    产品价值：用户在 UI 上看到指标解释，就知道"这个数为什么这么算、谁定的、第几版"。
    对齐"口径可追溯"卖点——不是只给结果，而是把口径这个关键上下文摊开给用户看。
    """
    m = cfg.metrics.get(metric_key)
    if not m:
        return {"ok": False, "reason": f"未知指标 {metric_key}"}
    return {
        "ok": True,
        "key": m.key, "name": m.name, "desc": m.desc,
        "metric_expr": m.metric_expr, "owner": m.owner, "version": m.version,
        "support_dims": m.support_dims, "support_filters": m.support_filters,
    }