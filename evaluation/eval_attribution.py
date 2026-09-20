"""评测 7：归因命中率（注入式真因，Ground Truth by Construction）。

这是论文里最缺的一块——此前没有任何"归因/诊断质量"的数字：只验证了"能算出波动"，
从未验证"定位到的主因到底对不对"。本脚本用**注入式真因**补齐：

  1. 从真实 Olist 拷贝一份工作库；
  2. 挑选某期·某维度下真实存在的强势段（如 2018-06 state=SP），
     把它当期价格**整体下调 X%**(默认 60%)——这就是**由构造定义的"真因"**；
  3. 用 attribution.analyze 对该期做维度拆解；
  4. 断言系统定位到**被注入的段**，且方向为"跌"(delta<0) → 计一次命中。

为什么这是可信的评测：
  - 真因不是人标出来的，而是**由注入动作构造出来的**（否则它会混入真实业务噪音）；
  - 只要注入量够大（把当期最大段砍 40%），该段必然是整体波动的最大负数驱动，
    系统若定位不出它就是真实的诊断缺口；
  - 全程确定性、不依赖 LLM、可复核。

用量化方式回答：**归因命中率 = 系统定位到注入真因的题数 / 注入题数**。

用法：
    python evaluation/eval_attribution.py
    python evaluation/eval_attribution.py --periods 2018-06 2018-07 --inject-pct 0.5

产物：<report-dir>/attribution.json（逐题明细 + 命中率聚合）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.config import ensure_utf8_console  # noqa: E402
from sqlpa.business.metric_config import load_config  # noqa: E402
from sqlpa.business import attribution  # noqa: E402

_TMP = ROOT / "._test_tmp"

# 维度的注入匹配：落在该期 + 非取消 + 命中该段即可（同段每行都打同样的折）。
# category 要按商品品类定位（逐 item 匹配产品表）；state 是订单级，命中订单即可。
_INJECT_MATCH = {
    "state": ("c.customer_state = '{value}'",
              "FROM order_items oi JOIN orders o ON oi.order_id=o.order_id "
              "JOIN customers c ON o.customer_id=c.customer_id "
              "WHERE o.order_status != 'canceled'"),
    "category": ("p.product_category_name = '{value}'",
                 "FROM order_items oi JOIN orders o ON oi.order_id=o.order_id "
                 "JOIN products p ON oi.product_id=p.product_id "
                 "WHERE o.order_status != 'canceled'"),
}


def _pct(a: float, b: float) -> float:
    return round(a / b, 4) if b else 0.0


def _correlated_update(dim: str):
    """返回 UPDATE 的关联定位片段：确保只改被注入段当期的行。"""
    value_cond, joined_from = _INJECT_MATCH[dim]

    def _sql(value: str, start: str, end: str, factor: float, conn: sqlite3.Connection):
        # 用相关子查询，保证只剪当期（start<=ts<end）且不误伤其他期/其他段
        if dim == "state":
            match_sub = ("EXISTS (SELECT 1 FROM orders o JOIN customers c "
                         f"ON o.customer_id=c.customer_id WHERE o.order_id=order_items.order_id "
                         f"AND o.order_status!='canceled' AND o.order_purchase_timestamp>={start} "
                         f"AND o.order_purchase_timestamp<{end} AND {value_cond.format(value=value)})")
        else:  # category（按商品匹配，需同时关联 order_items.product_id）
            match_sub = ("EXISTS (SELECT 1 FROM orders o JOIN products p "
                         f"ON p.product_id=order_items.product_id WHERE o.order_id=order_items.order_id "
                         f"AND o.order_status!='canceled' AND o.order_purchase_timestamp>={start} "
                         f"AND o.order_purchase_timestamp<{end} AND {value_cond.format(value=value)})")
        conn.execute(f"UPDATE order_items SET price = price * {factor} WHERE {match_sub}")

    return _sql


_INJECT_FN = {d: _correlated_update(d) for d in _INJECT_MATCH}


def _top_segments(conn: sqlite3.Connection, dim: str, start: str, end: str, k: int) -> List[tuple]:
    """当期 GMV 最大的 k 个段（真实存在的强势段，作为注入靶点）。"""
    frag = {"state": "c.customer_state", "category": "p.product_category_name"}[dim]
    join = {"state": "JOIN order_items oi ON o.order_id=oi.order_id "
                     "JOIN customers c ON o.customer_id=c.customer_id",
            "category": "JOIN order_items oi ON o.order_id=oi.order_id "
                        "JOIN products p ON oi.product_id=p.product_id"}[dim]
    sql = (f"SELECT {frag} AS d, SUM(oi.price) AS v FROM orders o {join} "
           f"WHERE o.order_status!='canceled' AND o.order_purchase_timestamp>={start} "
           f"AND o.order_purchase_timestamp<{end} GROUP BY {frag} ORDER BY v DESC LIMIT {k}")
    return [(r[0], r[1]) for r in conn.execute(sql).fetchall()]


def _delta_rank(result: Dict, dim: str, value: str) -> Optional[int]:
    """注入值在该维度下的**按 |delta| 降序**排名（1=最大负/正驱动；未出现=len(delta)+1）。

    从 analyze 的 result["dims"][dim]["delta"] 取全量段级变化，做 top-k 命中率的基础：
    analyze 的 top_contributors 每维只暴露 top-1，这里用完整 delta 表算 k>1 的宽松命中。
    """
    for d in (result.get("dims") or []):
        if d.get("dim") != dim:
            continue
        delta = d.get("delta") or {}
        if value not in delta:
            return len(delta) + 1
        order = sorted(delta, key=lambda k: abs(delta[k]), reverse=True)
        return order.index(value) + 1
    return None


def _run_scenario(cfg, real_db: str, dim: str, value: str, cur_spec: str,
                  prev_spec: str, factor: float) -> Dict:
    """单题：拷贝库→注入→跑归因→判定是否命中注入真因。"""
    c0, c1 = attribution._range_spec(cur_spec)
    p0, p1 = attribution._range_spec(prev_spec)
    _TMP.mkdir(parents=True, exist_ok=True)
    work = _TMP / f"att-inj-{uuid.uuid4().hex[:10]}.db"
    shutil.copyfile(real_db, work)
    conn = sqlite3.connect(work)
    _INJECT_FN[dim](value, c0, c1, factor, conn)
    conn.commit()
    conn.close()

    db = sqlite3.connect(work)
    try:
        r = attribution.analyze(cfg, db, "gmv", current_spec=cur_spec,
                                previous_spec=prev_spec, dims=[dim],
                                threshold_pct=0.05)
    finally:
        db.close()
        work.unlink(missing_ok=True)

    # 判定：注入段必须出现在该维度的 top_contributors 里，且 delta<0（跌的方向）
    found = [(c.get("key"), c.get("delta"), c.get("pct_of_change"))
             for c in (r.get("top_contributors") or []) if c.get("dim") == dim]
    hit_entry = next((e for e in found if e[0] == value), None)
    hit_direction = hit_entry is not None and hit_entry[1] < 0
    wrong_direction = hit_entry is not None and hit_entry[1] >= 0
    rank = _delta_rank(r, dim, value)
    return {
        "dim": dim, "value": value, "current_spec": cur_spec, "previous_spec": prev_spec,
        "inject_pct": round(1 - factor, 4),
        "is_abnormal": bool(r.get("is_abnormal")),
        "change_pct": r.get("change_pct"),
        "attributed_delta": hit_entry[1] if hit_entry else None,
        "attributed_pct_of_change": hit_entry[2] if hit_entry else None,
        "hit": bool(hit_direction),
        "found_in_top": hit_entry is not None,
        "wrong_direction": wrong_direction,
        "rank": rank,
        "top_contributor": found[0] if found else None,
    }


def _top_category_in_state(conn: sqlite3.Connection, state_val: str, start: str, end: str) -> str:
    """当期某州下 GMV 最大的品类（作为下钻注入靶点）。"""
    sql = (f"SELECT p.product_category_name AS c, SUM(oi.price) AS v FROM orders o "
           f"JOIN order_items oi ON o.order_id=oi.order_id "
           f"JOIN products p ON oi.product_id=p.product_id "
           f"JOIN customers c ON o.customer_id=c.customer_id "
           f"WHERE o.order_status!='canceled' AND o.order_purchase_timestamp>={start} "
           f"AND o.order_purchase_timestamp<{end} AND c.customer_state='{state_val}' "
           f"GROUP BY p.product_category_name ORDER BY v DESC LIMIT 1")
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


def _inject_drill(conn: sqlite3.Connection, state_val: str, cat_val: str,
                  start: str, end: str, factor: float):
    """下钻注入：把"当期·某州·某品类"的 order_items.price 整体打 factor 折。

    只改 当期(ts∈) + customer_state=state_val + 商品品类=cat_val 的行，
    这是由构造定义的"某一层子主因"，用于验证 drill 能否定位到它。
    """
    sub = ("EXISTS (SELECT 1 FROM orders o JOIN customers c ON o.customer_id=c.customer_id "
           "JOIN products p ON p.product_id=order_items.product_id "
           "WHERE o.order_id=order_items.order_id "
           f"AND o.order_status!='canceled' AND o.order_purchase_timestamp>={start} "
           f"AND o.order_purchase_timestamp<{end} "
           f"AND c.customer_state='{state_val}' AND p.product_category_name='{cat_val}')")
    conn.execute(f"UPDATE order_items SET price = price * {factor} WHERE {sub}")


def _run_drill_scenario(cfg, real_db: str, state_val: str, cat_val: str, cur_spec: str,
                        prev_spec: str, factor: float) -> Dict:
    """单题-下钻：注入"州⊃品类"子主因，验证 drill 沿 path 拆到注入品类。

    命中判据：attribution.drill(path=[state=state_val], next_dims=[category]) 的
    top_contributors 里，注入品类 cat_val 存在且 delta<0（向下的方向正确）。
    """
    c0, c1 = attribution._range_spec(cur_spec)
    p0, p1 = attribution._range_spec(prev_spec)
    _TMP.mkdir(parents=True, exist_ok=True)
    work = _TMP / f"drill-inj-{uuid.uuid4().hex[:10]}.db"
    shutil.copyfile(real_db, work)
    conn = sqlite3.connect(work)
    _inject_drill(conn, state_val, cat_val, c0, c1, factor)
    conn.commit()
    conn.close()

    db = sqlite3.connect(work)
    try:
        r = attribution.drill(cfg, db, "gmv", current_spec=cur_spec,
                              previous_spec=prev_spec,
                              path=[{"dim": "state", "value": state_val}],
                              next_dims=["category"], threshold_pct=0.05)
    finally:
        db.close()
        work.unlink(missing_ok=True)

    found = [(c.get("key"), c.get("delta")) for c in (r.get("top_contributors") or [])
             if c.get("dim") == "category"]
    hit_entry = next((e for e in found if e[0] == cat_val), None)
    return {
        "cur_spec": cur_spec, "prev_spec": prev_spec,
        "state": state_val, "injected_category": cat_val,
        "inject_pct": round(1 - factor, 4),
        "path_desc": r.get("path_desc"),
        "hit": bool(hit_entry and hit_entry[1] < 0),
        "wrong_direction": bool(hit_entry and hit_entry[1] >= 0),
        "drill_category_delta": hit_entry[1] if hit_entry else None,
        "drill_category": r.get("top_contributors"),
    }


def _inject_aov(conn: sqlite3.Connection, c0: str, c1: str, factor: float):
    """价因注入：整期所有非取消订单的价格打 factor 折 → 客单价(aov)下降，单量不变。"""
    sub = ("EXISTS (SELECT 1 FROM orders o WHERE o.order_id=order_items.order_id "
           f"AND o.order_status!='canceled' AND o.order_purchase_timestamp>={c0} "
           f"AND o.order_purchase_timestamp<{c1})")
    conn.execute(f"UPDATE order_items SET price = price * {factor} WHERE {sub}")


def _inject_quantity(conn: sqlite3.Connection, c0: str, c1: str, keep_frac: float):
    """量因注入：把当期订单按 rowid 取(1-keep)-比例标记 canceled → 单量(order_count)↓，量价不变。"""
    cand = [r[0] for r in conn.execute(
        "SELECT order_id FROM orders WHERE order_purchase_timestamp>=? AND "
        "order_purchase_timestamp<? AND order_status!='canceled' ORDER BY rowid", (c0.strip("'"), c1.strip("'")))]
    drop = max(1, int(len(cand) * (1 - keep_frac)))
    for oid in cand[:drop]:
        conn.execute("UPDATE orders SET order_status='canceled' WHERE order_id=?", (oid,))


def _run_factorize_scenario(cfg, real_db: str, cur_spec: str, prev_spec: str,
                            factor: float, keep_frac: float) -> List[Dict]:
    """单题-因子分解：两类注入，分别验证「价因→客单价」「量因→单量」被正确定位。

    返回一个 list（一个库里可同时做价因 与量因 两题），每题判定：
      factorize.main_factor == 期望因子（aov / order_count）。
    """
    c0, c1 = attribution._range_spec(cur_spec)
    rows = []

    # ---- 价因（aov）----
    work = _TMP / f"fz-aov-{uuid.uuid4().hex[:10]}.db"
    shutil.copyfile(real_db, work)
    conn = sqlite3.connect(work)
    _inject_aov(conn, c0, c1, factor)
    conn.commit(); conn.close()
    db = sqlite3.connect(work)
    try:
        r = attribution.factorize(cfg, db, "gmv", current_spec=cur_spec,
                                  previous_spec=prev_spec)
    finally:
        db.close(); work.unlink(missing_ok=True)
    f0 = {f.get("factor"): f for f in (r.get("factors") or [])}
    rows.append({
        "inject": "aov(客单价)", "expected": "aov",
        "ok": bool(r.get("ok")), "main_factor": r.get("main_factor"),
        "hit": r.get("ok") and r.get("main_factor") == "aov",
        "factor_shares": {k: v.get("share") for k, v in f0.items()},
    })

    # ---- 量因（order_count）----
    work = _TMP / f"fz-qty-{uuid.uuid4().hex[:10]}.db"
    shutil.copyfile(real_db, work)
    conn = sqlite3.connect(work)
    _inject_quantity(conn, c0, c1, keep_frac)
    conn.commit(); conn.close()
    db = sqlite3.connect(work)
    try:
        r = attribution.factorize(cfg, db, "gmv", current_spec=cur_spec,
                                  previous_spec=prev_spec)
    finally:
        db.close(); work.unlink(missing_ok=True)
    f0 = {f.get("factor"): f for f in (r.get("factors") or [])}
    rows.append({
        "inject": "order_count(单量)", "expected": "order_count",
        "ok": bool(r.get("ok")), "main_factor": r.get("main_factor"),
        "hit": r.get("ok") and r.get("main_factor") == "order_count",
        "factor_shares": {k: v.get("share") for k, v in f0.items()},
    })
    return rows


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="业务库（默认：真实全量库 > 仓库自带样本库；注入在副本上进行）")
    ap.add_argument("--report-dir", default=str(ROOT / "evaluation" / "reports"))
    ap.add_argument("--periods", nargs="+", default=["2018-05", "2018-06", "2018-07"],
                    help="当期月份字面量（真实 Olist 窗口 2016-09~2018-10）")
    ap.add_argument("--inject-pct", type=float, default=0.60,
                    help="每段价格下调比例（默认 60%，保证注入段成为**主导**负驱动；"
                         "过低的注入比在被测段本身环比上涨时不会成为最大负因，测试前提不成立）")
    ap.add_argument("--dims", nargs="+", default=["state", "category"])
    ap.add_argument("--topk", nargs="+", type=int, default=[1, 2, 3, 5],
                    help="top-k 命中率统计的 k 值集合（按该维 |变化| 排名）")
    ap.add_argument("--keep-frac", type=float, default=0.6,
                    help="量因注入时保留的当期订单比例（1-keep 被标取消）")
    args = ap.parse_args()

    cfg = load_config()
    from sqlpa.config import db_kind_note, resolve_db_path
    try:
        args.db, db_kind = resolve_db_path(args.db)
    except FileNotFoundError as e:
        print(f"[SKIP] {e}")
        return 0
    if db_kind_note(db_kind):
        print(db_kind_note(db_kind))
    if db_kind != "full":
        # 该评测的"真值"是**按当期最大分段注入**构造的，依赖分段规模排名；
        # 样本库（按天抽样、单段体量小）会让注入段不再占主导，跑出来的命中率不成立。
        # 宁可不跑，也不给一个会被误读为"系统退化"的数字。
        print("[SKIP] 归因命中率评测需要**全量数据**（真值由最大分段注入构造，依赖分段规模）。")
        print("       获取全量数据：python tools/build_olist_db.py --src data/olist "
              "--out data/olist/olist.db")
        return 0
    factor = 1.0 - args.inject_pct

    real = sqlite3.connect(args.db)
    scenarios, drill_targets, factor_periods = [], [], []
    for cur_spec in args.periods:
        prev_spec = attribution._default_previous_spec(cur_spec)
        c0, c1 = attribution._range_spec(cur_spec)
        # 跳过窗口边界（当月无前月数据）与只有几单的月份
        cur_orders = real.execute(
            f"SELECT COUNT(DISTINCT order_id) FROM orders "
            f"WHERE order_purchase_timestamp>={c0} AND order_purchase_timestamp<{c1}").fetchone()[0] or 0
        if cur_orders < 100:
            print(f"  [略] {cur_spec} 订单过少({cur_orders})，跳过")
            continue
        for dim in args.dims:
            if dim not in _INJECT_MATCH:
                continue
            for value, gm in _top_segments(real, dim, c0, c1, k=1):
                scenarios.append({"dim": dim, "value": value, "cur_spec": cur_spec,
                                  "prev_spec": prev_spec, "seg_gmv": gm})
        # 下钻靶点：当期最大州 及其 州内最大品类（子主因）
        top_state = _top_segments(real, "state", c0, c1, k=1)
        if top_state:
            sv = top_state[0][0]
            cv = _top_category_in_state(real, sv, c0, c1)
            if cv:
                drill_targets.append({"state": sv, "category": cv,
                                      "cur_spec": cur_spec, "prev_spec": prev_spec})
        factor_periods.append((cur_spec, prev_spec))
    real.close()

    print("=" * 66)
    print("评测：归因命中率（注入式真因）")
    print(f"真库: {args.db}   注入比: {args.inject_pct}")
    print(f"维度: {args.dims}   期数: {args.periods}")
    print(f"  归因题 {len(scenarios)} / 下钻题 {len(drill_targets)} / 因子题 {len(factor_periods) * 2}")
    print("=" * 66)

    # ---------- 1) 归因（analyze）命中率 + top-k ----------
    details = []
    n_hit = n_miss = n_not_found = n_wrong_dir = 0
    for sc in scenarios:
        r = _run_scenario(cfg, args.db, sc["dim"], sc["value"], sc["cur_spec"],
                          sc["prev_spec"], factor)
        entry = {**sc, **r}
        details.append(entry)
        if r["hit"]:
            n_hit += 1
        else:
            n_miss += 1
        if r["wrong_direction"]:
            n_wrong_dir += 1
        if not r["found_in_top"]:
            n_not_found += 1
        mark = "HIT " if r["hit"] else "MISS"
        print(f"  [{mark}] 归因 {entry['dim']}='{entry['value']}' @{entry['current_spec']} "
              f"(注入{entry['inject_pct']:.0%}) 波动={entry['change_pct']} "
              f"{'定位正确' if r['hit'] else ('定位错误' if r['wrong_direction'] else '未定位到')}")

    hit_rate = _pct(n_hit, len(scenarios))
    topk = {}
    for k in args.topk:
        topk[k] = _pct(sum(1 for d in details if (d.get("rank") or len(details) + 1) <= k),
                       len(details))
    print("=" * 66)
    print(f"归因命中率(top-1): {n_hit}/{len(scenarios)} = {hit_rate:.2%}"
          f"  未定位 {n_not_found} / 方向错 {n_wrong_dir}")
    print("top-k 归因命中率: " + "  ".join(f"k={k}:{topk[k]:.2%}" for k in args.topk))

    # ---------- 2) 下钻（drill）命中率 ----------
    drill_details, n_dh = [], 0
    for dt in drill_targets:
        r = _run_drill_scenario(cfg, args.db, dt["state"], dt["category"],
                                dt["cur_spec"], dt["prev_spec"], factor)
        entry = {**dt, **r}
        drill_details.append(entry)
        if r["hit"]:
            n_dh += 1
        mark = "HIT " if r["hit"] else "MISS"
        print(f"  [{mark}] 下钻 {entry['state']}⊃品类='{entry['injected_category']}' "
              f"@{entry['cur_spec']} delta={entry['drill_category_delta']}")
    drill_hit_rate = _pct(n_dh, len(drill_details))
    print(f"下钻命中率: {n_dh}/{len(drill_details)} = {drill_hit_rate:.2%}")

    # ---------- 3) 因子分解（factorize）命中率 ----------
    fz_details, n_fh = [], 0
    for cur_spec, prev_spec in factor_periods:
        for row in _run_factorize_scenario(cfg, args.db, cur_spec, prev_spec,
                                           factor, args.keep_frac):
            row.update({"cur_spec": cur_spec})
            fz_details.append(row)
            if row["hit"]:
                n_fh += 1
            mark = "HIT " if row["hit"] else "MISS"
            print(f"  [{mark}] 因子 {row['inject']} @{cur_spec} -> main={row['main_factor']} "
                  f"(期望 {row['expected']})  share={row['factor_shares']}")
    fz_hit_rate = _pct(n_fh, len(fz_details))
    print(f"因子分解命中率: {n_fh}/{len(fz_details)} = {fz_hit_rate:.2%}")

    report = {
        "suite": "attribution_hit_rate",
        "desc": ("注入式真因（Ground Truth by Construction）：归因(top-k)/下钻/因子分解 "
                 "三维定位命中率"),
        "db": args.db, "inject_pct": args.inject_pct,
        "dimensions": args.dims, "periods": args.periods,
        "attribution": {"total_scenarios": len(scenarios), "hits": n_hit,
                        "misses": n_miss, "hit_rate": hit_rate,
                        "topk_hit_rate": topk,
                        "not_found_in_top": n_not_found,
                        "wrong_direction": n_wrong_dir,
                        "details": details},
        "drill": {"total_scenarios": len(drill_details), "hits": n_dh,
                  "hit_rate": drill_hit_rate, "details": drill_details},
        "factorize": {"total_scenarios": len(fz_details), "hits": n_fh,
                      "hit_rate": fz_hit_rate, "details": fz_details},
    }
    out_dir = Path(args.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "attribution.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物 -> {out_dir / 'attribution.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())