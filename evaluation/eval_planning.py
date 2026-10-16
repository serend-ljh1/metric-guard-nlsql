"""L1 / L2 控制面评测线 —— 「规划循环与假设验证环到底值多少分」。

背景：L5（`eval_process.py`）量的是**基础编排**（工具选择/步数/门控节省），
它盖不住本轮新加的两种控制面能力：

  - **L1 受限规划循环**（`src/sqlpa/analysis/operators.py`）：首步沿用既有 `decide_drill`，
    **续步**才由规划器在"算子登记表"里推进（挑算子 → schema 校验 → 执行 → 观察 → 再决）。
    要证明：① 绝不越界（硬上限）；② 非法步骤真的被拦；③ 无 LLM 时确实剪枝（逐字回落）。
  - **L2 假设验证环**（`src/sqlpa/analysis/hypotheses.py`）：LLM 提假设 → **确定性判官**裁决。
    要证明：① 真因假设被 verified 而不是被误杀；② **结构不成立**的假设确实被否；③ 判官零 LLM；
    ④ 证据排序把真因排在第一。

做法（全部离线、确定性、可复现；注入式真因小库 + 脚本化 mock LLM，无需 API Key）：
  A. **规划循环电池**：给一段"首步已执行"的 history，让脚本化规划器推进**续步**，跑
     `operators.run_plan`，量 `bounded_violations`（越界次数，期望 0）、`cap_enforced`（长脚本被截到上限）、
     `plan_step_accuracy`（合法脚本被忠实执行）、`adversarial_block_rate`（非法脚本被拦）、
     `no_llm_prune_ok`（无 LLM 全剪枝）、`validator_accuracy`（`validate_args` 正反例）。
  B. **假设验证电池**：对真因库做 analyze，脚本化 LLM 提「真因 + 结构不成立 + 干扰」假设，
     跑 `hypotheses.run_hypothesis_loop`，量 `truth_verified_rate`、`false_reject_rate`、
     `invalid_rejected_rate`、`truth_ranked_first_rate`、`judge_llm_calls`。

> ⚠️ **口径边界**：判官判的是"**这条假设结构上成不成立**"（取值存在 / 占比可加 / 因子在分解里），
> 不是"它是不是主因"——所以"结构成立的干扰项"**不该**被否，主因由**证据排序**（|delta| / |share|）分出高下。

用法：
    python evaluation/eval_planning.py
    python evaluation/eval_planning.py --report-dir evaluation/reports
"""
from __future__ import annotations

import json
import sqlite3
import sys as _sys
from pathlib import Path
from typing import Dict, List, Optional

try:
    from evaluation.eval_process import _build_truth_db, _month_anchors
    from sqlpa.analysis import hypotheses, operators
    from sqlpa.analysis.operators import OpContext
    from sqlpa.business import attribution
    from sqlpa.business.metric_config import load_config
except ImportError:  # pragma: no cover - 独立运行兜底
    _sys.path.insert(0, str((Path(__file__).resolve().parents[1] / "src").resolve()))
    _sys.path.insert(0, str((Path(__file__).resolve().parents[1]).resolve()))
    from evaluation.eval_process import _build_truth_db, _month_anchors
    from sqlpa.analysis import hypotheses, operators
    from sqlpa.analysis.operators import OpContext
    from sqlpa.business import attribution
    from sqlpa.business.metric_config import load_config

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 脚本化 mock LLM

class _ScriptLLM:
    """脚本化 mock LLM：按**提示类型**返回预设响应，让规划/假设环可确定性复现。

    - 规划器提示（含"规划器"）→ 依次吐出 `plan_script` 里的算子（可含非法项，用于对抗）；
    - 假设提示（含"JSON 数组"）→ 吐出 `hypo_script`；
    - 其它 JSON 提示（decide_drill / 意图识别）→ 返回一个合法 action。

    `calls` 用于证明**判官零 LLM**：假设环里只应发生 1 次调用（提假设那一次）。
    """

    def __init__(self, plan_script: Optional[List[Dict]] = None,
                 hypo_script: Optional[List[Dict]] = None):
        self.plan_script = list(plan_script or [])
        self.hypo_script = list(hypo_script or [])
        self.calls = 0
        self._plan_i = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if "规划器" in prompt:
            if self._plan_i < len(self.plan_script):
                item = self.plan_script[self._plan_i]
                self._plan_i += 1
                return json.dumps(item, ensure_ascii=False)
            return json.dumps({"op": "stop"}, ensure_ascii=False)
        if "JSON 数组" in prompt:
            return json.dumps(self.hypo_script, ensure_ascii=False)
        return json.dumps({"action": "none", "reason": "script"}, ensure_ascii=False)


