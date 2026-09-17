"""
run_eval.py
===========
**主入口**：在 PyCharm 里运行本文件，填好 LLM Key 即可对公开基准做真实评测（默认 LangGraph 引擎）。

用法：
  # 先复制 .env.example 为 .env 并填入 LLM_API_KEY（或用命令行 --key）
  py run_eval.py --dataset spider --db-root data/spider --split dev
  py run_eval.py --dataset bird   --db-root data/bird   --split dev
  py run_eval.py --dataset spider --split dev --sample 100 --ablation
  py run_eval.py --dataset spider --split dev --sample 50  --critic-ablation
  py run_eval.py --dataset spider --split dev --sample 50  --baseline --ref 0.87

说明：
  - 真实准确率需接入真实 LLM（.env 配 key）；引擎默认用 LangGraph StateGraph（`--engine langgraph`）。
  - Key 通过 .env 或 `--key` 提供；端点/模型通过环境变量或 `--base/--model`。
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "tools"))

from sqlpa.data.loader import load_spider_dev  # noqa: E402
from sqlpa.eval.console import ensure_utf8_console  # noqa: E402
from sqlpa.eval.runner import run_benchmark, report, EvalSummary  # noqa: E402
from sqlpa.llm.mock_llm import MockLLM  # noqa: E402
from sqlpa.llm.openai_compat import OpenAICompatLLM  # noqa: E402

# Windows 控制台默认 GBK，无法编码 "¥" 等字符；统一改 UTF-8，
# 否则汇总打印会在所有结果都已算完之后抛 UnicodeEncodeError（退出码非 0）。
ensure_utf8_console()


def _has_key() -> bool:
    """是否已在环境/.env 中配了 LLM API Key（真实模式判定）。"""
    return bool(os.environ.get("LLM_API_KEY")
                or os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("OPENAI_API_KEY"))


def build_llm(args, answer_key=None):
    if args.mock:
        return MockLLM(answer_key=answer_key)
    return OpenAICompatLLM(api_key=args.key, base_url=args.base,
                           model=args.model, temperature=args.temperature)


def agg_summaries(ss) -> EvalSummary | None:
    """把多个 EvalSummary 按样本加权合并成一个总体汇总。"""
    n = sum(s.n for s in ss)
    if n == 0:
        return None
    ex = sum(s.ex * s.n for s in ss) / n
    em = sum(s.em * s.n for s in ss) / n
    attempts = sum(s.avg_attempts * s.n for s in ss) / n
    repairs = sum(s.avg_repairs * s.n for s in ss) / n
    lat = sum(s.avg_latency_ms * s.n for s in ss) / n
    by_route = {}
    for r in ("simple", "complex"):
        subs = [s.by_route[r] for s in ss if s.by_route and r in s.by_route]
        if subs:
            nr = sum(x["n"] for x in subs)
            by_route[r] = {"n": nr, "ex": sum(x["ex"] * x["n"] for x in subs) / nr,
                           "avg_repairs": sum(x["avg_repairs"] * x["n"] for x in subs) / nr,
                           "avg_latency_ms": sum(x["avg_latency_ms"] * x["n"] for x in subs) / nr}
    n_gold_failed = sum(s.gold_failed for s in ss)
    # token / 成本是"总量"字段，跨库合并要累加（不是加权平均），
    # 再据此算出加权后的平均值。漏掉这些字段会让 `run_eval` 的臂汇总
    # 丢失 token/成本（README 承诺的工程指标）。
    tot_tok = sum(s.total_tokens for s in ss)
    tot_prompt = sum(s.prompt_tokens for s in ss)
    tot_completion = sum(s.completion_tokens for s in ss)
    tot_cost = sum(s.total_cost for s in ss)
    return EvalSummary(n=n, ex=ex, em=em, avg_attempts=attempts, avg_repairs=repairs,
                       avg_latency_ms=lat, gold_failed=n_gold_failed,
                       gold_failed_rate=round(n_gold_failed / n, 4) if n else 0.0,
                       total_tokens=tot_tok, prompt_tokens=tot_prompt,
                       completion_tokens=tot_completion,
                       total_cost=round(tot_cost, 6),
                       avg_tokens=round(tot_tok / n, 1),
                       avg_cost=round(tot_cost / n, 6),
                       by_route=by_route)


def _arm_save_path(arm: str, engine: str, out_dir: Path | None) -> str | None:
    """为一条实验臂生成逐题落盘路径。

    修复前只有主跑路径会落盘，消融/基线/评测臂都不落盘 —— 于是"结果无法复核"
    这件事在方法层面就是必然的。现在每条臂都会写出逐题 JSON（含每题 SQL/路由/
    修复轮次/gold_failed），便于事后核对与回归对比。
    """
    if out_dir is None:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", arm)
    return str(out_dir / f"{safe}.{engine}.json")


def _arm_summaries(db_bms, llm, out_dir, arm: str, engine: str, **kw):
    """按臂跑 benchmark 并落盘，返回各库 summary 列表。"""
    ss = []
    for _db_id, bm in db_bms:
        ss.append(run_benchmark(bm, llm, engine=engine,
                                save_path=_arm_save_path(f"{arm}_{_db_id}", engine, out_dir),
                                **kw))
    return ss


def run_ablation_multi(db_bms, llm, max_rounds=(0, 1, 3), engine: str = "pipeline",
                       out_dir: Path | None = None) -> None:
    """在同一批（跨库）样本上跑自愈深度消融，L0/L1/L3 对齐可对比。"""
    print("\n=== 自愈深度消融（同一批样本，跨库）===")
    print("  max_repair=0=单次直出(无自愈) | 3=多Agent全自愈")
    for lvl in max_rounds:
        ss = _arm_summaries(db_bms, llm, out_dir, f"ablation_L{lvl}", engine,
                            max_repair_round=lvl)
        ag = agg_summaries(ss)
        if ag:
            line = (f"  max_repair={lvl}: EX={ag.ex:.4f} EM={ag.em:.4f} "
                    f"修复={ag.avg_repairs:.2f} 延迟={ag.avg_latency_ms:.0f}ms"
                    f" gold失败={ag.gold_failed}"
                    f" tok={ag.avg_tokens:.0f}/题 成本=¥{ag.avg_cost:.5f}/题")
            for r, v in ag.by_route.items():
                line += f" | {r}:EX={v['ex']:.4f}(n={v['n']})"
            print(line)


def run_critic_ablation(db_bms, llm, critic_only: bool = False, engine: str = "pipeline",
                        out_dir: Path | None = None) -> None:
    """多agent协作消融：同一批样本上, 单Writer vs Writer+Critic 的 EX 对比(含简单/复杂分档)。"""
    print("\n=== 多Agent协作消融: 单Writer vs Writer+Critic ===")
    print("  (真实 LLM 才有效果; 关键看 complex 的差距)")
    arms = [("Writer+Critic", True)] if critic_only else [("单Writer(无Critic)", False), ("Writer+Critic", True)]
    for label, use_critic in arms:
        ss = _arm_summaries(db_bms, llm, out_dir,
                            ("critic_on" if use_critic else "critic_off"), engine,
                            use_critic=use_critic)
        ag = agg_summaries(ss)
        if ag:
            line = (f"  {label}: EX={ag.ex:.4f} EM={ag.em:.4f} "
                    f"修复={ag.avg_repairs:.2f} 延迟={ag.avg_latency_ms:.0f}ms"
                    f" gold失败={ag.gold_failed}"
                    f" tok={ag.avg_tokens:.0f}/题 成本=¥{ag.avg_cost:.5f}/题")
            for r, v in ag.by_route.items():
                line += f" | {r}:EX={v['ex']:.4f}(n={v['n']})"
            print(line)


def run_baseline(db_bms, llm, ref=None, engine: str = "pipeline",
                 out_dir: Path | None = None) -> None:
    """基线对照：单次直出(zero-shot, L0) vs 完整引擎(自愈L1甜点位)，并可对标公开基线 --ref。"""
    print("\n=== 基线对照: 单次直出(zero-shot) vs 完整引擎 ===")
    b_ss = _arm_summaries(db_bms, llm, out_dir, "baseline_zeroshot", engine,
                          max_repair_round=0)
    o_ss = _arm_summaries(db_bms, llm, out_dir, "baseline_engineL1", engine,
                          max_repair_round=1, use_critic=False)
    b, o = agg_summaries(b_ss), agg_summaries(o_ss)
    if b and o:
        print(f"  单次直出 zero-shot(无自愈): EX={b.ex:.4f} (gold失败={b.gold_failed}) tok={b.avg_tokens:.0f}/题 成本=¥{b.avg_cost:.5f}/题")
        print(f"  完整引擎(自愈L1):          EX={o.ex:.4f} (gold失败={o.gold_failed}) tok={o.avg_tokens:.0f}/题 成本=¥{o.avg_cost:.5f}/题")
        print(f"  提升: {(o.ex - b.ex) * 100:+.1f} pp")
        if ref is not None:
            print(f"  对标公开 Spider-dev 基线 {ref:.3f}: 相对 {(o.ex - ref) * 100:+.1f} pp")


def run_schema_link_ablation(db_bms, llm, engine: str = "pipeline",
                             out_dir: Path | None = None) -> None:
    """Schema-Linker 消融：同一批样本上, 关(全量schema) vs 开(列级裁剪) 的 EX/token 对比。"""
    print("\n=== Schema-Linker 消融: 关(全量schema) vs 开(列级裁剪) ===")
    for label, use in [("关(全量schema)", False), ("开(列级裁剪)", True)]:
        ss = _arm_summaries(db_bms, llm, out_dir,
                            ("schemalink_on" if use else "schemalink_off"), engine,
                            use_schema_link=use)
        ag = agg_summaries(ss)
        if ag:
            print(f"  {label}: EX={ag.ex:.4f} EM={ag.em:.4f} "
                  f"修复={ag.avg_repairs:.2f} 延迟={ag.avg_latency_ms:.0f}ms"
                  f" gold失败={ag.gold_failed}"
                    f" tok={ag.avg_tokens:.0f}/题 成本=¥{ag.avg_cost:.5f}/题")


def main() -> int:
    ap = argparse.ArgumentParser(description="多智能体 Text-to-SQL 评测主入口")
    ap.add_argument("--dataset", default="spider", choices=["spider", "bird"])
    ap.add_argument("--db-root", default=None)
    ap.add_argument("--split", default="dev", help="spider/bird 的 split")
    ap.add_argument("--ablation", action="store_true", help="自愈深度消融 L0/L1/L3")
    ap.add_argument("--ablation-only", action="store_true",
                    help="只跑消融 L0/L1/L3（跳过主跑，省时）")
    ap.add_argument("--mock", action="store_true", help="强制用 MockLLM（演示用）")
    ap.add_argument("--critic", action="store_true", help="启用 Writer↔Critic 闭环(多agent协作)")
    ap.add_argument("--critic-ablation", action="store_true",
                    help="多agent协作消融: 单Writer vs Writer+Critic 的EX对比")
    ap.add_argument("--critic-only", action="store_true",
                    help="只跑 Writer+Critic 臂(补另一半,省时)")
    ap.add_argument("--out-dir", default=None,
                    help="各臂逐题 JSON 输出目录（默认 eval_results/，已 gitignore）")
    ap.add_argument("--baseline", action="store_true",
                    help="基线对照: 单次直出(zero-shot) vs 完整引擎, 并(可选)对标 --ref")
    ap.add_argument("--ref", type=float, default=None,
                    help="公开 Spider-dev 基线EX(用于对照,如 0.87)")
    ap.add_argument("--schema-link", action="store_true",
                    help="启用 Schema-Linker(列级裁剪schema,省token/降噪)")
    ap.add_argument("--schema-link-ablation", action="store_true",
                    help="Schema-Linker 消融: 关(全量schema) vs 开(列级裁剪) 的EX对比")
    ap.add_argument("--engine", default="langgraph", choices=["pipeline", "langgraph"],
                    help="引擎: langgraph(LangGraph StateGraph, 默认) / pipeline(确定性编排器回退)")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条(0=全部)")
    ap.add_argument("--sample", type=int, default=0,
                    help="跨库分层随机抽样 N 条(更代表性；优先于 --limit)")
    ap.add_argument("--seed", type=int, default=42, help="抽样随机种子(默认42，可复现)")
    ap.add_argument("--max-repair", type=int, default=None,
                    help="自愈最大轮次（默认读 config/settings.yaml）")
    ap.add_argument("--ablation-rounds", default="1,3",
                    help="自愈消融要跑的档位，逗号分隔（默认 1,3）。"
                         "注意：L0(单次直出)已由 --baseline 覆盖，重复跑纯属浪费；"
                         "想完整复现 L0/L1/L3 用 --ablation-rounds 0,1,3")
    ap.add_argument("--out", default="eval_result.json")
    ap.add_argument("--key", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--temperature", type=float, default=0.0)
    args = ap.parse_args()
    if args.max_repair is None:
        from sqlpa.config import get as cfg_get
        args.max_repair = int(cfg_get("pipeline.max_repair_round", 3))

    ab_benchmark = None

    # ---- 真实数据集（Spider/BIRD）：按库分组跑 ----
    from sqlpa.data.schema_extractor import extract_from_sqlite  # noqa: F401
    from sqlpa.sandbox.sql_executor import SqlSandbox  # noqa: F401
    # 数据集根目录优先级：--db-root > 环境变量 EVAL_DB_ROOT（.env 里配）> data/<dataset>
    # （此前 --db-root 默认值写死 data/<dataset>，导致 .env 的 EVAL_DB_ROOT 形同虚设）
    db_root = Path(args.db_root or os.environ.get("EVAL_DB_ROOT") or f"data/{args.dataset}")
    json_path = db_root / f"{args.split}.json"
    if not json_path.exists():
        raise FileNotFoundError(
            f"找不到 {json_path}。请确认 --db-root / EVAL_DB_ROOT 指向数据集根目录"
            f"（该目录下应有 {args.split}.json 与 database/）。")
    per_db = load_spider_dev(json_path, db_root / "database")
    # 收集 (db_id, question)，支持"跨库轮询抽样"(--sample) 或 "全局前 N 条"(--limit)
    if args.sample:
        # 分层随机：每库先随机打乱，再轮询取够 N 条（种子可复现）
        rng = random.Random(args.seed)
        buckets = []
        for db_id, bm in per_db.items():
            if Path(bm.db_path).exists():
                qs = [(db_id, q) for q in bm.questions]
                rng.shuffle(qs)
                buckets.append(qs)
        flat = []
        while len(flat) < args.sample and any(buckets):
            for b in buckets:
                if b and len(flat) < args.sample:
                    flat.append(b.pop(0))
        print(f"  [info] 跨库分层随机抽样 {len(flat)} 条"
              f"（覆盖 {sum(1 for b in buckets if b)} 个库，seed={args.seed}）")
    else:
        flat = [(db_id, q) for db_id, bm in per_db.items()
                if Path(bm.db_path).exists() for q in bm.questions]
        if args.limit:
            flat = flat[: args.limit]
        print(f"  [info] 将评测 {len(flat)} 条样本（--limit={args.limit or '不限'}）")

    order = []
    by_db = {}
    for db_id, q in flat:
        order.append(db_id)
        by_db.setdefault(db_id, []).append(q)
    if not order:
        print("  [!!] 没有可评测的样本，请检查 db_root/database 是否存在 .sqlite")
        return 1

    llm = build_llm(args)
    # 组装"库 -> 该库抽到的样本"（保持抽样顺序）
    db_bms = []
    for db_id in dict.fromkeys(order):
        bm = per_db[db_id]
        bm.questions = by_db[db_id]
        db_bms.append((db_id, bm))

    # 逐题产物目录（所有臂统一用；可用 --out-dir 指定）。这必须在主跑分支之前定义。
    out_dir = Path(args.out_dir) if args.out_dir else (ROOT / "eval_results")

    if not (args.ablation or args.ablation_only or args.critic_ablation or args.baseline
            or args.schema_link_ablation):
        ss = []
        for db_id, bm in db_bms:
            # 主跑路径同样落到 --out-dir（此前写死在项目根，--out-dir 对它无效，
            # 会在仓库根目录堆一堆 eval_result.json.<db>.json）
            summary = run_benchmark(bm, llm, max_repair_round=args.max_repair,
                                    save_path=_arm_save_path(f"main_{db_id}", args.engine, out_dir),
                                    use_critic=args.critic, use_schema_link=args.schema_link,
                                    engine=args.engine)
            print(f"\n=== {db_id} ({len(bm.questions)} 条) ===")
            print(report(summary))
            ss.append(summary)

        ag = agg_summaries(ss)
        if ag:
            print("\n===== 全量汇总（按样本加权）=====")
            print(f"样本: {ag.n}   EX: {ag.ex:.4f}   EM: {ag.em:.4f}   "
                  f"平均修复: {ag.avg_repairs:.2f}   "
                  f"平均延迟: {ag.avg_latency_ms:.0f} ms")

    # 每条臂的逐题产物目录（已在前面解析为 out_dir）；这是"数字可复核"的前提
    if args.ablation or args.ablation_only or args.critic_ablation or args.baseline \
            or args.schema_link_ablation:
        print(f"  [info] 各臂逐题结果将写入 {out_dir}/")

    if (args.ablation or args.ablation_only) and db_bms:
        try:
            rounds = tuple(int(x) for x in str(args.ablation_rounds).split(",") if x.strip())
        except ValueError:
            print(f"  [!!] --ablation-rounds 解析失败：{args.ablation_rounds}，回退 1,3")
            rounds = (1, 3)
        run_ablation_multi(db_bms, llm, max_rounds=rounds, engine=args.engine, out_dir=out_dir)
    if args.critic_ablation and db_bms:
        run_critic_ablation(db_bms, llm, critic_only=args.critic_only,
                            engine=args.engine, out_dir=out_dir)
    if args.baseline and db_bms:
        run_baseline(db_bms, llm, args.ref, engine=args.engine, out_dir=out_dir)
    if args.schema_link_ablation and db_bms:
        run_schema_link_ablation(db_bms, llm, engine=args.engine, out_dir=out_dir)
    print("\n完成。真实准确率需接入真实 LLM + 真实数据集；"
          f"逐题产物见 {out_dir}/（请连同数字一起提交以便复核）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
