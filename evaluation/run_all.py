"""批量评测入口：一次跑完全部**离线确定性**评测线，汇总成一份总报告。

用法：
    python evaluation/run_all.py
    python evaluation/run_all.py --report-dir evaluation/reports
    python evaluation/run_all.py --llm          # 额外跑"确定性引擎 vs 纯LLM"对照（需 API Key）

评测线（9 条，默认全部离线、无需 API Key、可复现）：
    1. business          业务语义层：口径命中 / 口径外拒绝 / 权限拦截 / PII 掩码 / 公式防篡改 / 审计
    2. product           产品模式（北极星）：语义层命中率 / 指标与维度识别 / 认证率
    3. decisions         归因决策质量（离线规则基线；接真实 LLM 请单独跑 `eval_decisions.py --llm`）
    4. attribution       归因三维定位命中率（注入式真因：analyze / drill / factorize）
    5. alert_quality     告警质量：阈值告警 vs 阈值+统计门控（抑制了多少噪声、多少是月长假象）
    6. sandbox_external  外部对抗语料：危险拦截率 / 只读误杀率 / 库不变性 / 过滤值注入
    7. external_bank     第三方公开基准（Spider dev world_1）：覆盖率 / 覆盖内正确率 / 拒答率
                         —— 优先用仓库内除样本语料跑；只有它跑不起来才跳过，并在总报告里标"未运行"
    8. process           过程级 Agent 指标（编排效率）：工具选择准确率 / 平均步数 / 无效步骤率 /
                         假设命中率@k / 门控节省率 —— 摊开 L4 决策轨迹，量"这个多 Agent 系统跑得怎么样"
    9. planning          L1 受限规划循环 + L2 假设验证环（控制面质量）：有界性 / schema 校验强度 /
                         真因假设命中 / 误否率 / 判官零 LLM —— 证明"控制面智能"不只是叙事

可选第 10 条（`--llm` 才跑，需 LLM_API_KEY）：
    attribution_llm_baseline   同一批注入真因判题上，确定性算术引擎 vs 纯 LLM 的 top@1 对照，
                               并由此得出 llm_value（引擎相对纯 LLM 的增量）。无 Key 时**显式标注未运行**，
                               绝不静默跳过。

产物：
    evaluation/reports/*.json  各线明细 + overall.json（本脚本产出的总报告）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sqlpa.config import repo_relative  # noqa: E402

REPORT_DIR = Path(__file__).resolve().parent / "reports"

# 需要 `--out <file>` 的脚本（其余统一用 `--report-dir <dir>`）
_OUT_STYLE = {"decisions", "attribution_llm_baseline"}


def _run_eval(name: str, report_dir: Path, extra: list | None = None) -> int:
    """运行 evaluation/eval_<name>.py，并返回其退出码。"""
    script = Path(__file__).resolve().parent / f"eval_{name}.py"
    cmd = [sys.executable, str(script)]
    if name in _OUT_STYLE:
        cmd += ["--out", str(report_dir / f"{name}.json")]
    else:
        cmd += ["--report-dir", str(report_dir)]
    cmd += (extra or [])
    print(f"\n>>> 运行 {name} …")
    sys.stdout.flush()
    return subprocess.call(cmd, cwd=str(ROOT))


def _load(report_dir: Path, name: str) -> dict | None:
    """读取某条线的报告；不存在（该线本轮跳过）返回 None，而不是让汇总崩掉。"""
    p = report_dir / f"{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _fmt(v) -> str:
    """报告值可能是百分数 / dict（如 top-k）/ 布尔 / 整数 / 字符串，统一成可读串。"""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, dict):
        return "  ".join(f"{k}={_pct(val)}" for k, val in v.items())
    if isinstance(v, float):
        return _pct(v)
    return str(v)


def _print_suite(title: str, mapping: dict) -> None:
    print("=" * 68)
    print(f"● {title}")
    for k, v in mapping.items():
        print(f"    {k:<26}: {_fmt(v)}")


def _llm_key_present() -> bool:
    # `.env` 是本仓库配置 Key 的默认位置（见 README 快速开始），因此这里也读一次：
    # 否则"Key 明明配在 .env 里"仍会被判成未配置，第 8 条线在本机永远跑不起来。
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv 不可用时退回纯环境变量
        pass
    return bool(os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("OPENAI_API_KEY"))


def main(report_dir: Path, with_llm: bool = False) -> int:
    report_dir.mkdir(parents=True, exist_ok=True)
    rc = 0

    # ---- 9 条离线评测线（顺序执行，后面的线依赖前面的产物）----
    rc |= _run_eval("business", report_dir)
    rc |= _run_eval("product", report_dir)
    rc |= _run_eval("decisions", report_dir)
    rc |= _run_eval("attribution", report_dir, [
        "--periods", "2018-05", "2018-06", "2018-07",
        "--dims", "state", "category", "--inject-pct", "0.60",
        "--topk", "1", "2", "3", "5",
    ])
    rc |= _run_eval("alert_quality", report_dir)
    rc |= _run_eval("sandbox_external", report_dir)

    # 外部公开基准（Spider dev world_1）：优先用**仓库内**样本语料（见 evaluation/spider_probe/），
    # 只有它也不可得时才跳过；跳过与否都写进总报告，不静默。
    external_ok = _run_eval("external_bank", report_dir) == 0

    # 过程级 Agent 指标（编排效率）：在完整 run_analysis 编排上量过程质量，纯离线。
    rc |= _run_eval("process", report_dir)

    # L1 受限规划循环 + L2 假设验证环（控制面质量）：注入式真因小库 + 脚本化 mock LLM，纯离线。
    rc |= _run_eval("planning", report_dir)

    # ---- 可选第 10 条：确定性引擎 vs 纯 LLM 对照（需 API Key）----
    llm_baseline_ran = False
    llm_baseline_note = "未运行（未加 --llm）"
    if with_llm:
        if _llm_key_present():
            llm_baseline_ran = _run_eval("attribution_llm_baseline", report_dir, [
                "--cases", str(report_dir / "attribution.json"),
                "--limit", "30",
            ]) == 0
            llm_baseline_note = "已运行" if llm_baseline_ran else "运行失败（见上方输出）"
        else:
            llm_baseline_note = ("未运行（--llm 已开启，但未检测到 LLM_API_KEY / "
                                 "DEEPSEEK_API_KEY / OPENAI_API_KEY）")
            print(f"\n>>> 跳过 attribution_llm_baseline：{llm_baseline_note}")

    overall = {
        "suite": "overall",
        "desc": ("批量评测总报告（9 条离线确定性评测线 + 可选 LLM 对照；"
                 "离线线无需 API Key、可复现）"),
        "report_dir": repo_relative(report_dir),
        "exit_code_ok": rc == 0,
        "suite_count_offline": 9,
        "suites": {},
    }

    # ---- 1. 业务语义层 ----
    b = _load(report_dir, "business")
    if b:
        overall["suites"]["business"] = {
            "口径内命中率": b["in_scope_hit_rate"],
            "公式防篡改拦截率": b["guard_block_rate"],
            "权限拦截率": b["permission_block_rate"],
            "PII 掩码覆盖率": b["pii_mask_rate"],
            "审计覆盖率": b["audit_coverage"],
        }

    # ---- 2. 产品模式（北极星 = 语义层命中率） ----
    p = _load(report_dir, "product")
    if p:
        overall["suites"]["product"] = {
            "语义层命中率(北极星)": p["semantic_hit_rate"],
            "口径内执行成功率": p["in_scope_ok_rate"],
            "指标识别准确率": p["metric_accuracy"],
            "维度识别准确率": p["dims_accuracy"],
            "口径认证率": p["certified_rate"],
        }

    # ---- 3. 归因决策（离线规则基线） ----
    d = _load(report_dir, "decisions")
    if d:
        overall["suites"]["decisions"] = {
            "决策准确率": d.get("decision_accuracy"),
            "判定题数": d.get("judgment_cases", 0),
        }

    # ---- 4. 归因二维/下钻/因子分解命中率 ----
    a = _load(report_dir, "attribution")
    if a:
        overall["suites"]["attribution"] = {
            "归因定位命中率(analyze)": a["attribution"]["hit_rate"],
            "归因 top-k 命中率": a["attribution"]["topk_hit_rate"],
            "下钻定位命中率(drill)": a["drill"]["hit_rate"],
            "量价因子分解命中率(factorize)": a["factorize"]["hit_rate"],
        }

    # ---- 5. 告警质量（阈值 vs 阈值+统计门控） ----
    aq = _load(report_dir, "alert_quality")
    if aq:
        alerts = aq.get("n_threshold_alerts", 0)
        lb = aq.get("labeled") or {}
        to_m = lb.get("threshold_only") or {}
        gd_m = lb.get("gated") or {}
        overall["suites"]["alert_quality"] = {
            "样本组合数": aq.get("n_cases", 0),
            "阈值告警率(旧行为)": aq.get("alert_rate_threshold_only"),
            "统计门控抑制率": aq.get("suppression_rate"),
            "月长假象告警占比": (round(aq.get("n_calendar_driven_alerts", 0) / alerts, 4)
                                 if alerts else None),
            "构造真值用例数": lb.get("n_cases"),
            "假阳性率_阈值only": to_m.get("false_positive_rate"),
            "假阳性率_统计门控": gd_m.get("false_positive_rate"),
            "召回率_统计门控": gd_m.get("recall"),
        }
        overall["suites"]["alert_quality"]["口径说明"] = aq.get("offline_note", "")

    # ---- 6. 沙箱外部对抗语料 ----
    se = _load(report_dir, "sandbox_external")
    if se:
        fv = se.get("filter_value_injection") or []
        fv_ok = sum(1 for x in fv if x.get("value_is_literal"))
        overall["suites"]["sandbox_external"] = {
            "对抗语料条数": se.get("corpus_size", 0),
            "危险语料拦截率": se.get("dangerous_block_rate"),
            "只读语料误杀率": se.get("readonly_false_positive_rate"),
            "库不变性(文件hash)": bool(se.get("db_unchanged_hash")),
            "库不变性(表行数)": bool(se.get("db_unchanged_counts")),
            "过滤值转义合规率": (round(fv_ok / len(fv), 4) if fv else None),
        }

    # ---- 7. 外部公开基准（Spider dev world_1） ----
    e = _load(report_dir, "external_bank_world_1")
    if e:
        es = e["summary"]
        shape = e["by_gold_shape"].get("单聚合（语义层目标形态）", {})
        overall["suites"]["external_bank_world_1"] = {
            "语料": f"Spider dev world_1, N={es['total']}",
            "覆盖率(开发集口径)": es["coverage_rate"],
            "覆盖内执行正确率": es["accuracy_on_covered"],
            "敢答且答错": es["wrong"],
            "拒答率": es["refusal_rate"],
            "目标形态覆盖率": (round(shape.get("correct", 0) / max(1, shape.get("total", 1)), 4)
                               if shape else None),
        }
    else:
        overall["suites"]["external_bank_world_1"] = {"状态": "未运行（语料不可得）"}

    # ---- 8. 过程级 Agent 指标（编排效率） ----
    pr = _load(report_dir, "process")
    if pr:
        g = pr.get("gate_savings") or {}
        overall["suites"]["process"] = {
            "工具选择准确率": pr.get("tool_selection_accuracy"),
            # 步数是"步"不是百分比：_fmt 会把 float 当百分数，故这里先格式化好再传。
            "平均步数": (f"{pr['avg_steps']:.2f} 步" if pr.get("avg_steps") is not None else None),
            "无效步骤率": pr.get("invalid_step_rate"),
            "假设命中率@k": pr.get("hypothesis_hit_rate"),
            "门控节省率": pr.get("gate_saving_rate"),
            "门控节省(规则短路/决策)": (f"{g.get('rule_shortcircuit')}/{g.get('decision_total')}"
                                       if g else None),
        }

    # ---- 9. L1 受限规划循环 + L2 假设验证环（控制面质量） ----
    pl = _load(report_dir, "planning")
    if pl:
        p1 = pl.get("planning") or {}
        p2 = pl.get("hypothesis") or {}
        overall["suites"]["planning"] = {
            "硬上限步数": p1.get("max_steps"),
            "越界次数": p1.get("bounded_violations"),
            "长脚本被截断": p1.get("cap_enforced"),
            "合法脚本忠实执行率": p1.get("plan_step_accuracy"),
            "非法脚本拦下率": p1.get("adversarial_block_rate"),
            "无 LLM 全剪枝": p1.get("no_llm_prune_ok"),
            "schema 校验准确率": p1.get("validator_accuracy"),
            "真因假设命中率": p2.get("truth_verified_rate"),
            "假设误否率": p2.get("false_reject_rate"),
            "真因排第一": p2.get("truth_ranked_first_rate"),
            "结构不成立假设被否率": p2.get("invalid_rejected_rate"),
            "判官 LLM 调用次数": p2.get("judge_llm_calls"),
        }

    # ---- 10. 可选：确定性引擎 vs 纯 LLM ----
    if llm_baseline_ran:
        lb = _load(report_dir, "attribution_llm_baseline")
        st = lb or {}
        overall["suites"]["attribution_llm_baseline"] = {
            "模型": st.get("model"),
            "判题数": st.get("n", 0),
            "确定性引擎 top@1": st.get("engine_top1"),
            "纯LLM top@1": st.get("llm_top1"),
            "llm_value(引擎增量)": (round((st.get("engine_top1") or 0)
                                          - (st.get("llm_top1") or 0), 4)),
        }
    else:
        overall["suites"]["attribution_llm_baseline"] = {"状态": llm_baseline_note}

    (report_dir / "overall.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 打印汇总 ----
    print("\n\n" + "#" * 68)
    print("# 批量评测总报告（9 条离线线 + 可选 LLM 对照）")
    print("#" * 68)
    titles = {
        "business": "1. 业务语义层（口径/治理护栏）",
        "product": "2. 产品模式（北极星=语义层命中率）",
        "decisions": "3. 归因决策（离线规则基线）",
        "attribution": "4. 归因三维定位命中率（注入式真因，inject 60%）",
        "alert_quality": "5. 告警质量（阈值 vs 阈值+统计门控）",
        "sandbox_external": "6. 沙箱外部对抗语料",
        "external_bank_world_1": "7. 第三方公开基准（Spider dev world_1）",
        "process": "8. 过程级 Agent 指标（编排效率）",
        "planning": "9. 控制面质量（L1 受限规划循环 + L2 假设验证环）",
        "attribution_llm_baseline": "10. 可选对照：确定性引擎 vs 纯 LLM",
    }
    for key, title in titles.items():
        if key in overall["suites"]:
            _print_suite(title, overall["suites"][key])
    print("#" * 68)
    print(f"总报告已保存: {report_dir / 'overall.json'}")
    print(f"退出码: {rc}（{'全部通过' if rc == 0 else '存在失败'}，单线输出见上方）")
    return rc


def _cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report-dir", default=str(REPORT_DIR),
                    help=f"报告输出目录（默认 {REPORT_DIR}）")
    ap.add_argument("--llm", action="store_true",
                    help="额外跑 attribution_llm_baseline 对照（需 LLM_API_KEY；无 Key 会显式标注未运行）")
    args = ap.parse_args()
    return main(Path(args.report_dir), with_llm=args.llm)


if __name__ == "__main__":
    raise SystemExit(_cli())