# ---------------------------------------------------------------- 电池 A：规划循环

# kind=valid → 续步脚本应被忠实执行（executed ops == script 前缀）
# kind=adversarial → 非法脚本应被**拦下**（executed 为空）
# cap=True → 脚本超过硬上限，应被截到 PLAN_MAX_STEPS
PLAN_TASKS: List[Dict] = [
    {"name": "续步下钻真因州", "kind": "valid",
     "script": [{"op": "drill", "args": {"path": [{"dim": "state", "value": "SP"}]},
                 "reason": "真因集中在 SP"}]},
    {"name": "续步下钻 + 因子分解", "kind": "valid",
     "script": [{"op": "drill", "args": {"path": [{"dim": "state", "value": "SP"}]},
                 "reason": "先定位州"},
                {"op": "factorize", "reason": "再看量价"}]},
    {"name": "续步统计门控复核", "kind": "valid",
     "script": [{"op": "check_significance", "reason": "复核显著性"}]},
    {"name": "长脚本应被截到硬上限", "kind": "valid", "cap": True,
     "script": [{"op": "drill", "args": {"path": [{"dim": "state", "value": "SP"}]}, "reason": "1"},
                {"op": "drill", "args": {"path": [{"dim": "state", "value": "RJ"}]}, "reason": "2"},
                {"op": "drill", "args": {"path": [{"dim": "state", "value": "MG"}]}, "reason": "3"},
                {"op": "factorize", "reason": "4"},
                {"op": "check_significance", "reason": "5"},
                {"op": "list_dims", "reason": "6"}]},
    {"name": "非法算子 drop_table", "kind": "adversarial",
     "script": [{"op": "drop_table", "reason": "越权"}]},
    {"name": "越界维度", "kind": "adversarial",
     "script": [{"op": "drill", "args": {"path": [{"dim": "not_a_dim", "value": "x"}]},
                 "reason": "乱指维度"}]},
    {"name": "坏参数（空 path）", "kind": "adversarial",
     "script": [{"op": "drill", "args": {"path": []}, "reason": "空路径"}]},
    {"name": "越界维度 analyze", "kind": "adversarial",
     "script": [{"op": "analyze", "args": {"dims": ["not_a_dim"]}, "reason": "越界"}]},
]

# validate_args 正反例（直接测 schema 校验强度）
VALIDATOR_CASES: List = [
    ("drop_table", {}, False),
    ("drill", {"path": []}, False),
    ("drill", {"path": [{"dim": "not_a_dim", "value": "x"}]}, False),
    ("drill", {"path": [{"dim": "state", "value": ""}]}, False),
    ("analyze", {"dims": ["not_a_dim"]}, False),
    ("analyze", {"dims": "state"}, False),
    ("factorize", {"多余参数": 1}, False),
    ("factorize", {}, True),
    ("drill", {"path": [{"dim": "state", "value": "SP"}]}, True),
    ("analyze", {"dims": ["state"]}, True),
    ("list_dims", None, True),
]


def _expected_ops(tk: Dict) -> List[str]:
    """合法脚本应被执行到的算子序列（长脚本截到硬上限）。"""
    return [s["op"] for s in tk["script"]][:operators.PLAN_MAX_STEPS]


