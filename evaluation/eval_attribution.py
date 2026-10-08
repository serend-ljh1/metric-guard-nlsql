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

from sqlpa.config import ensure_utf8_console, repo_relative  # noqa: E402
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


_DIM_FRAG_JOIN = {
    "state": ("c.customer_state",
              "JOIN order_items oi ON o.order_id=oi.order_id "
              "JOIN customers c ON o.customer_id=c.customer_id"),
    "category": ("p.product_category_name",
                 "JOIN order_items oi ON o.order_id=oi.order_id "
                 "JOIN products p ON oi.product_id=p.product_id"),
}


def _truth_delta_table(conn: sqlite3.Connection, dim: str, cur_spec: str,
                       prev_spec: str, state_filter: str | None = None) -> Dict[str, float]:
    """**评测侧独立**计算段级 delta 真值表 —— 不调用被测引擎、不复用它的任何输出。

    为什么必须有：旧实现用 `_delta_rank(result,...)` 从被测引擎自己算出的
    `result["dims"][dim]["delta"]` 里取排名，于是"命中"退化成"在引擎自己的表里排第几"——
    实测连"delta 表里只放注入段"的假引擎都会被判 rank=1/命中，即**该评测发现不了引擎算错**。
    这里改用一条独立 SQL 复算真值（口径与被测 gmv 一致：非取消 + SUM(oi.price)，
    段取两期键并集、缺失按 0），排名与命中全部基于它。

    注：时间窗仍复用 `attribution._range_spec` —— 那是共用的日历工具（spec→日期字面量），
    不是被测对象（被测对象是 delta 算术与定位逻辑）；复用可保证两把尺子的窗口完全一致。
    """
    frag, join = _DIM_FRAG_JOIN[dim]
    c0, c1 = attribution._range_spec(cur_spec)
    p0, p1 = attribution._range_spec(prev_spec)
    # state_filter 会引用 customers 别名 c，而 category 的 join 里没有该表 → 需要补上
    if state_filter and "JOIN customers" not in join:
        join += " JOIN customers c ON o.customer_id=c.customer_id"
    scope = f" AND {state_filter}" if state_filter else ""

    def _per_segment(start: str, end: str) -> Dict[str, float]:
        sql = (f"SELECT {frag} AS d, SUM(oi.price) AS v FROM orders o {join} "
               f"WHERE o.order_status!='canceled' AND o.order_purchase_timestamp>={start} "
               f"AND o.order_purchase_timestamp<{end}{scope} GROUP BY {frag}")
        return {r[0]: float(r[1] or 0.0) for r in conn.execute(sql).fetchall()}

    cur, prev = _per_segment(c0, c1), _per_segment(p0, p1)
    keys = set(cur) | set(prev)
    return {k: round(cur.get(k, 0.0) - prev.get(k, 0.0), 4) for k in keys}


def _rank_in(table: Dict[str, float], value: str):
    """在给定 delta 表里按 |delta| 降序取排名；返回 (rank, delta)。未出现 → (n+1, None)。"""
    if not table:
        return None, None
    if value not in table:
        return len(table) + 1, None
    order = sorted(table, key=lambda k: abs(table[k]), reverse=True)
    return order.index(value) + 1, table[value]


def _engine_delta(r: Dict, dim: str) -> Dict[str, float]:
    """被测引擎自己算出的该维 delta 表（**只用于一致性对照**，不再用于命中判定）。"""
    for d in (r.get("dims") or []):
        if d.get("dim") == dim:
            return dict(d.get("delta") or {})
    return {}


def _table_diff(a: Dict[str, float], b: Dict[str, float]) -> float:
    """两张 delta 表的最大绝对差（键并集，缺失按 0）。用于"引擎算术是否与真值一致"。"""
    keys = set(a) | set(b)
    worst = 0.0
    for k in keys:
        worst = max(worst, abs(float(a.get(k, 0.0)) - float(b.get(k, 0.0))))
    return round(worst, 4)


