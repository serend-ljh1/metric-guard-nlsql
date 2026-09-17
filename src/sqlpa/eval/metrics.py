"""
sqlpa.eval.metrics
==================
Text-to-SQL 客观度量：Execution Accuracy (EX) 与 Exact Match (EM)。

设计要点（对照业界公开基准的评测口径）：
  - EX：以"执行结果是否一致"判定对错，忽略 SQL 写法差异。这是最贴合真实业务价值的指标。
  - EX 归一化：行去重、行排序、数值类型放宽（1 == 1.0）、忽略列顺序不敏感。
  - EM：仅用于参考,衡量 SQL 字符串与金标准逐字匹配的比率（口径较严）。
  - 本模块与数据集、LLM、沙箱完全解耦，可独立单元测试。

注意：Spider 官方 EX 用 `evaluation` 脚本跑；BIRD 用 test-suite accuracy（对多组并列金标准做测试套件比对）。
这里实现的是"执行结果比对"的标准语义，作为基础可复现度量；接入官方套件可在 runner 中替换。
"""
from __future__ import annotations

from typing import Any, List, Sequence


def _norm_value(v: Any) -> Any:
    """数值与字符串归一：整数/浮点统一为 float（丢微小误差），null 特殊标记。"""
    if v is None:
        return ("__null__",)
    if isinstance(v, bool):
        return ("__bool__", v)
    if isinstance(v, (int, float)):
        # 处理 1 == 1.0
        return ("__num__", round(float(v), 6))
    return ("__str__", str(v))


def normalize_result(rows: Sequence[Sequence[Any]]) -> set:
    """把执行结果转为可比较的规范形态：逐行归一 + 行去重 + 行排序。

    - 列顺序保留（结果表列顺序不变）。
    - 行顺序无关：对行做排序。
    - 行去重：数据库结果集为集合语义，重复行判为相同。
    """
    normed = []
    for row in rows:
        normed.append(tuple(_norm_value(c) for c in row))
    # 去重 + 排序（tuple 可哈希、可比较）
    return set(normed)


def execution_match(gold_rows: Sequence[Sequence[Any]],
                    pred_rows: Sequence[Sequence[Any]],
                    column_order_insensitive: bool = False) -> bool:
    """判断两次执行结果是否一致（EX 的核心判定）。

    column_order_insensitive=True 时额外对列做置换等价（更宽松，用于某些口径）。
    """
    g = normalize_result(gold_rows)
    p = normalize_result(pred_rows)
    if g == p:
        return True
    if column_order_insensitive and g and p and _same_up_to_column_order(g, p):
        return True
    return False


def _same_up_to_column_order(g: set, p: set) -> bool:
    """把每行内部按列重排后是否可做到全等（仅用于宽松口径）。"""
    def row_bags(s: set) -> set:
        # 每行转为按值排序的元组（丢弃列位置信息），再收集为多集
        return set(tuple(sorted(r)) for r in s)
    return row_bags(g) == row_bags(p)


def gold_match(gold_rows: Sequence[Sequence[Any]],
               gold_valid: bool,
               pred_rows: Sequence[Sequence[Any]],
               column_order_insensitive: bool = False) -> "tuple[bool, str]":
    """金标准安全版判定：返回 (是否命中, 原因)。

    为什么需要它而不是直接用 execution_match：
    `execution_match([], [])` 为 True（空集等于空集）。但当**金标准 SQL 自身执行失败**时
    gold_rows 恰好也是空列表——此时若预测 SQL 同样没跑出结果（rows 也为空），
    就会把一条错误答案判成"正确"，从而**系统性虚高 EX**。

    这是本项目真实存在过的缺陷：修复前 `eval/runner.py`、`pipeline.validate`、
    `langgraph_graph._is_valid` 三处都在金标准执行失败时把"空 vs 空"当成匹配成功。

    Args:
        gold_rows: 金标准执行结果（gold_valid=False 时无意义）
        gold_valid: 金标准**是否执行成功**（由调用方从执行结果传入，不要默认 True）
        pred_rows: 预测执行结果

    Returns:
        (matched, reason)：matched 表示本题是否计为正确；reason 便于排查
        （"ok" / "gold_failed"）。
    """
    if not gold_valid:
        # 金标准都跑不出结果 → 本题无从判定，必须算错并单独统计，绝不能算对
        return False, "gold_failed"
    return execution_match(gold_rows, pred_rows, column_order_insensitive), "ok"


def em_match(gold_sql: str, pred_sql: str) -> bool:
    """精确匹配：SQL 字符串逐字相等（忽略首尾空白，默认大小写敏感）。"""
    return gold_sql.strip() == pred_sql.strip()


def accuracy(equal_flags: Sequence[bool]) -> float:
    if not equal_flags:
        return 0.0
    return sum(1 for f in equal_flags if f) / len(equal_flags)


def mean(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / len(xs)
