"""
sqlpa.agents.schema_linker
==========================
Schema-Linker Agent：根据问题**精简 schema 上下文**给下游 Writer。

设计（列级筛选，安全 + 真省 token）：
  - 保留**所有表**（JOIN 安全，不丢桥接表）；
  - 每张表只保留"相关列"：① 问题命中/同义词命中 的列名；② **外键列 + 被引用列**（join 必需）；
     ③ 主键列。
  - 其余无关列被过滤掉，减少上下文 token、降低冗余列干扰。
  - 词表/同义词匹配，可替换为 embedding 语义匹配。

纯函数、无 LLM、可单测。`link(question, schema)` -> {schema_text, kept_cols, orig_cols, reduced}
"""
from __future__ import annotations

import re
from typing import Dict, List

# 中文 -> 英文列/表同义词
_SYNONYMS = {
    "品类": "category", "类别": "category", "州": "state", "省份": "state",
    "订单": "order", "明细": "item", "商品": "product", "退款": "refund",
    "客户": "customer", "库存": "inventory",
}


def _relevant(question: str, table: Dict) -> Dict:
    """保留表(不变)，只裁剪列：问题命中/同义词命中 + 外键列 + 主键列。"""
    tname = table.get("name", "")
    cols = table.get("columns", [])
    q = (question or "").lower()
    keep = []
    for c in cols:
        cname = c.get("name", "")
        cname_l = cname.lower()
        hit = (cname_l in q
               or any(tok in q for tok in re.findall(r"[a-z_]+", cname_l) if len(tok) >= 3)
               or any(cn in (question or "") for cn, en in _SYNONYMS.items()
                      if (en in cname_l or en == tname.lower())))
        if hit or c.get("references_table") or c.get("primary_key"):
            keep.append(c)
        else:
            # 无 FK/主键且未命中 —— 若非该表唯一列,则丢弃; 否则保留(避免空表)
            pass
    if not keep:
        keep = cols[:1]  # 兜底: 至少保留一列,避免空表
    return {"name": tname, "columns": keep}


def link(question: str, schema: Dict) -> Dict:
    tables = schema.get("tables", []) if isinstance(schema, dict) else []
    new_tables = [_relevant(question, t) for t in tables]
    orig_cols = sum(len(t.get("columns", [])) for t in tables)
    kept_cols = sum(len(t.get("columns", [])) for t in new_tables)

    from sqlpa.llm.base import build_schema_text
    sub = {"tables": new_tables}
    return {"schema_text": build_schema_text(sub),
            "kept_tables": [t.get("name") for t in new_tables],
            "kept_cols": kept_cols, "orig_cols": orig_cols,
            "reduced": kept_cols < orig_cols}
