"""
sqlpa.business.deliver
======================
交付物层：把一次取数变成**能发出去的东西**。

为什么需要：取数只把表格显示在屏幕上，就还是个 demo——真实业务要的是
能贴到周报里的结论、能转发给同事的报告、能定时盯着阈值的告警。
本模块提供两件事：
  1. export_report / export_csv：把查询结果导出为 Markdown 报告或 CSV，
     并**自动带上口径说明**（指标名/公式/负责人/版本/数据来源/生成时间/SQL）——
     这正是"口径可追溯"在产品上的落地：数字离了口径就是不可信的。
  2. subscriptions：订阅规则（指标 + 阈值 + 收件人），可被定时任务调用检查；
     `check_subscriptions()` 返回需要推送的项，复用归因的异常判定。

边界：不做邮件/IM 实际投递、不做调度器（交给外部 cron/APScheduler），
只提供"生成交付物"与"判定该不该发出"这两件确定性的事。
"""
from __future__ import annotations

import csv
import datetime
import json
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .metric_config import BusinessConfig


# ---------------------------------------------------------------- 导出

def _safe_name(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", str(s))[:60] or "report"


def export_csv(path: str | Path, columns: Sequence[str], rows: Sequence[Sequence]) -> str:
    """导出结果为 CSV（UTF-8 BOM，Excel 打开不乱码）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if columns:
            w.writerow(list(columns))
        for r in rows:
            w.writerow(list(r))
    return str(p)


def build_report(question: str, answer: Dict, *, username: str = "",
                 generated_at: Optional[str] = None) -> str:
    """把一次取数结果渲染成 Markdown 报告（带完整口径说明）。"""
    ts = generated_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    compile_info = answer.get("compile") or {}
    metric_name = answer.get("metric_name") or compile_info.get("metric_name") or answer.get("metric", "-")
    lines: List[str] = []
    lines.append(f"# 取数报告：{question}")
    lines.append("")
    lines.append(f"- 生成时间：{ts}")
    if username:
        lines.append(f"- 取数人：{username}")
    lines.append(f"- 访问路径：**{answer.get('path', '-')}**"
                 + ("（语义层确定性编译，口径已认证）" if answer.get("path") == "semantic"
                    else "（多 Agent 生成，未经口径认证）"))
    lines.append(f"- 指标：{metric_name}（`{answer.get('metric', '-')}`）")
    if answer.get("dims"):
        lines.append(f"- 分组维度：{', '.join(answer['dims'])}")
    # 口径来源：这是报告能不能被信任的关键
    lines.append("")
    lines.append("## 口径说明（为什么这个数可以信）")
    lines.append("")
    lines.append(f"- 计算公式：`{compile_info.get('metric_expr') or answer.get('metric_expr', '-')}`")
    if compile_info.get("owner"):
        lines.append(f"- 口径负责人：{compile_info['owner']}")
    if compile_info.get("version"):
        lines.append(f"- 口径版本：{compile_info['version']}")
    if compile_info.get("sources"):
        lines.append(f"- 数据来源：{', '.join(compile_info['sources'])}")
    derived = compile_info.get("derived")
    if derived:
        kinds = {"ratio": "比率（两指标相除）", "share": "占比（占整体）"}
        lines.append(f"- 派生方式：{kinds.get(derived.get('kind'), derived.get('kind'))}")
    if answer.get("compile", {}).get("filters"):
        lines.append(f"- 过滤条件：{answer['compile']['filters']}")
    # 结果
    cols = answer.get("columns") or []
    rows = answer.get("rows") or []
    lines.append("")
    lines.append(f"## 结果（{len(rows)} 行）")
    lines.append("")
    if cols:
        lines.append("| " + " | ".join(str(c) for c in cols) + " |")
        lines.append("|" + "---|" * len(cols))
        for r in rows[:50]:
            lines.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
        if len(rows) > 50:
            lines.append(f"\n> 仅展示前 50 行，完整数据见导出的 CSV。")
    else:
        lines.append("（无结果）")
    # 归因与治理
    if answer.get("attribution"):
        lines.append("")
        lines.append("## 波动归因")
        lines.append("")
        lines.append(answer.get("attribution_summary") or "")
        for c in (answer["attribution"].get("top_contributors") or [])[:3]:
            lines.append(f"- {c.get('desc')}"
                         + (f"（占波动 {abs(c.get('pct_of_change', 0)) * 100:.0f}%）"
                            if c.get("pct_of_change") else ""))
        if answer.get("drill_suggestion"):
            s = answer["drill_suggestion"]
            lines.append(f"- 可继续下钻：`{s['dim']}={s['value']}`")
    if answer.get("drill"):
        lines.append("")
        lines.append(f"## 下钻分析（{answer['drill'].get('path_desc', '')}）")
        lines.append("")
        for c in (answer["drill"].get("top_contributors") or [])[:3]:
            lines.append(f"- {c.get('desc')}")
    if answer.get("hitl_id"):
        lines.append("")
        lines.append(f"> ⚠️ 本次波动已触发异常告警，已推送负责人复核（工单 {answer['hitl_id']}）。")
    # 可复核性
    lines.append("")
    lines.append("## 复现方式")
    lines.append("")
    lines.append("```sql")
    lines.append(answer.get("sql", "-"))
    lines.append("```")
    return "\n".join(lines)


def export_report(path: str | Path, question: str, answer: Dict, *,
                  username: str = "", also_csv: bool = True) -> Dict:
    """导出 Markdown 报告；(可选) 同时导出 CSV。返回产物路径。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_report(question, answer, username=username), encoding="utf-8")
    out = {"report": str(p)}
    if also_csv and answer.get("columns"):
        csv_path = p.with_suffix(".csv")
        export_csv(csv_path, answer["columns"], answer.get("rows") or [])
        out["csv"] = str(csv_path)
    return out


