#!/usr/bin/env python
"""告警质量评测（离线、确定性、零 token）：阈值告警 vs 阈值+统计门控。

本脚本分**两段**，回答两个不同的问题：

  A. 结构段（真实数据、无真值）：「5% 阈值到底报了多少警、其中多少能被统计门控抑制、
     多少是月长假象？」——报的是**告警量的下降与成因结构**，不是精确假阳性率。
  B. 标注段（注入式构造真值）：**真正度量假阳性率 / 假阴性率**。
     在真实 Olist 的副本上按构造注入，使每个组合的"是否真异常"由注入动作**定义**（非人标）：
       - 真异常（truth=abnormal）：把当期整期价格打 factor 折（默认 0.6 / 1.4），
         日均随之改变 → 判定应告警；
       - 真正常·日历假象（truth=normal）：把当期整期缩放，使**日均值与上期持平**，
         于是总值的唯一变化只剩"月长差"（change_pct 恒等于 天数比−1）→ 判定**不应**告警；
       - 真正常·平稳（truth=normal）：把当期缩放使**总值与上期持平**（change≈0）→ 不应告警。
     两套系统（阈值-only / 阈值+统计门控）各算一遍混淆矩阵，得到真实的
     recall / 假阳性率 / precision，回答"统计门控到底消掉了多少假阳、代价是多少假阴"。

跑法：python evaluation/eval_alert_quality.py [--months 18] [--threshold 0.05]
"""
from __future__ import annotations

import argparse
import calendar
import json
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.business.attribution import analyze          # noqa: E402
from sqlpa.business.metric_config import load_config    # noqa: E402
from sqlpa.config import ensure_utf8_console, repo_relative    # noqa: E402

# 只跑**可加指标**：显著性检验基于"日值之和 ≈ 期间总值"，比率类不满足该前提
ADDITIVE_METRICS = ("gmv", "order_count", "paid_order_count", "item_count")

_TMP = ROOT / "._test_tmp"

# ---------------------------------------------------------------- 标注段真值用例
# klass: abnormal（应告警）/ calendar（真正常·日历假象）/ stable（真正常·平稳）
# op   : 构造算子——必须与**该指标的口径**对齐，否则"注入"推不动被度量的量：
#        - price        : 缩放当期 order_items.price → 推动金额类（gmv = SUM(oi.price)）
#        - cancel       : 把当期一部分订单标记 canceled → 推动计数类
#                         （order_count 口径=delivered、paid_order_count/item_count 口径=非取消）
#        - price_calendar / price_stable : 金额类专用（缩放使日均/总值持平）
# 日历用例选"月长差 ≥ 阈值"的月对（2 月↔1/3 月：31/28≈+10.7%、28/31≈−9.7%）：
# 阈值-only 一定会报（暴露假阳），而日均持平 → 统计门控应抑制。
LABELED_CASES = [
    # ---- 真异常·金额（缩放价格：日均显著变化）----
    {"metric": "gmv", "prev": "2018-05", "cur": "2018-06", "klass": "abnormal",
     "op": "price", "factor": 0.6},
    {"metric": "gmv", "prev": "2018-06", "cur": "2018-07", "klass": "abnormal",
     "op": "price", "factor": 0.6},
    {"metric": "gmv", "prev": "2018-07", "cur": "2018-08", "klass": "abnormal",
     "op": "price", "factor": 1.4},
    {"metric": "gmv", "prev": "2017-08", "cur": "2017-09", "klass": "abnormal",
     "op": "price", "factor": 0.6},
    # ---- 真异常·订单类（取消一部分当期订单 → 计数下降）----
    {"metric": "order_count", "prev": "2018-05", "cur": "2018-06", "klass": "abnormal",
     "op": "cancel", "frac": 0.4},
    {"metric": "order_count", "prev": "2018-06", "cur": "2018-07", "klass": "abnormal",
     "op": "cancel", "frac": 0.4},
    {"metric": "item_count", "prev": "2018-06", "cur": "2018-07", "klass": "abnormal",
     "op": "cancel", "frac": 0.4},
    {"metric": "paid_order_count", "prev": "2018-06", "cur": "2018-07", "klass": "abnormal",
     "op": "cancel", "frac": 0.4},
    # ---- 真正常·日历假象：缩放当期使日均持平（总值只剩月长差）— 金额类专用 ----
    {"metric": "gmv", "prev": "2017-02", "cur": "2017-03", "klass": "calendar", "op": "price_calendar"},
    {"metric": "gmv", "prev": "2018-02", "cur": "2018-03", "klass": "calendar", "op": "price_calendar"},
    {"metric": "gmv", "prev": "2017-01", "cur": "2017-02", "klass": "calendar", "op": "price_calendar"},
    {"metric": "gmv", "prev": "2018-01", "cur": "2018-02", "klass": "calendar", "op": "price_calendar"},
    # ---- 真正常·平稳：缩放当期使总值与上期持平（change≈0）— 金额类专用 ----
    {"metric": "gmv", "prev": "2018-05", "cur": "2018-06", "klass": "stable", "op": "price_stable"},
    {"metric": "gmv", "prev": "2018-06", "cur": "2018-07", "klass": "stable", "op": "price_stable"},
]


