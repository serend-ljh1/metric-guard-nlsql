"""
sqlpa.business.followup
=======================
多轮追问的上下文管理：把"省略式追问"改写成语义完整的独立问题。

设计（对齐"推理归 LLM、计算归代码"）：
  - 有 LLM 时：由 LLM 结合对话历史做指代消解/省略补全（question rewriting）。
  - 无 LLM 时：确定性启发式兜底——短问题或追问标记词开头 → 把上一问作为上下文拼接。
  - 改写结果会透传给下游（MetricMatcher/引擎），并记入审计，保证可追溯。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# 追问启发式标记（以这些词开头的问题大概率是省略式追问）
_FOLLOWUP_HEADS = ("再", "那", "换", "还有", "只看", "只", "改成", "去掉", "加上",
                   "细分", "分别", "对比", "如果", "仅", "呢", "吗")


def is_likely_followup(question: str, history: List[Dict]) -> bool:
    """无 LLM 时的确定性判断：是否为省略式追问。"""
    if not history:
        return False
    q = (question or "").strip()
    if not q:
        return False
    if len(q) <= 10:                      # 很短的问题通常依赖上文
        return True
    return any(q.startswith(h) for h in _FOLLOWUP_HEADS)


def rewrite(question: str, history: List[Dict], llm=None) -> Tuple[str, bool]:
    """把最新问题改写为语义完整的独立问题。

    history: [{"question": str, "metric": str, "sql": str}, ...]（旧→新）
    返回 (改写后的问题, 是否使用了上下文)。
    """
    if not history:
        return question, False
    last = history[-1]

    # ---- 无 LLM：确定性兜底 ----
    if llm is None:
        if is_likely_followup(question, history):
            ctx = f"（这是在上一问「{last.get('question', '')}」基础上的追问，请合并上下文理解）"
            return question + ctx, True
        return question, False

    # ---- 有 LLM：指代消解/省略补全 ----
    hist_lines = []
    for i, h in enumerate(history[-3:], 1):   # 最多带最近 3 轮，控 token
        line = f"{i}. 用户：{h.get('question', '')}"
        if h.get("metric"):
            line += f"（识别指标：{h['metric']}）"
        hist_lines.append(line)
    prompt = (
        "你是取数对话的上下文管理器。根据对话历史，把用户最新的问题改写为一个"
        "语义完整、可独立理解的问题（补全省略的指标/维度/过滤条件，消解指代）。\n"
        "规则：\n"
        "1. 如果最新问题本身已经完整，原样返回；\n"
        "2. 只输出改写后的问题文本，不要任何解释、引号或前缀；\n"
        "3. 保持用户原意，不要增加用户没表达的条件。\n\n"
        "【对话历史】\n" + "\n".join(hist_lines) +
        f"\n\n【最新问题】{question}\n\n【改写后的问题】"
    )
    try:
        out = (llm.complete(prompt) or "").strip().strip('"').strip()
        if out:
            return out, True
    except Exception:  # noqa: BLE001  LLM 失败不阻塞主流程
        pass
    # LLM 失败 → 启发式兜底
    if is_likely_followup(question, history):
        return question + f"（这是在上一问「{last.get('question', '')}」基础上的追问）", True
    return question, False