# ---------------------------------------------------------------- 订阅 / 告警

def _subs_path(path: Optional[str | Path]) -> Path:
    return Path(path) if path else Path("data") / "subscriptions.json"


def load_subscriptions(path: Optional[str | Path] = None) -> List[Dict]:
    p = _subs_path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        return []


def save_subscriptions(subs: List[Dict], path: Optional[str | Path] = None) -> str:
    p = _subs_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def add_subscription(metric: str, threshold_pct: float, owner: str = "",
                     question: str = "", time_spec: str = "本月",
                     dims: Optional[List[str]] = None,
                     path: Optional[str | Path] = None) -> Dict:
    """新增订阅：某指标波动超过阈值就推送给负责人。

    threshold_pct 是"波动百分比阈值"（如 0.05 = 5%），与归因的异常判定同一口径。
    """
    subs = load_subscriptions(path)
    sub = {"id": uuid.uuid4().hex[:8], "metric": metric,
           "threshold_pct": float(threshold_pct), "owner": owner,
           "question": question or metric, "time_spec": time_spec,
           "dims": list(dims or []),
           "create_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    subs.append(sub)
    save_subscriptions(subs, path)
    return sub


def remove_subscription(sub_id: str, path: Optional[str | Path] = None) -> bool:
    subs = load_subscriptions(path)
    left = [s for s in subs if s.get("id") != sub_id]
    if len(left) == len(subs):
        return False
    save_subscriptions(left, path)
    return True


def check_subscriptions(cfg: BusinessConfig, db, *, path: Optional[str | Path] = None,
                        llm=None) -> List[Dict]:
    """逐条检查订阅，返回**需要推送**的项（波动超阈值）。

    复用 attribution.analyze 的异常判定，保证"订阅告警"与"人工归因"同一套口径，
    不会出现两处阈值算法不一致。
    """
    from . import attribution

    alerts: List[Dict] = []
    for s in load_subscriptions(path):
        metric = s.get("metric")
        if metric not in cfg.metrics:
            alerts.append({**s, "status": "skipped", "reason": f"未知指标 {metric}"})
            continue
        try:
            r = attribution.analyze(cfg, db, metric,
                                    current_spec=s.get("time_spec", "本月"),
                                    dims=s.get("dims") or None,
                                    threshold_pct=float(s.get("threshold_pct", 0.05)))
        except Exception as e:  # noqa: BLE001
            alerts.append({**s, "status": "error", "reason": str(e)[:120]})
            continue
        if r.get("ok") and r.get("is_abnormal"):
            alerts.append({**s, "status": "alert",
                           "change_pct": r.get("change_pct"),
                           "change": r.get("change"),
                           "metric_name": r.get("metric_name"),
                           "summary": attribution.summarize(r, llm)})
        else:
            alerts.append({**s, "status": "ok", "reason": r.get("reason", "波动未超阈值")})
    return alerts