def _delta_rank(result: Dict, dim: str, value: str) -> Optional[int]:
    """【引擎侧】注入值在被测引擎自己 delta 表里的排名。

    ⚠️ 仅供"引擎自评 vs 独立真值"对照使用。命中判定请用 `_rank_in(_truth_delta_table(...))`。
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
    """单题：拷贝库→注入→跑归因→判定是否命中注入真因。

    同时拆 state+category 两个维度，用于度量**维度误报**：注入段理应是非注入维之外、
    且全局 |delta| 最大的主因；若全局顶部被**另一维度**的段占走，说明系统把主因归到
    了错误的维度（注入在 A 维却归到 B 维）。
    """
    c0, c1 = attribution._range_spec(cur_spec)
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
                                previous_spec=prev_spec, dims=["state", "category"],
                                threshold_pct=0.05)
        # ---- 独立真值：在同一份注入后的库上用评测侧 SQL 复算 delta ----
        truth_by_dim = {d: _truth_delta_table(db, d, cur_spec, prev_spec)
                        for d in ("state", "category")}
    finally:
        db.close()
        work.unlink(missing_ok=True)

    truth = truth_by_dim.get(dim, {})
    rank, truth_delta = _rank_in(truth, value)                 # 真值排名（命中判定依据）
    engine_table = _engine_delta(r, dim)
    engine_rank, engine_delta = _rank_in(engine_table, value)  # 引擎自评（仅作对照）
    table_diff = _table_diff(engine_table, truth)

    # 真值口径下的"全局第一主因"：两维 delta 表并集里 |delta| 最大者
    global_pick, global_abs = None, -1.0
    for d, tbl in truth_by_dim.items():
        for k, v in tbl.items():
            if abs(v) > global_abs:
                global_pick, global_abs = (d, k), abs(v)
    dim_dominant = global_pick == (dim, value)
    wrong_dim_top1 = bool(global_pick and global_pick[0] != dim)

    hit_direction = rank == 1 and (truth_delta or 0) < 0
    wrong_direction = rank == 1 and (truth_delta or 0) >= 0
    engine_hit = engine_rank == 1 and (engine_delta or 0) < 0
    return {
        "dim": dim, "value": value, "current_spec": cur_spec, "previous_spec": prev_spec,
        "inject_pct": round(1 - factor, 4),
        "is_abnormal": bool(r.get("is_abnormal")),
        "change_pct": r.get("change_pct"),
        # 真值口径（命中判定）
        "rank": rank,
        "truth_delta": truth_delta,
        "dim_segment_count": len(truth),
        "dim_delta": truth_delta,
        # 引擎口径（仅对照：自评排名 + 算术一致性）
        "engine_rank": engine_rank,
        "engine_delta": engine_delta,
        "engine_dim_segment_count": len(engine_table),
        "delta_table_max_abs_diff": table_diff,
        "delta_table_match": bool(table_diff <= max(1e-6, abs(truth_delta or 0) * 1e-6)),
        "dim_top_key": (max(truth, key=lambda k: abs(truth[k])) if truth else None),
        "dim_top_delta": (truth.get(max(truth, key=lambda k: abs(truth[k])))
                          if truth else None),
        "engine_top_key": (max(engine_table, key=lambda k: abs(engine_table[k]))
                           if engine_table else None),
        "dim_dominant": dim_dominant,
        "wrong_dim_top1": wrong_dim_top1,
        "hit": bool(hit_direction),
        "engine_hit": bool(engine_hit),
        "found_in_top": rank == 1,
        "wrong_direction": bool(wrong_direction),
        "global_top": ({"dim": global_pick[0], "key": global_pick[1], "delta": global_abs}
                       if global_pick else None),
        "top_contributor": ({"dim": global_pick[0], "key": global_pick[1], "delta": global_abs}
                            if global_pick else None),
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
        # 独立真值：在"该州内"用评测侧 SQL 复算品类 delta（不复用 drill 的输出）
        cat_truth = _truth_delta_table(
            db, "category", cur_spec, prev_spec,
            state_filter=f"c.customer_state = '{state_val}'")
    finally:
        db.close()
        work.unlink(missing_ok=True)

    rank, truth_delta = _rank_in(cat_truth, cat_val)
    engine_cat = _engine_delta(r, "category")
    engine_rank, engine_delta = _rank_in(engine_cat, cat_val)
    found = [(c.get("key"), c.get("delta")) for c in (r.get("top_contributors") or [])
             if c.get("dim") == "category"]
    hit_entry = next((e for e in found if e[0] == cat_val), None)
    return {
        "cur_spec": cur_spec, "prev_spec": prev_spec,
        "state": state_val, "injected_category": cat_val,
        "inject_pct": round(1 - factor, 4),
        "path_desc": r.get("path_desc"),
        # 真值口径
        "hit": bool(rank == 1 and (truth_delta or 0) < 0),
        "wrong_direction": bool(rank == 1 and (truth_delta or 0) >= 0),
        "rank": rank,
        "truth_delta": truth_delta,
        "category_segment_count": len(cat_truth),
        "category_top_by_abs": (max(cat_truth, key=lambda k: abs(cat_truth[k]))
                                if cat_truth else None),
        # 引擎口径（对照）
        "engine_rank": engine_rank,
        "engine_delta": engine_delta,
        "engine_hit": bool(engine_rank == 1 and (engine_delta or 0) < 0),
        "delta_table_max_abs_diff": _table_diff(engine_cat, cat_truth),
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
    share_aov = (f0.get("aov") or {}).get("share")
    rows.append({
        "inject": "aov(客单价)", "expected": "aov",
        "ok": bool(r.get("ok")), "main_factor": r.get("main_factor"),
        # 命中要有**优势**：不仅 main_factor 对，注入因子还得解释 ≥50% 的总变动。
        # 否则 factorize 取 max|contribution| 会让"注入即命中"成为同义反复。
        "injected_share": share_aov,
        "margin_ok": bool(share_aov is not None and abs(share_aov) >= 0.5),
        "hit": bool(r.get("ok") and r.get("main_factor") == "aov"
                    and share_aov is not None and abs(share_aov) >= 0.5),
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
    share_qty = (f0.get("order_count") or {}).get("share")
    rows.append({
        "inject": "order_count(单量)", "expected": "order_count",
        "ok": bool(r.get("ok")), "main_factor": r.get("main_factor"),
        "injected_share": share_qty,
        "margin_ok": bool(share_qty is not None and abs(share_qty) >= 0.5),
        "hit": bool(r.get("ok") and r.get("main_factor") == "order_count"
                    and share_qty is not None and abs(share_qty) >= 0.5),
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
    ap.add_argument("--inject-pct", type=float, default=None,
                    help="[兼容旧用法] 单一注入比（若给则覆盖 --inject-pcts 为 [该值]）。"
                         "旧版只测单一档位；爆破版默认多档")
    ap.add_argument("--inject-pcts", nargs="+", type=float,
                    default=[0.30, 0.45, 0.60],
                    help="多档注入比：每段价格下调比例。60% 保证注入段成为主导负驱动；"
                         "30%/45% 是**弱注入**，用来量系统在'注入段未必占主导'时的 top-k 命中")
    ap.add_argument("--top-segments", type=int, default=10,
                    help="每个(期×维)下按当期 GMV 枚举 Top-N 个靶点段（注入目标，自动枚举）")
    ap.add_argument("--dims", nargs="+", default=["state", "category"])
    ap.add_argument("--topk", nargs="+", type=int, default=[1, 2, 3, 5],
                    help="top-k 命中率统计的 k 值集合（按该维 |变化| 排名）")
    ap.add_argument("--keep-frac", type=float, default=0.6,
                    help="量因注入时保留的当期订单比例（仅在 --inject-pct 兼容模式下生效；"
                         "多档模式下量因强度与 --inject-pcts 同步）")
    ap.add_argument("--max-cases", type=int, default=0,
                    help="只跑前 N 条归因题（冒烟用；0=全量）。完整口径请勿使用该参数")
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
    # 兼容旧用法：单档 --inject-pct 覆盖多档
    if args.inject_pct is not None:
        inject_pcts = [args.inject_pct]
    else:
        inject_pcts = sorted({round(v, 4) for v in args.inject_pcts})

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
        # ---- 归因靶点：每个(期×维)按当期 GMV 枚举 Top-N 段，× 多档注入比 ----
        for dim in args.dims:
            if dim not in _INJECT_MATCH:
                continue
            for value, gm in _top_segments(real, dim, c0, c1, k=args.top_segments):
                for p in inject_pcts:
                    scenarios.append({"dim": dim, "value": value, "cur_spec": cur_spec,
                                      "prev_spec": prev_spec, "inject_pct": p, "seg_gmv": gm})
        # ---- 下钻靶点：Top-N 州 各自 州内最大品类，× 多档注入比 ----
        for sv, _sm in _top_segments(real, "state", c0, c1,
                                     k=min(args.top_segments, 8)):
            cv = _top_category_in_state(real, sv, c0, c1)
            if not cv:
                continue
            for p in inject_pcts:
                drill_targets.append({"state": sv, "category": cv, "inject_pct": p,
                                      "cur_spec": cur_spec, "prev_spec": prev_spec})
        factor_periods.append((cur_spec, prev_spec))
    real.close()

    print("=" * 66)
    print("评测：归因命中率（注入式真因 · 爆破式多段枚举）")
    print(f"真库: {args.db}   注入比档: {inject_pcts}")
    print(f"维度: {args.dims}   期数: {args.periods}   每期×维 Top-{args.top_segments} 段")
    print(f"  归因题 {len(scenarios)} / 下钻题 {len(drill_targets)} / 因子题 {len(factor_periods) * len(inject_pcts) * 2}")
    print("  命中判定尺子：**评测侧独立 SQL 复算的 delta 真值表**（不读被测引擎输出）")
    if args.max_cases:
        scenarios = scenarios[:args.max_cases]
        drill_targets = drill_targets[:max(1, args.max_cases // 6)]
        factor_periods = factor_periods[:1]
        print(f"  [冒烟模式] 只跑归因题前 {len(scenarios)} 条 / 下钻 {len(drill_targets)} 条 / "
              f"因子 {len(factor_periods)} 期 —— 数字不完整，勿引用")
    print("=" * 66)

    # ---------- 1) 归因（analyze）命中率 + top-k + 维度误报 ----------
    details = []
    for sc in scenarios:
        r = _run_scenario(cfg, args.db, sc["dim"], sc["value"], sc["cur_spec"],
                          sc["prev_spec"], 1.0 - sc["inject_pct"])
        entry = {**sc, **r}
        details.append(entry)
        mark = "HIT " if r["hit"] else ("N/A " if not r["is_abnormal"] else "MISS")
        reason = ("定位正确" if r["hit"] else
                  ("(低于健康阈值，不产生归因)" if not r["is_abnormal"] else
                   ("方向错" if r["wrong_direction"] else
                    f"未到top1(rank={r['rank']}，段数{r['dim_segment_count']})")))
        print(f"  [{mark}] 归因 {entry['dim']}='{entry['value']}' @{entry['current_spec']} "
              f"(注入{entry['inject_pct']:.0%}) 波动={entry['change_pct']} "
              f"rank={r['rank']}/{r['dim_segment_count']} {reason}")

    # ---- 有效题：注入确实把总体压到"异常(≥阈值)"、系统理应展开归因的题；
    # 低于阈值 → 系统不判异常就不归因，属"未形成波动 N/A"，不进命中率分母。
    valid = [d for d in details if d.get("is_abnormal")]
    n_below = len(details) - len(valid)
    nv = len(valid) or 1
    n_hit = sum(1 for d in valid if d["hit"])
    n_wrong_dir = sum(1 for d in valid if d["wrong_direction"])
    n_not_found = sum(1 for d in valid if not d["found_in_top"])
    # 维度误报的**干净口径**：只有注入段已是"该维第一主因(rank-1)"的题，
    # 若全局 top 却落到另一维，才叫'归错维'；弱注入进非头部段"本不被全局第一"不算误报。
    n_rank1 = sum(1 for d in valid if d["found_in_top"])
    n_rank1_dom = sum(1 for d in valid if d["found_in_top"] and d["dim_dominant"])
    n_wrong_dim = n_rank1 - n_rank1_dom
    hit_rate = _pct(n_hit, len(valid))
    # ---- 双口径对照：真值口径（命中判定）vs 引擎自评口径（旧实现）----
    n_engine_hit = sum(1 for d in valid if d.get("engine_hit"))
    n_table_ok = sum(1 for d in valid if d.get("delta_table_match"))
    worst_diff = max((d.get("delta_table_max_abs_diff") or 0.0) for d in valid) if valid else 0.0
    hit_rate_all = _pct(n_hit, len(details))          # N/A 计入分母（不可被阈值选择美化）
    topk = {}
    for k in args.topk:
        topk[k] = _pct(sum(1 for d in valid if (d.get("rank") or nv) <= k), len(valid))
    mean_seg = _pct(sum(d.get("dim_segment_count") or 0 for d in valid), len(valid))
    print("=" * 66)
    print(f"有效归因题(已判异常): {len(valid)} / 总注入 {len(details)}"
          f"  （{n_below} 题低于阈值未判异常，N/A）")
    print(f"归因命中率(top-1，**独立真值表**): {n_hit}/{len(valid)} = {hit_rate:.2%}"
          f"  未定位 {n_not_found} / 方向错 {n_wrong_dir}(分母 {n_rank1})")
    print(f"  同口径全量分母(含 N/A): {n_hit}/{len(details)} = {hit_rate_all:.2%}"
          f"   N/A 率 {_pct(n_below, len(details)):.2%}")
    print(f"对照·引擎自评(top-1，读引擎自己的 delta 表): {n_engine_hit}/{len(valid)}"
          f" = {_pct(n_engine_hit, len(valid)):.2%}")
    print(f"算术一致性: 引擎 delta 表与独立真值表一致 {n_table_ok}/{len(valid)}"
          f"（最大绝对差 {worst_diff}）")
    print(f"top-k 归因命中率: " + "  ".join(f"k={k}:{topk[k]:.2%}" for k in args.topk)
          + f"   (段数均值≈{mean_seg}，随机基线 top-1≈{_pct(1, max(1, int(mean_seg))):.0%})")
    print(f"维度正确: rank-1 题 {n_rank1}/{len(valid)}"
          f"，其中仍占**全局第一** {n_rank1_dom}/{n_rank1} "
          f"→ 归错维 {n_wrong_dim}（占 rank-1 的 {_pct(n_wrong_dim, max(1, n_rank1)):.2%}）")

    # 分注入比 / 分维度命中率（仅有效题）
    by_inject = {p: {"total": 0, "hit_top1": 0, "ev_hit": 0.0} for p in inject_pcts}
    by_dim = {}
    for d in valid:
        p = d["inject_pct"]
        by_dim.setdefault(d["dim"], {"total": 0, "hit_top1": 0, "ev_hit": 0.0})
        by_inject[p]["total"] += 1
        by_inject[p]["hit_top1"] += (1 if d["hit"] else 0)
        by_inject[p]["ev_hit"] += (1 / max(1, d["dim_segment_count"]))
        dd = by_dim[d["dim"]]
        dd["total"] += 1
        dd["hit_top1"] += (1 if d["hit"] else 0)
        dd["ev_hit"] += (1 / max(1, d["dim_segment_count"]))
    print("分注入比(仅有效题):")
    for p in inject_pcts:
        t = by_inject[p]
        print(f"    注入{t['total']:3d}条 pct={p:.0%}: "
              f"top-1 {t['hit_top1']}/{t['total']}={_pct(t['hit_top1'], t['total']):.2%}"
              f"  (随机基线≈{_pct(t['ev_hit'], t['total']):.2%})")
    print("分维度(仅有效题):")
    for dim, t in by_dim.items():
        print(f"    {dim:10s} {t['total']:3d}条: "
              f"top-1 {t['hit_top1']}/{t['total']}={_pct(t['hit_top1'], t['total']):.2%}"
              f"  (随机基线≈{_pct(t['ev_hit'], t['total']):.2%})")

    # ---------- 2) 下钻（drill）命中率（top-1 + rank-based top-k）----------
    drill_details, n_dh = [], 0
    for dt in drill_targets:
        r = _run_drill_scenario(cfg, args.db, dt["state"], dt["category"],
                                dt["cur_spec"], dt["prev_spec"], 1.0 - dt["inject_pct"])
        entry = {**dt, **r}
        drill_details.append(entry)
        if r["hit"]:
            n_dh += 1
        mark = "HIT " if r["hit"] else "MISS"
        print(f"  [{mark}] 下钻 {entry['state']}⊃品类='{entry['injected_category']}' "
              f"@{entry['cur_spec']}(注入{entry['inject_pct']:.0%}) delta={entry['drill_category_delta']} "
              f"rank={entry['rank']}/{entry['category_segment_count']}")
    drill_hit_rate = _pct(n_dh, len(drill_details))
    drill_topk = {}
    ndv = len(drill_details) or 1
    for k in args.topk:
        drill_topk[k] = _pct(sum(1 for d in drill_details
                                 if (d.get("rank") or ndv) <= k), len(drill_details))
    print(f"下钻命中率(top-1): {n_dh}/{len(drill_details)} = {drill_hit_rate:.2%}"
          + "  top-k: " + "  ".join(f"k={k}:{drill_topk[k]:.2%}" for k in args.topk))

    # ---------- 3) 因子分解（factorize）命中率 ----------
    fz_details, n_fh = [], 0
    for cur_spec, prev_spec in factor_periods:
        for p in inject_pcts:
            # 量因注入强度要随档位变化：旧实现固定用 --keep-frac，导致同一实验在 3 档下
            # 重复跑 3 次、18 条"因子题"里其实只有 12 条独立实验。
            qty_keep = args.keep_frac if args.inject_pct is not None else round(1.0 - p, 4)
            for row in _run_factorize_scenario(cfg, args.db, cur_spec, prev_spec,
                                               1.0 - p, qty_keep):
                row.update({"cur_spec": cur_spec, "inject_pct": p})
                fz_details.append(row)
                if row["hit"]:
                    n_fh += 1
                mark = "HIT " if row["hit"] else "MISS"
                print(f"  [{mark}] 因子 {row['inject']} @{cur_spec}(注入{p:.0%}) -> "
                      f"main={row['main_factor']} (期望 {row['expected']})  share={row['factor_shares']}")
    fz_hit_rate = _pct(n_fh, len(fz_details))
    # 去重：同一(期×因子)若在多档注入比下产生**完全相同**的判定，视为一次实验
    distinct = {(d["inject"], d["cur_spec"], json.dumps(d.get("factor_shares"),
                                                        sort_keys=True))
                for d in fz_details}
    print(f"因子分解命中率: {n_fh}/{len(fz_details)} = {fz_hit_rate:.2%}"
          f"（判定要求 main_factor 正确**且**注入因子解释 ≥50% 变动；"
          f"独立实验数 {len(distinct)}/{len(fz_details)}）")

    report = {
        "suite": "attribution_hit_rate_bruteforce",
        "desc": ("注入式真因（Ground Truth by Construction）：归因(top-k)/下钻/因子分解 "
                 "三维定位命中率；多段枚举 + 弱/中/强多档注入比 + 维度误报率"),
        "db": repo_relative(args.db) if args.db != ":memory:" else ":memory:", "inject_pcts": inject_pcts, "top_segments": args.top_segments,
        "dimensions": args.dims, "periods": args.periods,
        "attribution": {
            "total_injected": len(details), "valid_scenarios": len(valid),
            "below_threshold_na": n_below,
            # 命中判定基于**评测侧独立 SQL 真值表**（不再读被测引擎的 delta）
            "truth_basis": "eval-side independent SQL delta table",
            "hits": n_hit, "hit_rate": hit_rate,
            "hit_rate_all_injected": hit_rate_all, "na_rate": _pct(n_below, len(details)),
            "topk_hit_rate": topk,
            "not_found_in_top": n_not_found,
            # 方向错的**正确分母**：只有 rank-1 的题才有机会判错方向
            "wrong_direction": n_wrong_dir, "wrong_direction_denominator": n_rank1,
            "rank1_in_dim": n_rank1, "rank1_and_global_dominant": n_rank1_dom,
            "wrong_dim_top1": n_wrong_dim,
            "wrong_dim_rate_of_rank1": _pct(n_wrong_dim, max(1, n_rank1)),
            # 对照：引擎自评口径（旧实现的判定方式）+ 算术一致性
            "engine_selfgraded_hits": n_engine_hit,
            "engine_selfgraded_hit_rate": _pct(n_engine_hit, len(valid)),
            "delta_table_match": n_table_ok,
            "delta_table_match_rate": _pct(n_table_ok, len(valid)),
            "delta_table_max_abs_diff": worst_diff,
            "mean_segment_count": mean_seg,
            "random_baseline_top1": _pct(1, max(1, int(mean_seg))) if mean_seg else None,
            "by_inject_pct": {str(p): {"total": t["total"], "hit_top1": t["hit_top1"],
                                       "hit_rate": _pct(t["hit_top1"], t["total"]),
                                       "random_baseline": _pct(t["ev_hit"], t["total"])}
                              for p, t in by_inject.items()},
            "by_dim": {str(k): {"total": v["total"], "hit_top1": v["hit_top1"],
                                "hit_rate": _pct(v["hit_top1"], v["total"]),
                                "random_baseline": _pct(v["ev_hit"], v["total"])}
                       for k, v in by_dim.items()},
            "details": details,
        },
        "drill": {"total_scenarios": len(drill_details), "hits": n_dh,
                  "hit_rate": drill_hit_rate, "topk_hit_rate": drill_topk,
                  "truth_basis": "eval-side independent SQL delta table (within-state)",
                  "engine_selfgraded_hits": sum(1 for d in drill_details if d.get("engine_hit")),
                  "delta_table_match": sum(1 for d in drill_details
                                           if (d.get("delta_table_max_abs_diff") or 0) <= 0.01),
                  "details": drill_details},
        "factorize": {"total_scenarios": len(fz_details), "hits": n_fh,
                      "hit_rate": fz_hit_rate,
                      "scoring": "main_factor 正确 且 注入因子 share≥0.5（避免取 max|贡献| 的同义反复）",
                      "distinct_trials": len(distinct),
                      "details": fz_details},
    }
    out_dir = Path(args.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "attribution.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物 -> {out_dir / 'attribution.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())