def _month_pairs(start: str, n: int):
    y, m = int(start[:4]), int(start[5:7])
    out = []
    for _ in range(n):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((f"{ny:04d}-{nm:02d}", f"{y:04d}-{m:02d}"))
        y, m = ny, nm
    return out


def _days_in_month(ym: str) -> int:
    return calendar.monthrange(int(ym[:4]), int(ym[5:7]))[1]


def _unq(s: str) -> str:
    """去掉 _range_spec 返回字面量外层的单引号，便于当作绑定参数使用。"""
    return s.strip("'")


def _period_total(conn: sqlite3.Connection, start: str, end: str) -> float:
    r = conn.execute(
        "SELECT SUM(oi.price) FROM order_items oi JOIN orders o ON oi.order_id=o.order_id "
        "WHERE o.order_status!='canceled' AND o.order_purchase_timestamp>=? "
        "AND o.order_purchase_timestamp<?", (start, end)).fetchone()
    return float(r[0] or 0.0)


def _scale_period(conn: sqlite3.Connection, start: str, end: str, factor: float) -> None:
    """把当期整期（非取消订单的 order_items）价格整体乘 factor。

    只改 ts∈[start,end) 且非取消的行；用相关子查询定位，保证不误伤其他期。
    """
    conn.execute(
        "UPDATE order_items SET price = price * ? WHERE EXISTS ("
        "SELECT 1 FROM orders o WHERE o.order_id=order_items.order_id "
        "AND o.order_status!='canceled' AND o.order_purchase_timestamp>=? "
        "AND o.order_purchase_timestamp<?)", (factor, start, end))


def _cancel_fraction(conn: sqlite3.Connection, start: str, end: str, frac: float) -> None:
    """把当期（非取消）订单里约 frac 比例标记为 canceled（按 rowid 取模，确定性）。

    用于推动**计数类**指标：order_count 口径是 delivered、paid_order_count/item_count 口径是「非取消」，
    标记 canceled 会让它们都从对应口径里掉出来 → 计数下降。
    """
    k = max(1, min(9, round(frac * 10)))
    conn.execute(
        "UPDATE orders SET order_status='canceled' WHERE order_purchase_timestamp>=? "
        "AND order_purchase_timestamp<? AND order_status!='canceled' AND (rowid % 10) < ?",
        (start, end, k))


def _run_labeled_case(cfg, real_db: str, case: dict, threshold: float) -> dict:
    """单题：拷贝真实库→按构造注入→跑归因→记录两套系统是否告警（真值由构造定义）。"""
    from sqlpa.business.attribution import _range_spec
    prev, cur = case["prev"], case["cur"]
    p0, p1 = _range_spec(prev)
    c0, c1 = _range_spec(cur)
    # _range_spec 返回**带引号**的字面量；作为绑定参数前先去引号（否则会连同引号一起比较）
    p0, p1, c0, c1 = _unq(p0), _unq(p1), _unq(c0), _unq(c1)

    _TMP.mkdir(parents=True, exist_ok=True)
    work = _TMP / f"alert-inj-{uuid.uuid4().hex[:10]}.db"
    shutil.copyfile(real_db, work)
    conn = sqlite3.connect(work)
    try:
        op = case["op"]
        if op == "price":
            factor = float(case["factor"])
            _scale_period(conn, c0, c1, factor)
            basis = f"整期价格×{factor}"
        elif op == "cancel":
            frac = float(case["frac"])
            _cancel_fraction(conn, c0, c1, frac)
            basis = f"当期订单取消 {frac:.0%}"
        elif op == "price_calendar":
            # 日均持平 → 总值只剩"月长差"（金额类专用）
            pt, ct = _period_total(conn, p0, p1), _period_total(conn, c0, c1)
            pd, cd = _days_in_month(prev), _days_in_month(cur)
            factor = (pt / pd) / (ct / cd) if ct else 1.0
            _scale_period(conn, c0, c1, factor)
            basis = f"整期价格×{factor:.4f}（日均持平，仅月长差）"
        else:  # price_stable：总值持平（金额类专用）
            pt, ct = _period_total(conn, p0, p1), _period_total(conn, c0, c1)
            factor = pt / ct if ct else 1.0
            _scale_period(conn, c0, c1, factor)
            basis = f"整期价格×{factor:.4f}（总值持平）"
        conn.commit()
    finally:
        conn.close()

    db = sqlite3.connect(work)
    try:
        r = analyze(cfg, db, case["metric"], current_spec=cur, previous_spec=prev,
                    dims=["state"], threshold_pct=threshold)
    finally:
        db.close()
        work.unlink(missing_ok=True)

    sig = (r.get("significance") or {})
    threshold_alert = bool(r.get("is_abnormal"))
    significant = sig.get("is_significant")
    return {
        "metric": case["metric"], "prev": prev, "cur": cur, "klass": case["klass"],
        "truth_abnormal": case["klass"] == "abnormal",
        "basis": basis,
        "change_pct": r.get("change_pct"),
        "threshold_alert": threshold_alert,                       # 系统 A：仅阈值
        "gated_alert": bool(threshold_alert and significant is True),  # 系统 B：阈值+门控
        "significant": significant, "p_value": sig.get("p_value"),
    }


