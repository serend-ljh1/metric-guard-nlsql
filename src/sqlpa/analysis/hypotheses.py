"""L2 假设验证环 —— LLM 提假设，确定性判官裁决。

把归因从"单维 top-1 一句话"升级为**证据排序的候选**：LLM 提出 2-3 条"这次波动最可能出在哪"
的**假设**，再由一个**确定性判官**逐条核验（复用既有 `attribution._additivity_check` 与规则
校验器），把命中假设标 `verified`、把不成立的假设连同**被否原因**一并记录。

分工（与项目铁律一致）：
  - **LLM 只负责"提假设"** —— 提出可能的原因方向，不写 SQL、不碰算术；
  - **"成立与否"由确定性算术说了算** —— 判官绝不调 LLM、绝不生成 SQL，防止幻觉混进结论；
  - 本环**只在接 LLM 时启用**：无 LLM → `run_hypothesis_loop` 不参与主链，行为逐字回落旧线性链。
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from sqlpa.analysis import operators
from sqlpa.business import attribution

_HYPO_KINDS = ("dim_value", "factor")


def deterministic_candidates(att: Dict) -> List[Dict]:
    """从 analyze 结果构建**确定性候选**（按 |delta| 降序），作为 LLM 假设的兜底/对照。"""
    out: List[Dict] = []
    for c in (att.get("top_contributors") or [])[:3]:
        out.append({"kind": "dim_value", "dim": c.get("dim"), "value": c.get("key"),
                    "claim": c.get("desc", ""), "delta": c.get("delta"),
                    "pct_of_change": c.get("pct_of_change"),
                    "pct_reliable": c.get("pct_reliable", True), "source": "analyze"})
    return out


def _dim_rows(att: Dict) -> Dict[str, Dict]:
    return {r.get("dim"): r for r in (att.get("dims") or []) if isinstance(r, dict)}


def _claim(h: Dict) -> str:
    return h.get("claim") or (f"{h.get('dim')}·{h.get('value')}" if h.get("dim")
                              else str(h.get("factor") or h))


def verify_hypothesis(h: Dict, att: Dict, factor_split: Optional[Dict] = None,
                      ctx=None) -> Dict:
    """确定性判官：判定一条假设是否成立，返回 `{hypothesis, verified, reason, evidence}`。"""
    kind = h.get("kind")

    if kind == "dim_value":
        dim, value = h.get("dim"), h.get("value")
        row = _dim_rows(att).get(dim)
        if row is None:
            return {"hypothesis": h, "verified": False,
                    "reason": f"维度「{dim}」不在该指标的候选拆解范围内"}
        delta_map = row.get("delta") or {}
        if value not in delta_map:
            return {"hypothesis": h, "verified": False,
                    "reason": f"取值「{value}」在维度「{dim}」下不存在或无变化"}
        chk = attribution._additivity_check(att.get("change") or 0.0, delta_map)
        ev = {"dim": dim, "value": value, "delta": delta_map.get(value)}
        if not chk.get("additive"):
            return {"hypothesis": h, "verified": False, "evidence": ev,
                    "reason": (f"维度「{dim}」分段之和不等于总变化（未覆盖 {chk.get('uncovered')}），"
                               f"该取值占比不可靠")}
        return {"hypothesis": h, "verified": True, "evidence": ev,
                "reason": "维度取值存在，且分段之和对得上总变化（占比可加）"}

    if kind == "factor":
        fs = factor_split
        if fs is None and ctx is not None:
            try:
                fs = operators.run_op("factorize", ctx, {})
            except Exception:  # noqa: BLE001
                fs = None
        if not fs or not fs.get("ok"):
            return {"hypothesis": h, "verified": False,
                    "reason": "当前指标未定义可核验的乘法因子，无法核验该假设"}
        label = h.get("factor")
        hit = next((f for f in (fs.get("factors") or []) if f.get("label") == label), None)
        if hit is None:
            return {"hypothesis": h, "verified": False,
                    "reason": f"因子「{label}」不在 {fs.get('formula')} 的分解里"}
        if abs(hit.get("share") or 0.0) < 1e-9:
            return {"hypothesis": h, "verified": False,
                    "reason": f"因子「{label}」对总变化的贡献≈0"}
        return {"hypothesis": h, "verified": True,
                "evidence": {"factor": label, "share": hit.get("share"),
                             "change": hit.get("change")},
                "reason": "该因子在乘法分解中确实有贡献"}

    return {"hypothesis": h, "verified": False, "reason": f"未知假设类型「{kind}」"}


def _dim_choices(att: Dict) -> str:
    lines = []
    for r in (att.get("dims") or [])[:4]:
        vals = list((r.get("delta") or {}).keys())[:6]
        lines.append(f"- {r.get('dim')}: {vals}")
    return "\n".join(lines) or "（无可用维度拆解）"


def propose_hypotheses(att: Dict, llm, n: int = 3) -> List[Dict]:
    """提假设：先给确定性候选（按证据排序）；接 LLM 时再让模型补充 2-3 条（去重合并）。"""
    base = deterministic_candidates(att)
    if llm is None:
        return base
    cont_lines = "\n".join(c.get("desc", "") for c in (att.get("top_contributors") or [])[:4])
    prompt = (
        "你是归因分析 Agent。下面是指标波动按维度拆解后的**主因候选**，"
        "请提出 2-3 条'这次波动最可能出在哪'的**假设**（每条一个方向，不要重复）：\n"
        "- dim_value：某个维度取值是主因，给 dim 与 value\n"
        "- factor：乘法因子出了问题，factor 取「订单量」或「客单价」\n"
        f"已有主因候选：\n{cont_lines or '（无显著维度贡献）'}\n"
        f"可选维度与取值（从这里挑）：\n{_dim_choices(att)}\n"
        "只输出 JSON 数组，例如："
        "[{\"kind\":\"dim_value\",\"dim\":\"state\",\"value\":\"SP\",\"claim\":\"主因是圣保罗\"}]"
    )
    try:
        text = llm.complete(prompt).strip().strip("`").removeprefix("json").removeprefix("JSON")
        arr = json.loads(text)
    except Exception:  # noqa: BLE001 —— 提假设失败即用确定性候选兜底
        return base
    if not isinstance(arr, list):
        return base
    extra: List[Dict] = []
    for item in arr[:n]:
        if isinstance(item, dict) and item.get("kind") in _HYPO_KINDS:
            it = dict(item)
            it["source"] = "llm"
            extra.append(it)
    # LLM 假设在前，确定性候选补足；按 (kind,dim,value,factor) 去重
    seen: set = set()
    merged: List[Dict] = []
    for h in extra + base:
        key = json.dumps({k: h.get(k) for k in ("kind", "dim", "value", "factor")},
                         ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        merged.append(h)
    return merged[:6]


def run_hypothesis_loop(ctx, att: Dict, llm, factor_split: Optional[Dict] = None) -> Dict:
    """假设验证环：提假设 → 确定性判官逐条裁决 → 返回证据排序的成立/被否清单。

    仅接 LLM 时由 orchestrator 调用；判官零 LLM、零 token、可复核。
    """
    hyps = propose_hypotheses(att, llm)
    verified: List[Dict] = []
    rejected: List[Dict] = []
    for h in hyps:
        res = verify_hypothesis(h, att, factor_split=factor_split, ctx=ctx)
        (verified if res["verified"] else rejected).append(res)
    # 证据排序：按 |delta|（dim_value）或 |share|（factor）降序
    def _rank(r: Dict) -> float:
        ev = r.get("evidence") or {}
        v = ev.get("delta")
        if v is None:
            v = ev.get("share")
        return abs(v) if isinstance(v, (int, float)) else 0.0

    verified.sort(key=_rank, reverse=True)
    return {"verified": verified, "rejected": rejected,
            "n": len(hyps), "n_verified": len(verified), "n_rejected": len(rejected)}
