#!/usr/bin/env python
"""告警质量评测（离线、确定性、零 token）：阈值告警 vs 阈值+统计门控。

回答的问题：**"5% 阈值到底报了多少不该报的警？"**（此前完全没有度量）

做法：在真实 Olist 上遍历「可加指标 × 相邻月对」，对每个组合分别计算
  1. `threshold_alert`：|change_pct| >= 阈值（旧行为，唯一判据）
  2. `significance`  ：日粒度均值的 Welch z 检验（p < 0.05）
  3. `calendar`      ：月份天数差异可解释的比例（|explained| >= 0.5 即"主要是月长造成的"）
然后报告：告警总量、被统计门控抑制的比例、其中属于月长假象的比例。

⚠️ 诚实的口径：这里**没有人工标注的真值**，因此报的是"告警量的下降与成因结构"，
不是"精确的假阳性率"。要得到真正的 FP/FN 需要独立标注集（属后续工作）。

跑法：python evaluation/eval_alert_quality.py [--months 24] [--threshold 0.05]
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.business.attribution import analyze          # noqa: E402
from sqlpa.business.metric_config import load_config    # noqa: E402
from sqlpa.config import ensure_utf8_console, repo_relative    # noqa: E402

# 只跑**可加指标**：显著性检验基于"日值之和 ≈ 期间总值"，比率类不满足该前提
ADDITIVE_METRICS = ("gmv", "order_count", "paid_order_count", "item_count")


def _month_pairs(start: str, n: int):
    y, m = int(start[:4]), int(start[5:7])
    out = []
    for _ in range(n):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((f"{ny:04d}-{nm:02d}", f"{y:04d}-{m:02d}"))
        y, m = ny, nm
    return out


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="业务库（默认：真实全量库 > 仓库自带样本库）")
    ap.add_argument("--start", default="2017-01", help="起始月（该月作为上期）")
    ap.add_argument("--months", type=int, default=18)
    ap.add_argument("--threshold", type=float, default=0.05)
    ap.add_argument("--report-dir", default=str(ROOT / "evaluation" / "reports"))
    args = ap.parse_args()

    from sqlpa.config import db_kind_note, resolve_db_path
    try:
        args.db, db_kind = resolve_db_path(args.db)
    except FileNotFoundError as e:
        print(f"[SKIP] {e}")
        return 0
    if db_kind_note(db_kind):
        print(db_kind_note(db_kind))
        if db_kind == "sample":
            print("   （README 中的 56 个阈值告警 / 抑制 32 个，是全量库上的实测值；"
                  "样本库上告警率与抑制率都会不同。）")

    import sqlite3
    cfg = load_config()
    pairs = _month_pairs(args.start, args.months)

    rows = []
    for metric in ADDITIVE_METRICS:
        if metric not in cfg.metrics:
            continue
        for cur, prev in pairs:
            try:
                r = analyze(cfg, sqlite3.connect(args.db), metric, current_spec=cur,
                            previous_spec=prev, dims=["state"], threshold_pct=args.threshold)
            except Exception as e:  # noqa: BLE001
                rows.append({"metric": metric, "current": cur, "previous": prev,
                             "error": f"{type(e).__name__}: {e}"})
                continue
            if not r.get("ok"):
                continue
            sig = r.get("significance") or {}
            cal = r.get("calendar") or {}
            rows.append({
                "metric": metric, "current": cur, "previous": prev,
                "change_pct": r.get("change_pct"),
                "threshold_alert": bool(r.get("is_abnormal")),
                "significant": sig.get("is_significant"),
                "p_value": sig.get("p_value"), "z": sig.get("z"),
                "calendar_explained": cal.get("explained_by_calendar"),
                "per_day_change_pct": cal.get("per_day_change_pct"),
            })

    valid = [r for r in rows if "error" not in r]
    alerts = [r for r in valid if r["threshold_alert"]]
    tested = [r for r in alerts if r.get("significant") is not None]
    suppressed = [r for r in tested if r["significant"] is False]
    calendar_driven = [r for r in alerts
                       if r.get("calendar_explained") is not None
                       and abs(r["calendar_explained"]) >= 0.5]
    confirmed = [r for r in tested if r["significant"] is True]

    report = {
        "db": repo_relative(args.db), "db_kind": db_kind, "threshold": args.threshold, "months": len(pairs),
        "n_cases": len(valid),
        "n_threshold_alerts": len(alerts),
        "n_significance_tested": len(tested),
        "n_suppressed_by_significance": len(suppressed),
        "n_confirmed_by_significance": len(confirmed),
        "n_calendar_driven_alerts": len(calendar_driven),
        "alert_rate_threshold_only": round(len(alerts) / len(valid), 4) if valid else 0.0,
        "suppression_rate": round(len(suppressed) / len(tested), 4) if tested else 0.0,
        "offline_note": ("本评测无可信真值标注，报的是**告警量的下降与成因结构**，"
                         "不是精确假阳性率；真 FP/FN 需独立标注集。"),
        "cases": rows,
    }
    out_dir = Path(args.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "alert_quality.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 70)
    print("告警质量：阈值告警 vs 阈值+统计门控（真实 Olist，相邻月对）")
    print("=" * 70)
    print(f"  组合数                    : {len(valid)}")
    print(f"  阈值告警数（旧行为）      : {len(alerts)}  "
          f"（告警率 {report['alert_rate_threshold_only']:.1%}）")
    print(f"  其中做了显著性检验        : {len(tested)}")
    print(f"  ★ 被统计门控抑制（噪声）  : {len(suppressed)}  "
          f"（占已检验告警 {report['suppression_rate']:.1%}）")
    print(f"  通过显著性检验（确认波动）: {len(confirmed)}")
    print(f"  月长假象告警（|explained|>=0.5）: {len(calendar_driven)}")
    print()
    print("逐条（仅列阈值告警）：")
    for r in alerts:
        sig = r.get("significant")
        flag = "抑制" if sig is False else ("确认" if sig else "未检验")
        print(f"  {r['metric']:18s} {r['previous']}→{r['current']} "
              f"波动={r['change_pct'] * 100:+6.1f}%  日均={((r.get('per_day_change_pct') or 0)) * 100:+6.1f}% "
              f"p={r.get('p_value')}  → {flag}")
    print()
    print(f"产物 -> {out_dir / 'alert_quality.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
