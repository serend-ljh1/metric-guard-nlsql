"""
evaluation/eval_attribution_llm_baseline.py
===========================================
归因层对照：确定性算术引擎 vs 纯 LLM（从原始汇总里挑主因）。

目的：回答面试官"为什么需要你这套确定性归因引擎，直接让 LLM 看数不就行了"。
做法（Self-Base on the SAME 判题）：
  1. 读上次爆破版 `reports/attribution.json` 的**有效判题**（is_abnormal=True，注入真因，
     引擎已给出 top-1 归属 dim_top_key / 是否命中 truth=value）。
  2. 对每条：在临时副本上**重放同一注入** → 查出该维 Top-N 段的"上月/本月"原始 GMV
     （**不喂算好的 delta**）→ 让纯 LLM 判断"最可能是哪个段主导了变化"。
  3. 打分：engine_hit@1（引擎 top-1==truth） vs llm_hit@1（LLM top==truth），
     以及 llm↔engine 一致性 / 解析失败率。

结论口径（诚实）：二者都需要 injected 段恰好是全局/维度第一才谈得上 hit@1；
对照看的是**在同一批判题上 LLM 是否退化**（算术错、只按体量不按变化、胡编段名），
从而量化"确定性引擎挡掉的退化"。

用法：
    python -m evaluation.eval_attribution_llm_baseline --limit 30
    python -m evaluation.eval_attribution_llm_baseline --limit 30 --out evaluation/reports/attribution_llm_baseline.json
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from evaluation import eval_attribution as _ea  # noqa: E402


def _inject_and_query(real_db: str, dim: str, value: str, cur_spec: str,
                      factor: float, k: int = 0) -> tuple:
    """copy→注入→返回(**两期并集**的分段表, 当期/上期总 GMV, 表内合计)。

    公平性要点（旧实现的三个偏差）：
      1. 表只含"当期存在"的段 → 当期塌成 0 的段对 LLM 不可见，而引擎的 delta 表包含两期并集，
         等于把"某段归零"这类最重要的主因藏起来了。现在取**两期并集**、缺失记 0。
      2. 表加了 `frag IS NOT NULL` 而合计没有 → 两臂分母不一致。现在表与合计用**同一套
         FROM/WHERE**（NULL 段作为一行"（未标注）"保留），并返回表内合计以供自校验。
      3. 旧实现 `ORDER BY cur DESC LIMIT k` 会把小段截掉。默认 `k=0` 表示**全量段**，
         排序仅按当期体量（中性顺序，不泄漏 |变动| 排名）。
    """
    c0, c1 = _ea.attribution._range_spec(cur_spec)
    p0, _p1 = _ea.attribution._range_spec(_ea.attribution._default_previous_spec(cur_spec))
    _ea._TMP.mkdir(parents=True, exist_ok=True)
    work = _ea._TMP / f"llm-att-{uuid.uuid4().hex[:10]}.db"
    shutil.copyfile(real_db, work)
    con = sqlite3.connect(work)
    _ea._INJECT_FN[dim](value, c0, c1, factor, con)
    con.commit()

    frag = {"state": "c.customer_state", "category": "p.product_category_name"}[dim]
    join = ("JOIN order_items oi ON o.order_id=oi.order_id "
            "JOIN customers c ON o.customer_id=c.customer_id" if dim == "state" else
            "JOIN order_items oi ON o.order_id=oi.order_id "
            "JOIN products p ON oi.product_id=p.product_id")
    base_where = (f"o.order_status!='canceled' AND {frag} IS NOT NULL AND "
                  f"o.order_purchase_timestamp>={p0} AND o.order_purchase_timestamp<{c1}")
    limit_sql = f" LIMIT {int(k)}" if k and k > 0 else ""
    rows = con.execute(
        f"SELECT {frag} AS d, "
        f" COALESCE(SUM(CASE WHEN o.order_purchase_timestamp>={c0} "
        f"                   AND o.order_purchase_timestamp<{c1} THEN oi.price END), 0) AS cur, "
        f" COALESCE(SUM(CASE WHEN o.order_purchase_timestamp>={p0} "
        f"                   AND o.order_purchase_timestamp<{c0} THEN oi.price END), 0) AS prev "
        f"FROM orders o {join} WHERE {base_where} GROUP BY {frag} "
        f"ORDER BY cur DESC{limit_sql}"
    ).fetchall()
    totals = con.execute(
        f"SELECT "
        f" COALESCE(SUM(CASE WHEN o.order_purchase_timestamp>={c0} "
        f"                   AND o.order_purchase_timestamp<{c1} THEN oi.price END), 0) AS cur, "
        f" COALESCE(SUM(CASE WHEN o.order_purchase_timestamp>={p0} "
        f"                   AND o.order_purchase_timestamp<{c0} THEN oi.price END), 0) AS prev "
        f"FROM orders o {join} WHERE {base_where}"
    ).fetchone()
    con.close()
    work.unlink(missing_ok=True)
    # 自校验：表内合计必须等于总合计（否则两臂信息/分母仍不一致）
    table_sum = (sum(r[1] or 0 for r in rows), sum(r[2] or 0 for r in rows))
    sums_match = abs(table_sum[0] - (totals[0] or 0)) < 0.01 and \
        abs(table_sum[1] - (totals[1] or 0)) < 0.01
    return rows, (totals[0], totals[1]), sums_match


_LABEL = {"state": "州", "category": "品类"}


def _prompt(dim: str, rows: List[tuple], totals: tuple) -> str:
    cur_t, prev_t = totals
    chg = (cur_t - prev_t) / prev_t if prev_t else 0.0
    name = _LABEL[dim]
    lines = []
    for seg, cur, prev in rows:
        lines.append(f"- {seg}: 上月 {prev:.2f}, 本月 {cur:.2f}")
    return (
        f"月度GMV归因判断题。某电商平台按{name}统计的GMV(上月 vs 本月)如下。\n"
        f"本月GMV合计={cur_t:.2f}，上月={prev_t:.2f}，总变动 {chg:+.1%}。\n"
        + "\n".join(lines) +
        f"\n请先对每个{name}计算**变动量 = 本月 − 上月**，再回答："
        f"本月GMV相对上月的变化，主要由哪一个{name}的变动主导"
        f"（即变动量**绝对值最大**的那一个）。\n"
        f"你拥有与确定性引擎完全相同的原始数据，可以做减法，不要只按体量大小判断。\n"
        f"只输出一个 JSON，格式 {{\"top\":\"<该{name}的名称>\"}}；"
        f"若数据确实不足或无法确定则输出 {{\"top\":null}}。不要其他任何文字。"
    )


def _parse_top(text: str, candidates: Optional[List[str]] = None) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    # 去掉 markdown 代码围栏
    t = re.sub(r"```(?:json)?", "", t)
    m = re.search(r"\{.*?\}", t, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            obj = None
        if obj:
            v = obj.get("top")
            if isinstance(v, str) and v:
                return v
            # 显式 null / 无法确定 → 弃权
            if v is None or str(v).strip() in ("", "null", "None"):
                return None
    # 宽松：top"…" 或 top：…（含全角冒号/引号）
    m2 = re.search(r'["\u201c]?top["\u201d]?\s*[:：]\s*["\u201c\']?([^"\u201d\'\s][^"\u201d\']*)["\u201d\']?', text, re.S)
    if m2:
        val = m2.group(1).strip().rstrip("}")
        if val and re.search(r"^(null|None)$", val, re.I):
            return None
        if val:
            return val
    # 兜底：候选集里那个被明确当作答案写出的值
    if candidates:
        for cand in sorted(candidates, key=len, reverse=True):
            if cand and cand in text:
                return cand
    # 显式"无法确定/无" → 弃权（返回 None 而非当判错）
    if re.search(r"null|无法确定|无法判断|None|不确定", text or "", re.I):
        return None
    return None


def run_case(llm, cfg, real_db: str, case: Dict, topk: int) -> Dict:
    dim, value = case["dim"], case["value"]
    cur_spec = case["current_spec"]
    factor = 1.0 - case["inject_pct"]
    rows, totals, sums_match = _inject_and_query(real_db, dim, value, cur_spec, factor, topk)
    text = str(llm.complete(_prompt(dim, rows, totals)))
    llm_top = _parse_top(text, candidates=[str(r[0]) for r in rows][:topk or len(rows)])
    return {
        "dim": dim, "value": value, "period": cur_spec,
        "inject_pct": case["inject_pct"],
        "table_rows": len(rows), "table_sums_match_totals": bool(sums_match),
        # 引擎口径来自 attribution.json（其命中判定已改为**独立真值表**）
        "engine_top": case.get("dim_top_key"),
        "engine_hit": bool(case.get("hit")),
        "engine_basis": "replayed from attribution.json (truth-table based)",
        "llm_top": llm_top,
        "llm_hit": bool(llm_top is not None and llm_top == value),
        "agree_with_engine": bool(llm_top is not None and llm_top == case.get("dim_top_key")),
        "parse_fail": bool(llm_top is None),
        "raw": text,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="归因：确定性引擎 vs 纯LLM 对照基线")
    ap.add_argument("--cases", default=str(ROOT / "evaluation" / "reports" / "attribution.json"))
    ap.add_argument("--db", default=None, help="真库；默认解析到全量库")
    ap.add_argument("--limit", type=int, default=30,
                    help="最多跑多少条 LLM（控 token/时间）；0 = 全部有效判题")
    ap.add_argument("--seed", type=int, default=20260920,
                    help="分层抽样随机种子（固定种子 → 抽样可复现、不是事后挑好看的子集）")
    ap.add_argument("--topk-table", type=int, default=0,
                    help="喂给 LLM 的分段表行数；0=全量段（默认，保证与引擎信息对等）")
    ap.add_argument("--model", default=None)
    ap.add_argument("--debug", action="store_true", help="打印每条 LLM 原始输出")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    import os
    from dotenv import load_dotenv
    load_dotenv()
    from sqlpa.config import ensure_utf8_console, repo_relative, resolve_db_path
    from sqlpa.business.metric_config import load_config
    from sqlpa.llm.openai_compat import OpenAICompatLLM
    ensure_utf8_console()

    real = args.db or (resolve_db_path()[0] if isinstance(resolve_db_path(), tuple)
                       else resolve_db_path())
    cfg = load_config()
    model = args.model or os.environ.get("LLM_MODEL") or "qwen3.8-max"
    llm = OpenAICompatLLM(model=model, temperature=0.0,
                          model_pool=[model], max_retries=2, timeout=180)
    print(f"对照模型：{model}  真库：{real}")

    data = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    valid = [c for c in data["attribution"]["details"] if c.get("is_abnormal")]
    # ---- 分层抽样：按 (维度 × 期 × 注入档) 分层后**固定种子**随机抽，覆盖全部三层 ----
    strata: Dict[tuple, List[Dict]] = {}
    for c in valid:
        strata.setdefault((c["dim"], c["current_spec"], c["inject_pct"]), []).append(c)
    rng = random.Random(args.seed)
    picked: List[Dict] = []
    for key in sorted(strata):
        bucket = sorted(strata[key], key=lambda c: (c["value"],))
        rng.shuffle(bucket)
        picked.append(bucket[0])                      # 每层至少 1 条 → 覆盖面不偏
    rest = [c for c in valid if c not in picked]
    rng.shuffle(rest)
    picked += rest
    if args.limit and args.limit > 0:
        picked = picked[:args.limit]
    print(f"可用有效判题 {len(valid)} 条；分层 {len(strata)} 层，种子 {args.seed}，"
          f"跑 {len(picked)} 条（每层至少 1 条，其余随机补足）")

    results: List[Dict] = []
    for case in picked:
        r = run_case(llm, cfg, real, case, args.topk_table)
        results.append(r)
        mark = "H   " if r["llm_hit"] else ("X   " if not r["parse_fail"] else "PARSE")
        print(f"  [{mark}] {r['dim']}='{r['value']}' @{r['period']}(注入{r['inject_pct']:.0%}) "
              f"engine_top={r['engine_top']} llm_top={r['llm_top']}")
        if args.debug:
            print("       RAW:", str(r["raw"])[:240].replace(chr(10), " "))

    n = len(results) or 1
    e_hit = sum(1 for r in results if r["engine_hit"])
    l_hit = sum(1 for r in results if r["llm_hit"])
    agree = sum(1 for r in results if r["agree_with_engine"])
    pfail = sum(1 for r in results if r["parse_fail"])
    print("=" * 66)
    print(f"纯 LLM 归因 top@1: {l_hit}/{len(results)} = {l_hit / n:.1%}   "
          f"(同一批判题) 确定性引擎 top@1: {e_hit}/{len(results)} = {e_hit / n:.1%}")
    print(f"llm 与引擎一致 {agree}/{len(results)}  解析失败 {pfail}")

    report = {
        "suite": "attribution_llm_baseline",
        "desc": ("同一批注入真因判题上，确定性算术引擎 vs 纯 LLM(原始分组合计) 的 top@1 对照。"
                 "两臂信息对等：分段表取两期并集、缺失记 0、与合计同一 WHERE；"
                 "prompt 明写『变动量 = 本月 − 上月』并提示可做减法。"),
        "db": repo_relative(real), "model": model, "limit": args.limit, "seed": args.seed,
        "topk_table": args.topk_table,
        "sampling": ("按 (维度×期×注入档) 分层，固定种子随机抽样；每层至少 1 条，"
                     "其余随机补足 —— 不是事后挑引擎表现好的时段"),
        "strata": len(strata), "valid_pool": len(valid),
        "command": ("python evaluation/eval_attribution_llm_baseline.py "
                    f"--limit {args.limit} --seed {args.seed} --topk-table {args.topk_table} "
                    f"--model {model}"),
        "engine_basis": "replayed from attribution.json (its hits are truth-table based)",
        "engine_top1": round(e_hit / n, 4), "llm_top1": round(l_hit / n, 4),
        "engine_hits": e_hit, "llm_hits": l_hit, "n": len(results),
        "agree_with_engine": round(agree / n, 4), "parse_fail": pfail,
        "table_universe_mismatch": sum(1 for r in results
                                       if not r.get("table_sums_match_totals")),
        "details": results,
    }
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n产物 -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())