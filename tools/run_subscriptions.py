#!/usr/bin/env python
"""订阅告警的**调度入口**（给 cron / 任务计划程序调用）。

用法：
    python tools/run_subscriptions.py [--db data/olist/olist.db] [--dry] [--outbox data/alerts_outbox.jsonl]

行为：
  1. 读 data/subscriptions.json 里的订阅（指标 + 阈值 + 负责人 + 投递通道）；
  2. 用与人工归因**同一套**口径检查波动（阈值 + 显著性门控）；
  3. 需要推送的告警按各自通道投递（console / file / webhook）。

退出码：0=检查并投递完成（含"没有告警"）；1=有投递失败；2=缺库/参数问题。
设计上不内置调度器：定时交给 cron/Task Scheduler/容器 CronJob，进程退出即结束，
避免"常驻进程 + 内存里排队"这种在真实部署里最容易丢告警的形态。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import sqlite3  # noqa: E402

from sqlpa.business.deliver import check_subscriptions, deliver_alerts, load_subscriptions  # noqa: E402
from sqlpa.business.metric_config import load_config  # noqa: E402
from sqlpa.config import ensure_utf8_console  # noqa: E402


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "olist" / "olist.db"))
    ap.add_argument("--subs", default=str(ROOT / "data" / "subscriptions.json"))
    ap.add_argument("--outbox", default=str(ROOT / "data" / "alerts_outbox.jsonl"))
    ap.add_argument("--dry", action="store_true", help="只检查不投递")
    args = ap.parse_args()

    subs = load_subscriptions(args.subs)
    if not subs:
        print(f"[SKIP] 没有订阅规则（{args.subs}）。"
              f"可用 deliver.add_subscription(...) 或 POST /api/subscriptions 添加。")
        return 0
    if not Path(args.db).exists():
        print(f"[SKIP] 缺少业务库 {args.db}：订阅检查需要真实/样本数据。")
        return 0

    cfg = load_config()
    alerts = check_subscriptions(cfg, sqlite3.connect(args.db), path=args.subs)
    need = [a for a in alerts if a.get("status") == "alert"]
    suppressed = [a for a in alerts if a.get("status") == "suppressed"]
    print(f"订阅 {len(alerts)} 条：需推送 {len(need)}、被显著性门控抑制 {len(suppressed)}、"
          f"正常/跳过 {len(alerts) - len(need) - len(suppressed)}")
    for a in alerts:
        flag = {"alert": "告警", "suppressed": "抑制", "ok": "正常",
                "skipped": "跳过", "error": "错误"}.get(a.get("status"), a.get("status"))
        extra = a.get("reason") or f"波动 {(a.get('change_pct') or 0) * 100:+.1f}%"
        print(f"  [{flag}] {a.get('metric')} {a.get('time_spec')}: {extra}")

    if args.dry or not need:
        return 0
    report = deliver_alerts(need, outbox=args.outbox)
    print(f"投递：成功 {report['sent']}、失败 {report['failed']}")
    for r in report["results"]:
        if not r.get("ok"):
            print(f"  ✗ {r.get('metric')}: {r.get('detail')}")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