def _planning_battery(ctx: OpContext, history0: List[Dict]) -> Dict:
    cases: List[Dict] = []
    bounded_violations = 0
    valid_matched = valid_expected = 0
    adv_blocked = adv_total = 0
    prune_ok = True
    cap_ok = True

    for tk in PLAN_TASKS:
        # 用**独立副本**的 history，避免任务间互相污染
        executed = operators.run_plan(ctx, [dict(h) for h in history0],
                                      question="本月 GMV 为什么跌",
                                      llm=_ScriptLLM(plan_script=tk["script"]))
        ops = [e["plan"].op for e in executed]

        if len(executed) > operators.PLAN_MAX_STEPS:
            bounded_violations += 1
        if tk.get("cap") and len(executed) != operators.PLAN_MAX_STEPS:
            cap_ok = False

        # 无 LLM：应完全剪枝（回退保证）
        if operators.run_plan(ctx, [dict(h) for h in history0],
                              question="本月 GMV 为什么跌", llm=None):
            prune_ok = False

        if tk["kind"] == "valid":
            exp = _expected_ops(tk)
            valid_expected += len(exp)
            valid_matched += sum(1 for a, b in zip(ops, exp) if a == b)
        else:
            adv_total += 1
            adv_blocked += int(len(executed) == 0)

        cases.append({"name": tk["name"], "kind": tk["kind"],
                      "script_ops": [s.get("op") for s in tk["script"]],
                      "executed_ops": ops,
                      "expected_ops": _expected_ops(tk) if tk["kind"] == "valid" else [],
                      "blocked": (len(executed) == 0) if tk["kind"] == "adversarial" else None})

    v_ok = 0
    for name, args, expect in VALIDATOR_CASES:
        ok, _n, _e = operators.validate_args(name, args, ctx)
        v_ok += int(bool(ok) == expect)

    return {
        "n_tasks": len(PLAN_TASKS),
        "bounded_violations": bounded_violations,
        "max_steps": operators.PLAN_MAX_STEPS,
        "cap_enforced": cap_ok,
        "plan_step_accuracy": round(valid_matched / valid_expected, 4) if valid_expected else None,
        "plan_steps_matched": valid_matched,
        "plan_steps_expected": valid_expected,
        "adversarial_block_rate": round(adv_blocked / adv_total, 4) if adv_total else None,
        "adversarial_blocked": adv_blocked,
        "adversarial_total": adv_total,
        "no_llm_prune_ok": prune_ok,
        "validator_accuracy": round(v_ok / len(VALIDATOR_CASES), 4),
        "validator_hits": v_ok,
        "validator_total": len(VALIDATOR_CASES),
        "cases": cases,
    }


# ---------------------------------------------------------------- 电池 B：假设验证环

def _hyp_key(h: Dict) -> str:
    return json.dumps({k: h.get(k) for k in ("kind", "dim", "value", "factor")},
                      ensure_ascii=False, sort_keys=True)


HYP_CASES: List[Dict] = [
    {"name": "真因SP + 不存在值 + 越界维度",
     "hypos": [{"kind": "dim_value", "dim": "state", "value": "SP", "claim": "真因 SP"},
               {"kind": "dim_value", "dim": "state", "value": "ZZ", "claim": "不存在的州"},
               {"kind": "dim_value", "dim": "not_a_dim", "value": "x", "claim": "越界维度"}],
     "truth": {"kind": "dim_value", "dim": "state", "value": "SP"},
     "invalid": [{"kind": "dim_value", "dim": "state", "value": "ZZ"},
                 {"kind": "dim_value", "dim": "not_a_dim", "value": "x"}]},
    {"name": "真因SP + 未知因子",
     "hypos": [{"kind": "dim_value", "dim": "state", "value": "SP", "claim": "真因 SP"},
               {"kind": "factor", "factor": "运费", "claim": "运费变了"}],
     "truth": {"kind": "dim_value", "dim": "state", "value": "SP"},
     "invalid": [{"kind": "factor", "factor": "运费"}]},
    {"name": "真因SP + 结构成立的干扰RJ/MG",
     "hypos": [{"kind": "dim_value", "dim": "state", "value": "SP", "claim": "真因 SP"},
               {"kind": "dim_value", "dim": "state", "value": "RJ", "claim": "干扰 RJ"},
               {"kind": "dim_value", "dim": "state", "value": "MG", "claim": "干扰 MG"}],
     "truth": {"kind": "dim_value", "dim": "state", "value": "SP"},
     "invalid": []},
]


def _hypothesis_battery(ctx: OpContext, att: Dict, factor_split: Dict) -> Dict:
    cases: List[Dict] = []
    truth_verified = false_reject = truth_first = 0
    inv_rejected = inv_total = 0
    judge_llm_calls = 0

    for hc in HYP_CASES:
        llm = _ScriptLLM(hypo_script=hc["hypos"])
        res = hypotheses.run_hypothesis_loop(ctx, att, llm, factor_split=factor_split)
        judge_llm_calls += max(0, llm.calls - 1)   # 只有"提假设"那次应调 LLM

        vkeys = [_hyp_key(r["hypothesis"]) for r in res["verified"]]
        rkeys = {_hyp_key(r["hypothesis"]) for r in res["rejected"]}
        tkey = _hyp_key(hc["truth"])
        if tkey in vkeys:
            truth_verified += 1
        if tkey in rkeys:
            false_reject += 1
        if vkeys and vkeys[0] == tkey:
            truth_first += 1
        for iv in hc["invalid"]:
            inv_total += 1
            inv_rejected += int(_hyp_key(iv) in rkeys)

        cases.append({"name": hc["name"], "n": res["n"],
                      "verified": [hypotheses._claim(r["hypothesis"]) for r in res["verified"]],
                      "rejected": [{"claim": hypotheses._claim(r["hypothesis"]),
                                    "reason": r.get("reason", "")} for r in res["rejected"]],
                      "truth_verified": tkey in vkeys,
                      "truth_first": bool(vkeys and vkeys[0] == tkey)})

    n = len(HYP_CASES)
    return {
        "n_cases": n,
        "truth_verified_rate": round(truth_verified / n, 4) if n else None,
        "false_reject_rate": round(false_reject / n, 4) if n else None,
        "truth_ranked_first_rate": round(truth_first / n, 4) if n else None,
        "invalid_rejected_rate": (round(inv_rejected / inv_total, 4) if inv_total else None),
        "invalid_rejected": inv_rejected,
        "invalid_total": inv_total,
        "judge_llm_calls": judge_llm_calls,
        "cases": cases,
    }