def _confusion(cases: list, key: str) -> dict:
    tp = fp = tn = fn = 0
    for c in cases:
        alert, truth = bool(c[key]), c["truth_abnormal"]
        if truth and alert:
            tp += 1
        elif truth and not alert:
            fn += 1
        elif not truth and alert:
            fp += 1
        else:
            tn += 1
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else None,
        "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
    }


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

    cfg = load_config()

    # ============ 段 A：真实数据上的结构度量（无真值）============
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

    # ============ 段 B：注入式构造真值（度量真 FP/FN）============
    labeled = []
    for case in LABELED_CASES:
        if case["metric"] not in cfg.metrics:
            continue
        labeled.append(_run_labeled_case(cfg, args.db, case, args.threshold))
    sys_a = _confusion(labeled, "threshold_alert")   # 阈值-only
    sys_b = _confusion(labeled, "gated_alert")       # 阈值+统计门控

    report = {
        "db": repo_relative(args.db), "db_kind": db_kind, "threshold": args.threshold, "months": len(pairs),
        # ---- 段 A：结构度量（无真值）----
        "n_cases": len(valid),
        "n_threshold_alerts": len(alerts),
        "n_significance_tested": len(tested),
        "n_suppressed_by_significance": len(suppressed),
        "n_confirmed_by_significance": len(confirmed),
        "n_calendar_driven_alerts": len(calendar_driven),
        "alert_rate_threshold_only": round(len(alerts) / len(valid), 4) if valid else 0.0,
        "suppression_rate": round(len(suppressed) / len(tested), 4) if tested else 0.0,
        "cases": rows,
        # ---- 段 B：注入式构造真值（真 FP/FN）----
        "labeled": {
            "method": ("注入式构造真值：真异常=整期×0.6/1.4（日均显著变化）；"
                       "真正常·日历假象=缩放当期使日均与上期持平（总值只剩月长差）；"
                       "真正常·平稳=缩放当期使总值持平。真值由构造定义，非人工标注。"),
            "n_cases": len(labeled),
            "n_abnormal": sum(1 for c in labeled if c["truth_abnormal"]),
            "n_normal": sum(1 for c in labeled if not c["truth_abnormal"]),
            "threshold_only": sys_a,
            "gated": sys_b,
            "cases": labeled,
        },
        "offline_note": ("段 A 无可信真值，报的是告警量的下降与成因结构；"
                         "段 B 用**注入式构造真值**给出真 FP/FN 率（构造分布，非真实流量分布）。"),
    }
    out_dir = Path(args.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "alert_quality.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 70)
    print("段 A｜告警质量：阈值告警 vs 阈值+统计门控（真实 Olist，相邻月对）")
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
    print("=" * 70)
    print("段 B｜注入式构造真值：真假阳性率 / 假阴性率")
    print("=" * 70)
    print(f"  用例数: {len(labeled)}（真异常 {report['labeled']['n_abnormal']} / "
          f"真正常 {report['labeled']['n_normal']}）")
    print(f"  {'系统':<22}{'TP':>4}{'FP':>4}{'TN':>4}{'FN':>4}"
          f"{'recall':>9}{'假阳性率':>10}{'precision':>11}")
    for name, m in (("阈值-only（旧行为）", sys_a), ("阈值+统计门控（现行为）", sys_b)):
        print(f"  {name:<22}{m['tp']:>4}{m['fp']:>4}{m['tn']:>4}{m['fn']:>4}"
              f"{(m['recall'] if m['recall'] is not None else 0):>9.1%}"
              f"{(m['false_positive_rate'] if m['false_positive_rate'] is not None else 0):>10.1%}"
              f"{(m['precision'] if m['precision'] is not None else 0):>11.1%}")
    print()
    print("逐条（构造真值用例）：")
    for c in labeled:
        a = "告警" if c["threshold_alert"] else "不告"
        b = "告警" if c["gated_alert"] else "不告"
        t = "应告警" if c["truth_abnormal"] else "不应告警"
        print(f"  {c['metric']:16s} {c['prev']}→{c['cur']} [{c['klass']:8s}] "
              f"波动={((c['change_pct'] or 0)) * 100:+7.1f}%  阈值-only={a}  门控={b}  真值={t}")
    print()
    print(f"产物 -> {out_dir / 'alert_quality.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
