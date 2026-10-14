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

import json
import re
from typing import Dict, List, Optional

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
    return re.sub(r"\s+", " ", (expr or "").strip().lower())


def _norm_name(name: str) -> str:
    """标准化指标名（去所有空白 + 小写），用于判断两个指标是否"同名"。"""
    return re.sub(r"\s+", "", str(name)).lower()


def _canon_expr(expr: str) -> str:
    """口径等价判断用的**严格**归一：去掉所有空白 + 小写。

    `_normalize_expr` 只把连续空白折叠成一个空格（用于近似对比），但 SQL 里空白本身
    不改变语义 —— `sum( oi.price )` 与 `sum(oi.price)` 是同一个口径。判断"是否同式"
    必须用这一层，否则一个多余空格就会让重复登记逃过检测。
    """
    return re.sub(r"\s+", "", (expr or "").strip().lower())


def _canon_metric(m) -> str:
    """指标**口径指纹**：把"决定这个数怎么算"的字段一起归一（表达式/来源/JOIN/核心过滤）。

    为什么不能只比 `metric_expr`：「订单数」「已支付订单数」「取消订单数」的表达式都是
    `COUNT(DISTINCT o.order_id)`，靠 `where_core` 的订单状态区分口径 —— 只比表达式会把
    它们误判成同一口径（实测就会报出 4 个"重复"，全是假阳性）。
    """
    return "|".join(_canon_expr(getattr(m, f, "")) for f in
                    ("metric_expr", "from_clause", "join_clause", "where_core"))


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


def _auto_history() -> Dict[str, Dict]:
    """从口径版本存储里取每个指标的**最新一条**记录（读不到就返回空）。

    只用于第 3 类冲突（版本漂移）。存储不可用/为空时静默跳过 —— 治理检测不能因为
    一个附属存储而整体失败（与"取数不被治理层阻塞"同一原则）。
    """
    try:
        from sqlpa.business import storage
        rows = storage.list_metric_versions(limit=500)   # 已按 id DESC → 首条即最新
    except Exception:  # noqa: BLE001
        return {}
    latest: Dict[str, Dict] = {}
    for r in rows:
        k = r.get("metric_key")
        if k and k not in latest:
            latest[k] = r
    return latest


def _entry(cfg: BusinessConfig, ctype: str, severity: str, keys: List[str],
           reason: str, ai_analysis: str = "") -> Dict:
    """统一构造一条冲突记录（保持既有字段形状，供前端/演示/测试消费）。"""
    return {
        "type": ctype,
        "keys": list(keys),
        "names": [cfg.metrics[k].name for k in keys if k in cfg.metrics],
        "severity": severity,
        "reason": reason,
        "exprs": {k: _normalize_expr(cfg.metrics[k].metric_expr) for k in keys},
        "versions": {k: cfg.metrics[k].version for k in keys},
        "owners": {k: cfg.metrics[k].owner for k in keys},
        "ai_analysis": ai_analysis,
    }


