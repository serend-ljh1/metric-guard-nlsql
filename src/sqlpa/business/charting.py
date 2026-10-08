"""
sqlpa.business.charting
=======================
结果自动可视化：根据「列名 + 数据类型 + 行数」确定性推荐图表类型并生成 Altair 图。

规则（纯代码启发式，与 LLM 无关）：
  - 1 个维度 + 1 个数值列        → 柱状图（维度是日期则折线图）
  - 2 个维度 + 1 个数值列        → 分组柱状图（颜色区分第二维度）
  - 仅 1 行 1 个数值列           → 大数字指标卡（返回 kind="metric"）
  - 其余                          → 不推荐图表（kind="table"）
"""
from __future__ import annotations

import datetime
import re
from typing import Dict, List, Optional, Sequence

_DATE_HINT = re.compile(r"(date|time|dt|day|month|year|timestamp|_at$)", re.I)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_date_like(col: str, values: Sequence) -> bool:
    if _DATE_HINT.search(col or ""):
        return True
    for v in values[:5]:
        if isinstance(v, (datetime.date, datetime.datetime)):
            return True
        if isinstance(v, str) and re.fullmatch(r"\d{4}[-/]\d{1,2}([-/]\d{1,2})?", v.strip()):
            return True
    return False


def recommend(columns: List[str], rows: List[Sequence]) -> Dict:
    """返回图表推荐规格：{kind, x, y, color?, title_hint}。

    kind ∈ {"bar","line","grouped_bar","metric","table"}
    """
    if not columns or not rows:
        return {"kind": "table"}
    num_cols = [c for c in columns
                if any(_is_number(r[i]) for r in rows[:10]
                       for i in [columns.index(c)] if i < len(r))]
    non_num = [c for c in columns if c not in num_cols]

    # 大数字指标卡：单行单数值
    if len(rows) == 1 and len(num_cols) == 1 and len(columns) <= 2:
        return {"kind": "metric", "y": num_cols[0],
                "label": next((c for c in columns if c != num_cols[0]), num_cols[0])}

    if len(num_cols) == 1 and len(non_num) >= 1:
        y = num_cols[0]
        x = non_num[0]
        xvals = [r[columns.index(x)] for r in rows]
        if _is_date_like(x, xvals):
            return {"kind": "line", "x": x, "y": y}
        if len(non_num) >= 2 and len(rows) <= 200:
            return {"kind": "grouped_bar", "x": x, "y": y, "color": non_num[1]}
        return {"kind": "bar", "x": x, "y": y}

    return {"kind": "table"}
