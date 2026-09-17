"""
sqlpa.eval.runner
=================
一键评测入口：对某个基准（Spider / BIRD）批量跑分，输出可复现报告。

产出指标：EX、EM、平均修复轮次、端到端延迟、（可选）Token。
支持消融骨架：传入不同的 llm / 开关即可对比 Baseline1/2 vs Ours。

⚠️ 真实数据集 + 真实 LLM 才有"真实准确率"；mini_benchmark + Mock 仅验证流程链路可复现。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

from sqlpa.eval.metrics import execution_match, gold_match, em_match, accuracy, mean
from sqlpa.eval.console import ensure_utf8_console
from sqlpa.graph.pipeline import run_question
from sqlpa.sandbox.sql_executor import SqlSandbox, ExecConfig
from sqlpa.data.schema_extractor import extract_from_sqlite
from sqlpa.llm.base import LLMProvider


@dataclass
class QResult:
    id: int
    db_id: str
    question: str
    route: str
    final_sql: str
    gold_sql: str
    ex: bool
    em: bool
    gold_failed: bool
    repairs: int
    attempts: int
    latency_ms: float
    terminate_reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalSummary:
    n: int
    ex: float
    em: float
    avg_attempts: float
    avg_repairs: float
    avg_latency_ms: float
    gold_failed: int = 0          # 金标准在本沙箱执行失败的题数（这些题不可判定，已计为错）
    gold_failed_rate: float = 0.0
    by_route: Dict[str, Dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def run_benchmark(benchmark, llm: LLMProvider, max_repair_round: int = 3,
                  save_path: Optional[str] = None, use_critic: bool = False,
                  use_schema_link: bool = False, engine: str = "pipeline") -> EvalSummary:
    sb = SqlSandbox(benchmark.db_path, ExecConfig.from_settings(max_rows=2000))
    schema = extract_from_sqlite(benchmark.db_path, benchmark.db_id).to_dict()
    qres: List[QResult] = []
    for q in benchmark.questions:
        t0 = time.time()
        if engine == "langgraph":
            try:
                from sqlpa.graph.langgraph_graph import run_graph
                r = run_graph(sb, schema, llm, q.question, benchmark.db_id, gold_sql=q.gold_sql,
                              max_repair_round=max_repair_round, use_critic=use_critic,
                              use_schema_link=use_schema_link)
            except ImportError:
                print("  [提示] 未安装 langgraph，回退到 pipeline(确定性编排器)")
                from sqlpa.graph.pipeline import run_question
                r = run_question(q.question, benchmark.db_id, schema, sb, llm,
                                 gold_sql=q.gold_sql, max_repair_round=max_repair_round,
                                 use_critic=use_critic, use_schema_link=use_schema_link)
        else:
            from sqlpa.graph.pipeline import run_question
            r = run_question(q.question, benchmark.db_id, schema, sb, llm,
                             gold_sql=q.gold_sql, max_repair_round=max_repair_round,
                             use_critic=use_critic, use_schema_link=use_schema_link)
        lat = (time.time() - t0) * 1000.0
        gold = sb.execute(q.gold_sql)
        pred = sb.execute(r.final_sql)
        # 金标准执行失败时**不能**用"空 vs 空"判为一致（修复前会系统性虚高 EX）。
        # gold_match 会在 gold_valid=False 时直接返回 False 并标记原因。
        gold_ok = bool(gold.ok)
        matched, _reason = gold_match(gold.rows, gold_ok, pred.rows)
        ex = bool(r.final_valid) and matched
        # 金标准失败的本题不可判定：既已计为错，也要单独计数暴露出来
        gold_failed = (not gold_ok) or bool(getattr(r, "gold_failed", False))
        em = em_match(q.gold_sql, r.final_sql)
        qres.append(QResult(id=q.id, db_id=q.db_id, question=q.question,
                            route=r.route, final_sql=r.final_sql, gold_sql=q.gold_sql,
                            ex=ex, em=em, gold_failed=gold_failed,
                            repairs=r.repairs, attempts=r.attempts,
                            latency_ms=round(lat, 1), terminate_reason=r.terminate_reason))
        # 实时进度（每处理一条打一行，避免"卡死"的错觉）
        ensure_utf8_console()   # Windows GBK 控制台无法编码 ✓/✗，会直接抛错
        flag = "金标准失败" if gold_failed else ("✓" if ex else "✗")
        print(f"  [{len(qres)}/{len(benchmark.questions)}] "
              f"{flag} route={r.route} repairs={r.repairs} "
              f"{r.terminate_reason} | {q.question[:40]}")

    by_route: Dict[str, Dict] = {}
    for route in ("simple", "complex"):
        sub = [r for r in qres if r.route == route]
        if sub:
            by_route[route] = {
                "n": len(sub),
                "ex": accuracy([r.ex for r in sub]),
                "avg_repairs": mean([r.repairs for r in sub]),
                "avg_latency_ms": round(mean([r.latency_ms for r in sub]), 1),
            }

    n_gold_failed = sum(1 for r in qres if r.gold_failed)
    summary = EvalSummary(
        n=len(qres), ex=accuracy([r.ex for r in qres]),
        em=accuracy([r.em for r in qres]),
        avg_attempts=round(mean([r.attempts for r in qres]), 2),
        avg_repairs=round(mean([r.repairs for r in qres]), 2),
        avg_latency_ms=round(mean([r.latency_ms for r in qres]), 1),
        gold_failed=n_gold_failed,
        gold_failed_rate=round(n_gold_failed / len(qres), 4) if qres else 0.0,
        by_route=by_route)

    if save_path:
        out = {"summary": summary.to_dict(),
               "results": [r.to_dict() for r in qres]}
        # 目标目录可能不存在（例如 --out-dir 首次运行）→ 先建目录，
        # 否则会 FileNotFoundError 让整轮评测在"算完之后"失败。
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    return summary


def report(summary: EvalSummary) -> str:
    lines = [f"样本数: {summary.n}",
             f"EX (Execution Accuracy): {summary.ex:.4f}",
             f"EM (Exact Match): {summary.em:.4f}",
             f"金标准执行失败: {summary.gold_failed} ({summary.gold_failed_rate:.2%})"
             " ← 这些题不可判定，已计为错",
             f"平均尝试次数: {summary.avg_attempts}",
             f"平均修复轮次: {summary.avg_repairs}",
             f"平均延迟: {summary.avg_latency_ms} ms"]
    if summary.by_route:
        for route, s in summary.by_route.items():
            lines.append(
                f"  [{route}] n={s['n']} EX={s['ex']:.4f} "
                f"修复={s['avg_repairs']} 延迟={s['avg_latency_ms']}ms")
    return "\n".join(lines)
