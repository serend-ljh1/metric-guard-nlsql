"""
evaluation/eval_process.py
==========================
**过程级 Agent 指标**评测（L5）：不问"结果对不对"，而问"编排过程好不好"。

为什么需要这一条线：既有的 7 条线全部在评"**结果**"——口径命中、归因命中、告警质量。
它们无法回答"这个多 Agent 系统本身跑得怎么样"：选对工具了吗？走了几步？有没有空转？
候选里有没有真因？有多少步被确定性规则省下了 LLM 调用？这些是**过程质量**，
只有把每个 Agent 的决策轨迹（L4 决策轨迹：候选/选中/被拒/门控）摊开才量得出来。

本文件在**一次完整编排**（`run_analysis`：Router→Executor→Attribution→Conclusion→Decision）
上，对一组自带真值的业务场景统计 5 个过程指标：

  1. **工具选择准确率** tool_selection_accuracy
     ：RouterAgent 的分流（analyze / query_only / unmatched）+ 命中指标，是否与场景真值一致。
     —— 衡量"该分析的去分析、该取数的去取数、该拒的拒"。
  2. **平均步数** avg_steps
     ：每个场景平均发射多少个 `agent_step`（过程可观测的粒度成本）。
  3. **无效步骤率** invalid_step_rate
     ：未产出可用产物的步骤占比（硬异常 / 路由拒绝 / 失败产物），越低越好。
  4. **假设命中率@k** hypothesis_hit_rate
     ：注入式真因下，真凶是否落在归因候选的前 k 名（k=1/3/5）。衡量"候选池覆盖真因"的能力，
     与"排到第几"解耦（排序质量在 attribution 线里评）。
  5. **门控节省率** gate_saving_rate
     ：下钻决策中，由确定性规则/门控直接给出（未消耗 LLM token）的占比。

全部离线、确定性、可复现：用一个自建小库 + 离线 mock LLM，不碰真实 Olist、无需 API Key。

用法：
    python evaluation/eval_process.py
    python evaluation/eval_process.py --report-dir evaluation/reports
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import sys as _sys
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional

# 路径引导：`python evaluation/eval_process.py` 直接运行时项目根/`src` 不在 sys.path
try:
    from sqlpa.analysis.orchestrator import run_analysis
    from sqlpa.business.metric_config import load_config
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox
except ImportError:  # pragma: no cover - 独立运行兜底
    _sys.path.insert(0, str((Path(__file__).resolve().parents[1] / "src").resolve()))
    from sqlpa.analysis.orchestrator import run_analysis
    from sqlpa.business.metric_config import load_config
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 离线 mock LLM

class _ProcLLM:
    """离线 mock LLM：让"真追问"走到 LLM 判断、"非追问/量价"被规则短路。

    为什么要 mock 而不是 llm=None：若整条链都不接 LLM，规则短路会把**所有**决策
    都变成 rule，门控节省率恒为 100%，量不出任何区分。这里让 mock 对"决策 JSON"
    提示返回一个合法 action（decision_source=llm），对意图识别返回缺 metric 的 JSON
    （→ 自然回落到关键词兜底），对结论返回一句话 —— 于是能测出规则与 LLM 的真实分界。
    """

    def __init__(self, action: str = "none"):
        self.action = action
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0}

    def complete(self, prompt: str) -> str:
        self.usage["calls"] += 1
        if "只输出 JSON" in prompt:
            # 该提示同时出现在 metric_matcher（要 metric）与 decide_drill（要 action）。
            # 返回带 action、不带 metric 的 JSON：意图识别读到空 metric → 回落关键词，
            # 下钻决策读到合法 action → decision_source=llm。
            return json.dumps({"action": self.action, "reason": "mock 判断"},
                              ensure_ascii=False)
        return "（mock）本期指标出现明显波动，主因见结构化依据。"

    def stats(self) -> Dict:
        return {"usage": dict(self.usage), "cost": 0.0, "last_model": "mock"}


# ---------------------------------------------------------------- 自建真因库

def _month_anchors() -> Dict[str, str]:
    """与「本月 / 上月」语义对齐的日期锚点（按运行当天动态计算，避免日期炸弹）。"""
    first = datetime.date.today().replace(day=1)
    prev_first = (first - datetime.timedelta(days=1)).replace(day=1)
    return {"current": first.isoformat(), "previous": prev_first.isoformat()}


def _build_truth_db(tmp: Path, truth_state: str = "SP") -> str:
    """构造"真因明确"的小库：真凶集中在一个州（默认 SP），其余州两期不变。

    SP 当期保留 10%、且**按品类等比缩减**，因此无论按州还是按品类拆解，
    真凶（该州，|delta| 最大）都稳居候选第一 —— 保证注入式真因可判定。
    """
    m = _month_anchors()
    cur, prev = m["current"], m["previous"]
    p = tmp / f"process-{uuid.uuid4().hex[:6]}.db"
    con = sqlite3.connect(p)
    # 州/品类布局：c1=真凶州(默认 SP), c2=RJ, c3=MG；p1=alimentos, p2=health, p3=brinquedos
    rows_orders = [
        # 上月：SP 1000（p1=400,p2=500,p3=100）/ RJ 200 / MG 100
        ("o1", "c1", prev, "p1", 400), ("o2", "c1", prev, "p2", 500),
        ("o3", "c1", prev, "p3", 100),
        ("o4", "c2", prev, "p1", 100), ("o5", "c2", prev, "p2", 100),
        ("o6", "c3", prev, "p1", 100),
        # 本月：SP 缩到 10%（等比）/ RJ、MG 不变
        ("o7", "c1", cur, "p1", 40), ("o8", "c1", cur, "p2", 50),
        ("o9", "c1", cur, "p3", 10),
        ("o10", "c2", cur, "p1", 100), ("o11", "c2", cur, "p2", 100),
        ("o12", "c3", cur, "p1", 100),
    ]
    con.executescript("""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT PRIMARY KEY, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT PRIMARY KEY, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT PRIMARY KEY, customer_state TEXT);
    """)
    con.executemany("INSERT INTO customers VALUES (?,?)",
                    [("c1", truth_state), ("c2", "RJ"), ("c3", "MG")])
    for oid, cust, ts, pid, price in rows_orders:
        con.execute("INSERT INTO orders VALUES (?,?,?,'delivered')", (oid, cust, ts))
        con.execute("INSERT INTO order_items VALUES (?,?,?)", (oid, pid, price))
    con.executemany("INSERT INTO products VALUES (?,?)",
                    [("p1", "alimentos"), ("p2", "health"), ("p3", "brinquedos")])
    con.commit()
    con.close()
    return str(p)


# ---------------------------------------------------------------- 场景集（自带真值）

# expected_route：RouterAgent 应给出的分流；expected_metric：命中的指标（未命中为 None）
# truth_key：注入式真因（该维度值应为候选第一）；None 表示该场景不参与假设命中率
SCENARIOS: List[Dict] = [
    {"name": "分析-为什么跌", "question": "本月 GMV 为什么跌？",
     "route": "analyze", "metric": "gmv", "truth_key": "SP"},
    {"name": "分析-定向州", "question": "本月 GMV 下滑主要出在哪个州？",
     "route": "analyze", "metric": "gmv", "truth_key": "SP"},
    {"name": "分析-订单量为什么跌", "question": "本月订单量为什么跌？",
     "route": "analyze", "metric": "order_count", "truth_key": None},
    {"name": "分析-客单价为什么跌", "question": "本月客单价为什么跌？",
     "route": "analyze", "metric": "aov", "truth_key": "SP"},
    {"name": "分析-量价拆解", "question": "本月 GMV 为什么跌，按因子做乘积分解？",
     "route": "analyze", "metric": "gmv", "truth_key": "SP"},
    {"name": "分析-定向品类", "question": "本月 GMV 下滑出在哪个品类？",
     "route": "analyze", "metric": "gmv", "truth_key": None},
    {"name": "取数-无时间", "question": "GMV 是多少？",
     "route": "query_only", "metric": "gmv", "truth_key": None},
    {"name": "取数-各州订单量", "question": "各州订单量对比一下",
     "route": "query_only", "metric": "order_count", "truth_key": None},
    {"name": "拒绝-无关问题", "question": "今天天气怎么样？",
     "route": "unmatched", "metric": None, "truth_key": None},
]


# ---------------------------------------------------------------- 过程指标计算

def _collect(events: List[Dict]) -> Dict:
    """从一次编排的事件流里抽出过程指标所需的原始计数。"""
    steps = [e for e in events if e.get("type") == "agent_step"]
    errors = [e for e in events if e.get("type") == "error"]
    dones = [e for e in events if e.get("type") == "agent_done"]
    decisions = [e for e in events if e.get("type") == "agent_decision"]

    # 失败产物：agent_done 明确报 ok=False，或带 error 键
    failed_done = [d for d in dones
                   if isinstance(d.get("payload"), dict)
                   and (d["payload"].get("ok") is False or "error" in d["payload"])]

    steps_by_agent: Dict[str, int] = {}
    for s in steps:
        steps_by_agent[s.get("name", "?")] = steps_by_agent.get(s.get("name", "?"), 0) + 1

    # 路由分流（RouterAgent 的决策轨迹）
    route_dec = next((d for d in decisions if d.get("name") == "RouterAgent"
                      and d.get("stage") == "route"), None)
    chosen = (route_dec or {}).get("payload", {}).get("chosen") or {}

    # 归因候选（AttributionAgent analyze 步骤里带 dim/key/delta，按 |delta| 排）
    cands = []
    for s in steps:
        if s.get("name") == "AttributionAgent" and s.get("stage") == "analyze":
            p = s.get("payload") or {}
            if p.get("key") is not None:
                cands.append({"dim": p.get("dim"), "key": p.get("key"),
                              "delta": p.get("delta")})
    cands.sort(key=lambda c: abs(float(c.get("delta") or 0)), reverse=True)

    # 下钻决策来源（rule=确定性短路 / llm / memory）
    drill_dec = next((d for d in decisions if d.get("name") == "AttributionAgent"
                      and d.get("stage") == "decide_drill"), None)
    decision_source = ((drill_dec or {}).get("payload", {}).get("chosen") or {}).get(
        "decision_source")

    return {"steps": steps, "n_steps": len(steps), "n_errors": len(errors),
            "n_failed_done": len(failed_done), "steps_by_agent": steps_by_agent,
            "route": chosen.get("route"), "metric": chosen.get("metric"),
            "candidates": cands, "decision_source": decision_source}


def evaluate(scenarios: Optional[List[Dict]] = None,
             tmp_dir: Optional[Path] = None) -> Dict:
    """跑完整编排并统计 5 个过程指标（离线、确定性）。"""
    scenarios = scenarios if scenarios is not None else SCENARIOS
    cfg = load_config()
    tmp = Path(tmp_dir) if tmp_dir else (Path(tempfile.gettempdir()) / "metric_guard_eval")
    tmp.mkdir(parents=True, exist_ok=True)
    db_path = _build_truth_db(tmp)
    sb = SqlSandbox(db_path, ExecConfig.from_settings(max_rows=2000))

    # 评测不应污染真实 data/app.db：trace / 审计在本线只作旁路，临时短路掉（best-effort）。
    import sqlpa.business.storage as _storage
    import sqlpa.business.audit as _audit
    _orig_trace, _orig_audit = _storage.insert_trace, _audit.append_audit
    _storage.insert_trace = lambda *a, **k: None
    _audit.append_audit = lambda *a, **k: None

    cases: List[Dict] = []
    tot_steps = tot_errors = tot_failed = 0
    rule_dec = llm_dec = other_dec = 0
    tool_ok = 0
    steps_by_agent: Dict[str, int] = {}
    try:
        for sc in scenarios:
            llm = _ProcLLM()   # 每场景独立 mock：调用次数干净可数
            events: List[Dict] = []
            run_analysis(sc["question"], cfg, sb, db_path, llm,
                         role="analyst", alert_threshold_pct=0.05,
                         emit=lambda p: events.append(p))
            c = _collect(events)

            # 1) 工具选择：分流 + 指标是否与真值一致
            route_hit = c["route"] == sc["route"]
            metric_hit = (sc["metric"] is None) or (c["metric"] == sc["metric"])
            sel_hit = bool(route_hit and metric_hit)
            tool_ok += int(sel_hit)

            # 3) 无效步骤
            n_invalid = c["n_errors"] + c["n_failed_done"]
            tot_steps += c["n_steps"]
            tot_errors += c["n_errors"]
            tot_failed += c["n_failed_done"]

            # 5) 门控节省
            src = c["decision_source"]
            if src == "rule":
                rule_dec += 1
            elif src == "llm":
                llm_dec += 1
            elif src:
                other_dec += 1

            for k, v in c["steps_by_agent"].items():
                steps_by_agent[k] = steps_by_agent.get(k, 0) + v

            # 4) 假设命中率：真因是否落在候选前 k 名
            rank = None
            if sc.get("truth_key"):
                for i, cand in enumerate(c["candidates"]):
                    if cand.get("key") == sc["truth_key"]:
                        rank = i + 1
                        break
            cases.append({
                "name": sc["name"], "route_expected": sc["route"], "route_actual": c["route"],
                "metric_expected": sc["metric"], "metric_actual": c["metric"],
                "tool_selection_hit": sel_hit, "steps": c["n_steps"],
                "invalid_steps": n_invalid,
                "truth_key": sc.get("truth_key"), "truth_rank": rank,
                "decision_source": src,
                "top_candidates": [f"{x.get('dim')}·{x.get('key')}"
                                   for x in c["candidates"][:5]],
            })
    finally:
        _storage.insert_trace = _orig_trace
        _audit.append_audit = _orig_audit

    n = len(scenarios)
    truth_cases = [c for c in cases if c.get("truth_key")]
    hypothesis = {}
    for k in (1, 3, 5):
        hit = sum(1 for c in truth_cases if c.get("truth_rank") and c["truth_rank"] <= k)
        hypothesis[str(k)] = round(hit / len(truth_cases), 4) if truth_cases else None

    decision_total = rule_dec + llm_dec + other_dec
    return {
        "suite": "process_metrics",
        "desc": "过程级 Agent 指标（编排效率）：在完整 run_analysis 编排上量工具选择/步数/无效步骤/假设命中/门控节省",
        "offline": True,
        "n_scenarios": n,
        "tool_selection_accuracy": round(tool_ok / n, 4) if n else 1.0,
        "tool_selection_hits": tool_ok,
        "avg_steps": round(tot_steps / n, 3) if n else 0.0,
        "invalid_step_rate": (round((tot_errors + tot_failed) / (tot_steps + tot_errors + tot_failed), 4)
                              if (tot_steps + tot_errors + tot_failed) else 0.0),
        "invalid_steps": {"errored": tot_errors, "failed_product": tot_failed,
                          "total_steps": tot_steps,
                          "note": "无效步骤=未产出可用产物的步骤（硬异常/路由拒绝/失败产物）"},
        "hypothesis_hit_rate": hypothesis,
        "hypothesis_truth_cases": len(truth_cases),
        "gate_saving_rate": (round(rule_dec / decision_total, 4) if decision_total else None),
        "gate_savings": {"decision_total": decision_total, "rule_shortcircuit": rule_dec,
                         "llm_consulted": llm_dec, "other_memory": other_dec,
                         "note": "节省率=由确定性规则/门控直接给出、未消耗 LLM token 的决策占比"},
        "steps_by_agent": steps_by_agent,
        "cases": cases,
    }


def _report(r: Dict) -> None:
    print("\n=== 过程级 Agent 指标（编排效率） ===")
    print(f"工具选择准确率 tool_selection_accuracy = {r['tool_selection_hits']}/{r['n_scenarios']}"
          f" = {r['tool_selection_accuracy']:.1%}")
    print(f"平均步数 avg_steps = {r['avg_steps']}（每场景 agent_step 数）")
    print(f"无效步骤率 invalid_step_rate = {r['invalid_step_rate']:.1%}"
          f"（errored={r['invalid_steps']['errored']} failed={r['invalid_steps']['failed_product']}"
          f" / steps={r['invalid_steps']['total_steps']}）")
    h = r["hypothesis_hit_rate"]
    print(f"假设命中率 hypothesis_hit_rate = "
          + "  ".join(f"k={k}:{v:.1%}" for k, v in h.items() if v is not None)
          + f"（真因场景 {r['hypothesis_truth_cases']} 条）")
    g = r["gate_savings"]
    print(f"门控节省率 gate_saving_rate = {r['gate_saving_rate']:.1%}"
          f"（规则短路 {g['rule_shortcircuit']} / 决策 {g['decision_total']}，"
          f"LLM 咨询 {g['llm_consulted']}）")
    miss = [c for c in r["cases"] if not c["tool_selection_hit"]]
    if miss:
        print(f"工具选择未命中 {len(miss)} 条：")
        for c in miss:
            print(f"  x {c['name'][:22]:24s} 实得({c['route_actual']},{c['metric_actual']}) "
                  f"期望({c['route_expected']},{c['metric_expected']})")
    else:
        print("工具选择全部命中")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    try:
        from sqlpa.config import ensure_utf8_console
        ensure_utf8_console()
    except ImportError:  # pragma: no cover
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report-dir", default=str(ROOT / "evaluation" / "reports"))
    ap.add_argument("--tmp-dir", default=None, help="自建小库的目录（默认平台临时目录）")
    args = ap.parse_args(argv)

    r = evaluate(tmp_dir=Path(args.tmp_dir) if args.tmp_dir else None)
    _report(r)
    out = Path(args.report_dir) / "process.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
