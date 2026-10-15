#!/usr/bin/env python
"""HITL 工单 SLA 的**调度入口**（给 cron / 任务计划程序调用）。

用法：
    python tools/run_hitl_sla.py [--now "2026-10-15 09:00:00"] [--sla-hours 24]

行为：
  1. 扫一遍未闭环工单：
     - 已超 SLA 且未升级 → 置 escalated，写 hitl_history（超时升级）；
     - 距 SLA 截止不足 1 小时（或不足 SLA 的 1/4）且未催办 → 写 hitl_history（临期催办）；
  2. 处理结果同时落到 data/hitl_sla_outbox.jsonl，供人工/机器人读取投递。

退出码：0=巡检完成（含"没有需要处理的工单"）。
与 tools/run_subscriptions.py 同款设计：不内置调度器，进程跑完即退出，
定时交给 cron / Task Scheduler / 容器 CronJob（常驻进程最容易在重启时丢任务）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.business import hitl  # noqa: E402
from sqlpa.config import ensure_utf8_console  # noqa: E402


def _parse_now(text: str) -> float:
    """可选的"假装现在是几点"（演示/回归用）；默认取系统当前时间。"""
    if not text:
        return time.time()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    raise SystemExit(f"[ERR] 无法解析 --now：{text}（示例 2026-10-15 09:00）")


def main() -> int:
    ensure_utf8_console()
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", default="", help="以该时刻作为『现在』（缺省用系统当前时间）")
    ap.add_argument("--sla-hours", default="", help="覆盖 SLA 时长（小时），缺省读 SQLPA_HITL_SLA_HOURS")
    args = ap.parse_args()

    if args.sla_hours:
        os.environ["SQLPA_HITL_SLA_HOURS"] = str(args.sla_hours)
    now_ts = _parse_now(args.now)
    summary = hitl.sweep(now_ts=now_ts, actor="cron:sla")

    print(f"SLA 巡检完成（SLA={summary['sla_hours']:g}h）："
          f"超时升级 {summary['escalated_count']}、临期催办 {summary['reminded_count']}")
    for r in summary["escalated"]:
        print(f"  [升级] {r.get('record_id')} {r.get('matched_metric')} "
              f"超期 {r.get('overdue_hours')}h owner={r.get('owner') or '未指派'}")
    for r in summary["reminded"]:
        print(f"  [催办] {r.get('record_id')} {r.get('matched_metric')} "
              f"剩余 {r.get('left_hours')}h owner={r.get('owner') or '未指派'}")
    if not summary["escalated_count"] and not summary["reminded_count"]:
        print("  （暂无超期/临期工单）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