# ---------------------------------------------------------------- 汇总

def evaluate(tmp_dir: Optional[Path] = None) -> Dict:
    import tempfile
    cfg = load_config()
    tmp = Path(tmp_dir) if tmp_dir else (Path(tempfile.gettempdir()) / "metric_guard_eval")
    tmp.mkdir(parents=True, exist_ok=True)
    db_path = _build_truth_db(tmp)
    cur_month = _month_anchors()["current"][:7]

    con = sqlite3.connect(db_path)
    ctx = OpContext(cfg=cfg, db=con, metric="gmv", time_spec=cur_month,
                    threshold_pct=0.05, dims=["state", "category"])
    att = attribution.analyze(cfg, con, "gmv", current_spec=cur_month,
                              dims=["state", "category"], threshold_pct=0.05)
    # 首步已由既有规则执行（analyze + 一次 drill）——L1 规划循环量的是**续步**
    first_drill = attribution.drill(cfg, con, "gmv", current_spec=cur_month,
                                    path=[{"dim": "state", "value": "SP"}])
    try:
        factor_split = attribution.factorize(cfg, con, "gmv", current_spec=cur_month)
    except Exception:  # noqa: BLE001
        factor_split = {"ok": False}
    history0 = [{"op": "analyze", "result": att}, {"op": "drill", "result": first_drill}]

    planning = _planning_battery(ctx, history0)
    hyp = _hypothesis_battery(ctx, att, factor_split)
    con.close()

    return {
        "suite": "planning_hypothesis",
        "desc": ("L1 受限规划循环 + L2 假设验证环 的过程质量：有界性/校验强度/"
                 "假设真值命中/误否率（注入式真因 + 脚本化 mock LLM）"),
        "offline": True,
        "n_plan_tasks": planning["n_tasks"],
        "n_hypothesis_cases": hyp["n_cases"],
        "planning": planning,
        "hypothesis": hyp,
    }


def _report(r: Dict) -> None:
    p, h = r["planning"], r["hypothesis"]
    print("\n=== L1 受限规划循环 + L2 假设验证环（控制面） ===")
    print(f"规划任务 {p['n_tasks']} 个 / 假设用例 {h['n_cases']} 条（注入式真因、离线）")
    print(f"【L1】越界次数 bounded_violations = {p['bounded_violations']}"
          f"（硬上限 PLAN_MAX_STEPS={p['max_steps']}，长脚本截断 {p['cap_enforced']}）")
    print(f"【L1】合法脚本忠实执行率 plan_step_accuracy = "
          f"{p['plan_steps_matched']}/{p['plan_steps_expected']} = {p['plan_step_accuracy']:.1%}")
    print(f"【L1】非法脚本拦下率 adversarial_block_rate = "
          f"{p['adversarial_blocked']}/{p['adversarial_total']} = {p['adversarial_block_rate']:.1%}")
    print(f"【L1】schema 校验准确率 validator_accuracy = "
          f"{p['validator_hits']}/{p['validator_total']} = {p['validator_accuracy']:.1%}")
    print(f"【L1】无 LLM 全剪枝 no_llm_prune_ok = {p['no_llm_prune_ok']}")
    print(f"【L2】真因假设被 verified = {h['truth_verified_rate']:.1%}"
          f"；误否率 false_reject_rate = {h['false_reject_rate']:.1%}"
          f"；真因排第一 = {h['truth_ranked_first_rate']:.1%}")
    print(f"【L2】结构不成立的假设被否 = "
          f"{h['invalid_rejected']}/{h['invalid_total']} = {h['invalid_rejected_rate']:.1%}")
    print(f"【L2】判官 LLM 调用次数 judge_llm_calls = {h['judge_llm_calls']}（应为 0）")


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
    out = Path(args.report_dir) / "planning.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
