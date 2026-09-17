"""
sqlpa.agents.router
===================
难度路由（Router Agent）：判断一条自然语言问题应走"轻量单 Agent"还是"多 Agent 自愈链路"。

⚠️ 诚实说明（重要）：
  - 纯关键词路由**无法可靠识别隐式 JOIN**（如"订单金额超过3000的客户"需要跨表，但字面无 join 词）。
  - 因此本模块做"schema 感知"：将问题里的术语解析到实际表实体，统计**涉及的表数**，
    跨表（≥2 表）或命中聚合/排序/去重/子查询等逻辑信号 → 判为 complex。
  - 仍为确定性启发式，属于**快速预筛**；边界情形在生产中由一层**轻量 LLM 二次判断**兜底。
"""

from __future__ import annotations

import re
from typing import Dict, List

# 中文 -> 英文表/列同义词映射
_SYNONYMS: Dict[str, str] = {
    "客户": "customers", "顾客": "customers", "用户": "customers",
    "订单": "orders", "产品": "products", "商品": "products", "物料": "products",
    "采购": "orders", "部门": "departments", "员工": "employees", "职员": "employees",
    "国家": "country", "类别": "category", "分类": "category",
}

# 逻辑信号（聚合/分组/排序/去重/子查询/窗口）
_LOGIC = re.compile(
    r"\b(join|group\s+by|having|order\s+by|distinct|min|max|sum|avg|count"
    r"|over\s*\(|window|union|case\s+when|limit)\b|"
    r"(每个|各|分别|按|平均|占比|排名|累计|总计|比例|总额|最多|最高|最少|最低"
    r"|排序|嵌套|子查询|同时|对比)", re.I)


def _build_index(schema: Dict) -> Dict[str, set]:
    """把 schema（{tables:[{name,columns:[{name,...}]}]}）做成 表名 -> 术语集合 的索引。"""
    index: Dict[str, set] = {}
    tables = schema.get("tables", []) if isinstance(schema, dict) else []
    for t in tables:
        tname = t.get("name", "")
        terms = {tname.lower()}
        for c in t.get("columns", []):
            cname = c.get("name", "")
            terms.add(cname.lower())
        index[tname] = terms
    return index


def _referenced_tables(question: str, schema: Dict) -> List[str]:
    """返回问题里被引用的表名（通过 表名/列名/同义词 匹配）。"""
    if not schema:
        return []
    index = _build_index(schema)
    q = (question or "").lower()
    hits = []
    for tname, terms in index.items():
        if any(tok in q for tok in terms if tok):
            hits.append(tname)
    # 同义词兜底：把中文同义词映射到表名
    for cn, en in _SYNONYMS.items():
        if cn in (question or "") and en in index and en not in hits:
            hits.append(en)
    return hits


def route_decision(question: str, schema: Dict | None = None) -> Dict:
    """返回路由决策与可观测的判据（纯确定性，不调用 LLM）。"""
    q = question or ""
    refs = _referenced_tables(q, schema)
    n_tables = len(refs)
    logic_hit = bool(_LOGIC.search(q))
    # 判据：跨表(≥2) 或 命中逻辑信号 → complex
    decision = "complex" if (n_tables >= 2 or logic_hit) else "simple"
    return {
        "decision": decision,
        "n_tables": n_tables,
        "referenced_tables": refs,
        "logic_hit": logic_hit,
    }


def route_with_llm_fallback(question: str, schema: Dict | None = None, llm=None,
                            schema_text: str = "",
                            enabled: Optional[bool] = None) -> Dict:
    """确定性路由 + **可选的** LLM 二次判断兜底。

    动机：纯词面/跨表启发式对"隐式 JOIN"这类题会误判为 simple
    （例如"每个州有多少订单"没写出表名，但实际需要 join）。
    enabled=True（或 enabled=None 且 settings.yaml `pipeline.route_llm_fallback: true`）
    且提供了 llm 时，**仅对判为 simple 的题**问一次 LLM：它若认为复杂则升为 complex。

    设计取舍：
      - 只做"single → complex"的**单向升级**，且只在判为 simple 时调用，
        避免在简单题上白白多花一次 LLM 调用；
      - 任何异常都静默回退到确定性结论——兜底失败不能让主流程失败；
      - 修复前 `difficulty_judge` 在 3 个 LLM 实现里都有定义却**从未被调用**，
        `route_llm_fallback` 配置也从未被读取（文档声称有兜底，实际没有）。
    """
    base = route_decision(question, schema)
    base["llm_fallback"] = False
    if base["decision"] != "simple" or llm is None:
        return base
    if enabled is None:
        try:
            from sqlpa.config import get as cfg_get
            enabled = bool(cfg_get("pipeline.route_llm_fallback", False))
        except Exception:  # noqa: BLE001
            enabled = False
    if not enabled:
        return base
    try:
        verdict = str(llm.difficulty_judge(question, schema_text or "") or "").strip().lower()
    except Exception:  # noqa: BLE001 —— 兜底失败不影响主流程
        return base
    if verdict == "complex":
        base["decision"] = "complex"
        base["llm_fallback"] = True
        base["logic_hit"] = True   # 记为命中了复杂信号，便于 trace 解释
    return base


def classify(question: str, schema: Dict | None = None) -> str:
    return route_decision(question, schema)["decision"]