def detect_conflicts(cfg: BusinessConfig, llm=None,
                     history: Optional[Dict[str, Dict]] = None) -> List[Dict]:
    """口径冲突检测：返回**四类**口径冲突（前 3 类确定性，第 4 类启发式）。

    每条冲突：{"type": str, "keys": [...], "names": [...], "severity": str,
               "reason": str, "exprs": {key: expr}, "versions": {key: version},
               "owners": {key: owner}, "ai_analysis": str}

    四类（type）：
      1. **同名不同式**（severity=error）—— key 不同、**展示名相同**、公式不同。
         典型场景：两个团队各建了一条「GMV成交总额」，一个含运费一个不含 → 同名不同数。
      2. **同式不同名**（severity=info）—— 公式（归一化后）**完全一致**但名称不同。
         口径冗余：同一件事被登记了两遍，维护时会改一漏一。
      3. **版本漂移**（severity=warning）—— 当前配置的公式与 `metric_versions`
         里最后登记的口径不一致。意味着这条口径被**绕过指标中心**直接改了配置，
         版本历史里查不到这次变更（`history` 不传时自动读 storage，读不到则跳过本类）。
      4. **近义不同式**（severity=warning）—— 别名族内名称语义相近但公式不同（启发式）。

    若传入 llm，会为每条冲突生成 "ai_analysis"（中文解读：为何口径可能不一致、
    风险与统一建议）。规则负责"检测+复核"，LLM 只负责"解释"；llm 缺失/失败时该字段
    为空串，不阻塞确定性结果。
    """
    conflicts: List[Dict] = []
    names = list(cfg.metrics.keys())
    if history is None:
        history = _auto_history()

    def _llm_analyze(a: str, b: str) -> str:
        if llm is None:
            return ""
        ma, mb = cfg.metrics[a], cfg.metrics[b]
        prompt = (
            "你是指标口径治理顾问。两个指标口径存在潜在冲突：\n"
            f"- 「{ma.name}」= {ma.metric_expr}（负责人 {ma.owner}，版本 {ma.version}）\n"
            f"- 「{mb.name}」= {mb.metric_expr}（负责人 {mb.owner}，版本 {mb.version}）\n"
            "请用一两句中文说明：两者口径为何可能不一致、带来的业务风险、以及建议如何统一。"
        )
        try:
            return (llm.complete(prompt) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    # ---- 1. 同名不同式：key 不同、名字相同、口径不同 ----
    by_name: Dict[str, List[str]] = {}
    for k in names:
        by_name.setdefault(_norm_name(cfg.metrics[k].name), []).append(k)
    for grp in by_name.values():
        if len(grp) < 2:
            continue
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                a, b = grp[i], grp[j]
                if _canon_metric(cfg.metrics[a]) == _canon_metric(cfg.metrics[b]):
                    continue   # 同名同式只是重复登记，不算口径冲突
                conflicts.append(_entry(
                    cfg, "同名不同式", "error", [a, b],
                    f"「{cfg.metrics[a].name}」与「{cfg.metrics[b].name}」**同名但口径不同**"
                    f"（{a}={cfg.metrics[a].metric_expr} / {b}={cfg.metrics[b].metric_expr}）——"
                    f"同一份报表里会出现两个「同名不同数」的口径，必须定一个为准。",
                    _llm_analyze(a, b)))

    # ---- 2. 同式不同名：口径指纹完全一致、名称不同 ----
    by_fp: Dict[str, List[str]] = {}
    for k in names:
        fp = _canon_metric(cfg.metrics[k])
        if fp.strip("|"):
            by_fp.setdefault(fp, []).append(k)
    for fp, grp in by_fp.items():
        if len(grp) < 2:
            continue
        if len({_norm_name(cfg.metrics[k].name) for k in grp}) < 2:
            continue   # 名字全都一样 → 属"同名同式"，不重复报
        # 一次给出整组：它们是同一个口径的多份登记，治理时应当一起处理
        conflicts.append(_entry(
            cfg, "同式不同名", "info", grp,
            "以下指标的**口径完全一致（公式 / 来源 / JOIN / 核心过滤都不变）但名称不同**：" +
            "、".join(f"{k}「{cfg.metrics[k].name}」" for k in grp) +
            f"（公式 {cfg.metrics[grp[0]].metric_expr}）——"
            f"同一件事被登记了多遍，改口径时容易改一漏一，建议合并或明确分工。"))

    # ---- 3. 版本漂移：配置里的公式 ≠ 版本历史里最后登记的口径 ----
    for k in names:
        rec = history.get(k)
        if not rec:
            continue
        after = rec.get("after_json")
        if isinstance(after, str):
            try:
                after = json.loads(after)
            except Exception:  # noqa: BLE001
                after = None
        if not isinstance(after, dict):
            continue   # delete / rollback 等无 after 定义的记录，不判漂移
        recorded = _normalize_expr(after.get("metric_expr") or "")
        current = _normalize_expr(cfg.metrics[k].metric_expr)
        if not recorded or not current or recorded == current:
            continue
        conflicts.append(_entry(
            cfg, "版本漂移", "warning", [k],
            f"「{cfg.metrics[k].name}」当前配置公式为 `{current}`，但口径版本历史里最后登记的"
            f"是 `{recorded}`（version_seq={rec.get('version_seq')}，action={rec.get('action')}，"
            f"actor={rec.get('actor') or '未记录'}）——这条口径被**绕过指标中心**直接改了配置，"
            f"版本历史里查不到这次变更。"))

    # ---- 4. 近义不同式（启发式，原有逻辑） ----
    seen = set()
    family_names = [cfg.metrics[k].name if k in cfg.metrics else k for k in names]
    for family in _same_family(family_names):
        fam_keys = [k for k in names if cfg.metrics[k].name in family]
        for i in range(len(fam_keys)):
            for j in range(i + 1, len(fam_keys)):
                a, b = fam_keys[i], fam_keys[j]
                pair = tuple(sorted((a, b)))
                if pair in seen:
                    continue
                seen.add(pair)
                if _normalize_expr(cfg.metrics[a].metric_expr) != \
                        _normalize_expr(cfg.metrics[b].metric_expr):
                    conflicts.append(_entry(
                        cfg, "近义不同式", "warning", [a, b],
                        f"「{cfg.metrics[a].name}」与「{cfg.metrics[b].name}」语义相近"
                        f"但公式不一致，属典型口径冲突，需确认是否应统一。",
                        _llm_analyze(a, b)))
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