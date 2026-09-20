"""跑通治理面演示：口径冲突检测 + 异常归因 + HITL 闭环（真实 Olist 数据）。

用法：python demo_governance.py
"""
import os
import sqlite3
import sys
from pathlib import Path

# 与 api.py / run_business.py 保持一致：把仓库内的 src 加入 import 路径，
# 否则从项目根直接 `python demo_governance.py` 会 ModuleNotFoundError: sqlpa
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.business.metric_config import load_config  # noqa: E402
from sqlpa.business import governance  # noqa: E402
from sqlpa.business import attribution  # noqa: E402


def main():
    db_path = os.path.join("data", "olist", "olist.db")
    conn = sqlite3.connect(db_path)
    cfg = load_config()
    sep = "=" * 60
    ok = True

    # ---------- 场景 1：口径冲突检测 ----------
    print(sep)
    print("场景1  口径冲突检测（GovernanceAgent）")
    print(sep)
    conflicts = governance.detect_conflicts(cfg)
    if conflicts:
        for c in conflicts:
            print(f"\n[严重度 {c['severity']}]")
            print(c["reason"])
            for i, k in enumerate(c["keys"]):
                print(f"  - {c['names'][i]} ({k}) 公式={c['exprs'][k]}  "
                      f"负责人={c['owners'][k]}  {c['versions'][k]}")
    else:
        print("当前指标中心健康，未检测到口径冲突。")
        print("\n>>> 注入演示：人为新增一名近义但公式不同的指标（净成交额），触发冲突告警：")
        # 临时注入一个与 GMV 近义、但公式不同的指标，演示冲突检测能力
        import copy
        from sqlpa.business.metric_config import Metric
        cfg2 = copy.deepcopy(cfg)
        cfg2.metrics["net_gmv"] = Metric(
            "net_gmv", "净成交额", "扣退款后的支付金额", "SUM(oi.price) - COALESCE(SUM(r.refund),0)",
            "FROM orders o JOIN order_items oi ON o.order_id=oi.order_id",
            "o.order_status != 'canceled'",
            support_dims=["dt", "state", "category"], support_filters=["time_range", "state", "category"],
            owner="运营线", version="v1")
        for c in governance.detect_conflicts(cfg2):
            print(f"\n[严重度 {c['severity']}] {c['reason']}")
            for i, k in enumerate(c["keys"]):
                print(f"  - {c['names'][i]} ({k}) 公式={c['exprs'][k]}  "
                      f"负责人={c['owners'][k]}  {c['versions'][k]}")
    # 展示指标解释
    ex = governance.explain_metric(cfg, "gmv")
    print("\n[指标解释示例 GMV]")
    print(f"  {ex['name']}: {ex['desc']}")
    print(f"  公式={ex['metric_expr']}  负责人={ex['owner']}  版本={ex['version']}")
    print(f"  支持维度={ex['support_dims']}")

    # ---------- 场景 2：异常归因 ----------
    print("\n" + sep)
    print("场景2  异常归因拆解（真实 Olist，按 state/category 并行拆解）")
    print(sep)
    # 用真实数据时间窗口内的两个月做对比（Olist 为 2016-09 ~ 2018-10）
    # 实扫确认 2018-03 vs 2018-02 为真实季末下滑 -14.6%（>5%，触发维度归因）
    month_pairs = [("2018-03", "2018-02"), ("2018-08", "2018-07")]
    for metric in ["gmv", "order_count"]:
        for cur, prev in month_pairs:
            r = attribution.analyze(cfg, conn, metric, current_spec=cur,
                                    previous_spec=prev, dims=["state", "category"])
            if not r["ok"]:
                print(f"\n[{metric} {cur} vs {prev}] 归因失败: {r['reason']}")
                continue
            print(f"\n[{r['metric_name']}] 当期({cur})={r['current_total']:,.2f} "
                  f"上期({prev})={r['previous_total']:,.2f} "
                  f"变化={r['change']:+,.2f} ({r['change_pct']*100:+.1f}%)")
            if r["is_abnormal"]:
                print("  → 超过阈值，维度拆解（并行查询）：")
                for c in r["top_contributors"]:
                    # pct_of_change 可能为 None：比率/均值类指标或"维度没覆盖全部行"时，
                    # 系统会拒绝给出"占波动"（数学上不成立），这里如实显示而不是硬乘。
                    pct = c.get("pct_of_change")
                    share = f"占整体波动 {pct * 100:.0f}%" if pct is not None else "占比不适用（不可加指标）"
                    print(f"    - {c['desc']}  ({share})")
                if r.get("contribution_note"):
                    print(f"  ⚠ {r['contribution_note']}")
                if (r.get("calendar") or {}).get("note"):
                    print(f"  ⚠ {r['calendar']['note']}")
            else:
                print(f"  → 波动在阈值内（默认5%），不展开归因/不告警")
            break  # 只取第一个有完整结果的月份对
        print()

    # ---------- 场景 3：环形闭环（异常→HITL→负责人） ----------
    print("\n" + sep)
    print("场景3  闭环：异常告警 → 写入 HITL 队列 → 关联负责人")
    print(sep)
    r = attribution.analyze(cfg, conn, "gmv", current_spec="2018-03",
                            previous_spec="2018-02", dims=["state", "category"],
                            threshold_pct=0.01)
    hitl_file = os.path.join("data", "demo_hitl.jsonl")
    if r["ok"] and r["is_abnormal"]:
        rid = attribution.notify_anomaly(cfg, r, path=hitl_file)
        if rid:
            print(f"已生成 HITL 待办 #{rid}，推送负责人「{cfg.metrics['gmv'].owner}」")
            print("HITL 记录内容：")
            with open(hitl_file, encoding="utf-8") as f:
                print("  " + f.read().strip())
        else:
            print("no notify")
    else:
        print("未触发异常（阈值内或无数据），未生成待办")

    conn.close()
    print("\n" + "=" * 60)
    print("OK" if ok else "有冲突/异常输出（属正常演示结果）")


if __name__ == "__main__":
    sys.exit(main())