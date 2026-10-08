"""
sqlpa.business.confirm
======================
**口径确认门**（HITL 前置）的共用实现：LLM 猜出来的口径必须先由人确认，才允许拉数/归因。

为什么单独成模块：此前这套逻辑只写在分析链（orchestrator）里，取数链（service.answer）
完全没有门 —— 同一个治理问题在两条路径上有两种行为。把"谁算合法口径 / 怎么登记确认 /
怎么按确认结果逐字执行"收敛到一处，两条路径共用，从结构上消除分叉。

设计要点（对齐本项目的红线）：
  - **确定性保底**：确认后执行的 spec 来自**落库的确认记录**，逐字执行（指标/维度/时间粒度
    与确认卡里展示的完全一致），不再用关键词二次解析导致"确认的是一回事、执行的是另一回事"。
  - **LLM 兜底**：LLM 只能在**已登记**的口径里选（`all_metric_keys`），不能即兴造指标。
  - **越界即拒绝**：非法指标 / 无效或已处理的 confirm_id / 过期记录 → 明确拒绝，不放行。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .metric_config import BusinessConfig


def all_metric_keys(cfg: BusinessConfig) -> List[str]:
    """**可编译**的合法口径集合：基础指标 + 派生指标（ratio@a/b、share@a 语法）。

    注意派生指标的合法 key 是 `derived_key()` 生成的语法形式（`ratio@运费/GMV`），
    而不是配置里的 `key: freight_rate` —— 后者编译器不认识，之前确认卡把它列成候选，
    用户一选就会编译失败。
    """
    keys = list(cfg.metrics.keys())
    for d in (getattr(cfg, "derived_metrics", None) or {}).values():
        k = cfg.derived_key(d)
        if k:
            keys.append(k)
    return keys


def candidates(cfg: BusinessConfig) -> List[Dict[str, str]]:
    """确认卡上的候选清单（key 必须是可编译的合法口径）。"""
    out = [{"key": k, "name": m.name} for k, m in cfg.metrics.items()]
    for d in (getattr(cfg, "derived_metrics", None) or {}).values():
        k = cfg.derived_key(d)
        if k:
            out.append({"key": k, "name": d.get("name", k)})
    return out


def is_valid_metric(cfg: BusinessConfig, key: str) -> bool:
    return bool(key) and key in set(all_metric_keys(cfg))


def proposed_spec(res, time_spec: Optional[str], question: str,
                  cfg: Optional[BusinessConfig] = None) -> Dict:
    """把一次 LLM 推断的结果整理成待确认的 spec（确认后按它逐字执行）。

    **确定性补全**：LLM 常常漏掉时间窗/维度（例如只回 `{"metric":"gmv"}`）。旧实现在
    *执行时*偷偷用关键词再解析一遍，于是"确认卡展示的"与"实际执行的"可能不是一回事。
    这里改为在**生成确认卡之前**用确定性的关键词解析补齐，并把补了什么显式标出来
    （`filled_from_question`）——补全仍然是确定性的，而且人看得见。
    """
    spec = {
        "question": question,
        "metric": getattr(res, "metric_key", "") or "",
        "metric_name": getattr(res, "metric_name", "") or "",
        "dims": list(getattr(res, "dims", []) or []),
        "filters": [tuple(f) for f in (getattr(res, "filters", []) or [])],
        "time_grain": getattr(res, "time_grain", None),
        "time_spec": time_spec,
        "method": getattr(res, "method", "") or "llm",
    }
    filled: List[str] = []
    try:
        import sqlpa.business.metric_matcher as _mm
        # 传 cfg：关键词解析要按**当前业务域**的词表来（词表可配置化后，
        # 不传 cfg 会用内置中文表，跨域配置下会解析出错误的时间窗/维度）。
        km = _mm._keyword_match(question, cfg)
        kw_dims, kw_filters = km.dims, km.filters
        if not spec["time_spec"]:
            kw_time = next((v for t, v in kw_filters if t == "time_range"), None)
            if kw_time:
                spec["filters"] = [f for f in spec["filters"] if f[0] != "time_range"]
                spec["filters"].append(("time_range", kw_time))
                spec["time_spec"] = kw_time
                filled.append("time_spec")
        if not spec["dims"] and kw_dims:
            spec["dims"] = [d for d in kw_dims]
            filled.append("dims")
    except Exception as e:  # noqa: BLE001
        # 不能再"静默 pass"：本次改动就踩过——`_keyword_match` 签名变化后这里抛
        # TypeError 被吞掉，确认卡上的时间窗**静默变空**（表现为"确认后只取数、不归因"），
        # 单测才发现。失败必须留在 spec 里可见、可审计。
        spec["fill_error"] = f"{type(e).__name__}: {e}"
    if filled:
        spec["filled_from_question"] = filled
    return spec


def persist(spec: Dict, actor: str = "") -> str:
    """登记待确认口径，返回 confirm_id。"""
    from . import storage
    return storage.insert_confirmation(
        question=spec.get("question", ""), metric=spec["metric"],
        metric_name=spec.get("metric_name", ""), dims=spec.get("dims"),
        filters=spec.get("filters"), time_grain=spec.get("time_grain"),
        method=spec.get("method", "llm"), actor=actor)


def load(cfg: BusinessConfig, confirm_id: str) -> Tuple[Optional[Dict], Optional[str]]:
    """取一条**可执行**的确认记录；返回 (spec, error)。

    拒绝场景（越界即拒绝）：id 不存在 / 已处理过 / 指标已不可编译（配置改过）。
    """
    from . import storage
    if not confirm_id:
        return None, "缺少 confirm_id"
    row = storage.get_confirmation(confirm_id)
    if not row:
        return None, f"确认记录不存在：{confirm_id}"
    if row.get("status") != "pending":
        return None, (f"确认记录 {confirm_id} 状态为 {row.get('status')}，"
                      f"不可重复使用（每次确认只能用一次）")
    if not is_valid_metric(cfg, row.get("metric", "")):
        return None, f"确认的口径「{row.get('metric')}」已不在可编译口径集合中，请重新提问"
    spec = {
        "question": row.get("question", ""),
        "metric": row["metric"],
        "metric_name": row.get("metric_name") or row["metric"],
        "dims": list(row.get("dims") or []),
        "filters": [tuple(f) for f in (row.get("filters") or [])],
        "time_grain": row.get("time_grain") or None,
        "time_spec": next((v for t, v in (row.get("filters") or []) if t == "time_range"),
                          None),
        "method": "confirmed",
        "confirm_id": confirm_id,
    }
    return spec, None


def res_from_spec(spec: Dict):
    """按确认的 spec 造 MatchResult（**逐字**：维度/过滤/粒度都取自确认记录）。"""
    from .metric_matcher import MatchResult
    filters = [tuple(f) for f in (spec.get("filters") or [])]
    dims = [d for d in (spec.get("dims") or [])]
    return MatchResult(matched=True, metric_key=spec["metric"],
                       metric_name=spec.get("metric_name") or spec["metric"],
                       dims=dims, filters=filters, method="confirmed",
                       time_grain=spec.get("time_grain"))


def consume(confirm_id: str, actor: str = "") -> None:
    """把确认记录标记为已用（防止重放）。"""
    from . import storage
    try:
        storage.decide_confirmation(confirm_id, "confirmed", actor=actor)
    except Exception:  # noqa: BLE001
        pass


def override(cfg: BusinessConfig, confirm_id: str, metric: str,
             actor: str = "") -> Tuple[Optional[str], Optional[str]]:
    """**改选**口径：把待确认记录换成另一个合法指标，返回新的 confirm_id。

    语义：原记录置 dismissed（留痕"用户改选了"），新记录继承原 spec 的
    维度/过滤/时间粒度，只替换指标 —— 因为改选不等于可以顺手改口径范围。
    """
    from . import storage
    if not is_valid_metric(cfg, metric):
        return None, (f"指标「{metric}」不在可编译口径集合中（基础指标或 ratio@/share@ 派生）")
    row = storage.get_confirmation(confirm_id)
    if not row:
        return None, f"确认记录不存在：{confirm_id}"
    if row.get("status") != "pending":
        return None, f"确认记录 {confirm_id} 已处理（{row.get('status')}），不能再改选"
    m = cfg.metrics.get(metric)
    name = m.name if m else metric
    spec = {
        "question": row.get("question", ""),
        "metric": metric, "metric_name": name,
        "dims": list(row.get("dims") or []),
        "filters": [tuple(f) for f in (row.get("filters") or [])],
        "time_grain": row.get("time_grain") or None,
        "time_spec": next((v for t, v in (row.get("filters") or []) if t == "time_range"),
                          None),
        "method": "confirmed",
    }
    storage.decide_confirmation(confirm_id, "dismissed", actor=actor or "override")
    return persist(spec, actor=actor), None
