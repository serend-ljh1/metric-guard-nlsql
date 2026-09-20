#!/usr/bin/env python
"""构建**仓库自带的小样本库**，让 `git clone` 之后无需下载 66MB 真实数据也能跑通。

背景：真实 Olist 库（data/olist/olist.db，约 66MB）被 .gitignore 排除，于是克隆者
跑 pytest 会看到大量 skip、评测脚本全部 SKIP、前端打开没有数据可演示——
"开箱即跑"是简历项目的第一道门槛。

做法：**按天分层抽样**（默认每天最多 5 单，覆盖窗口内每一天）。
不用"每月取前 N 单"——那种抽法会把样本全挤在月初几天，导致
①日粒度显著性检验拿不到足够天数、②"本月 vs 上月"的演示在月中无可比数据。

输出：data/sample/olist_sample.db（含 orders/order_items/customers/products/reviews
      + 查询所需索引），并打印体积与覆盖范围。

用法：
    python tools/build_olist_sample.py                     # 从真实库抽样
    python tools/build_olist_sample.py --per-day 8         # 抽大一点
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.config import ensure_utf8_console  # noqa: E402

SCHEMA = """
CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT, order_status TEXT,
  order_purchase_timestamp TEXT, order_delivered_customer_date TEXT,
  order_estimated_delivery_date TEXT);
CREATE TABLE order_items(order_id TEXT, order_item_id INTEGER, product_id TEXT,
  seller_id TEXT, price REAL, freight_value REAL);
CREATE TABLE customers(customer_id TEXT PRIMARY KEY, customer_unique_id TEXT,
  customer_zip_code_prefix TEXT, customer_city TEXT, customer_state TEXT);
CREATE TABLE products(product_id TEXT PRIMARY KEY, product_category_name TEXT);
CREATE TABLE reviews(review_id TEXT, order_id TEXT, review_score REAL);
CREATE INDEX idx_orders_time ON orders(order_purchase_timestamp);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_reviews_order ON reviews(order_id);
"""


def build(src: Path, dest: Path, per_day: int) -> dict:
    if dest.exists():
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(dest)
    conn.executescript(SCHEMA)
    conn.execute("ATTACH DATABASE ? AS sr", (str(src.resolve()),))

    # 按天分层抽样：**每一天**都留数据，保证日粒度序列与"上月 vs 本月"分析可用
    conn.execute("""
        INSERT INTO orders
        SELECT order_id, customer_id, order_status, order_purchase_timestamp,
               order_delivered_customer_date, order_estimated_delivery_date
        FROM (
            SELECT o.*, ROW_NUMBER() OVER (
                       PARTITION BY DATE(o.order_purchase_timestamp)
                       ORDER BY o.order_purchase_timestamp) AS rn
            FROM sr.orders o) t
        WHERE rn <= ?""", (per_day,))
    for table in ("order_items", "customers", "products", "reviews"):
        if table == "order_items":
            conn.execute("INSERT INTO order_items SELECT * FROM sr.order_items "
                         "WHERE order_id IN (SELECT order_id FROM orders)")
        elif table == "customers":
            conn.execute("INSERT INTO customers SELECT * FROM sr.customers "
                         "WHERE customer_id IN (SELECT customer_id FROM orders)")
        elif table == "products":
            conn.execute("INSERT INTO products SELECT * FROM sr.products "
                         "WHERE product_id IN (SELECT product_id FROM order_items)")
        else:
            conn.execute("INSERT INTO reviews SELECT * FROM sr.reviews "
                         "WHERE order_id IN (SELECT order_id FROM orders)")
    conn.commit()
    conn.execute("DETACH DATABASE sr")
    stats = {}
    for t in ("orders", "order_items", "customers", "products", "reviews"):
        stats[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    rng = conn.execute("SELECT MIN(order_purchase_timestamp), MAX(order_purchase_timestamp), "
                       "COUNT(DISTINCT DATE(order_purchase_timestamp)) "
                       "FROM orders").fetchone()
    conn.execute("VACUUM")
    conn.close()
    return {"tables": stats, "first": rng[0], "last": rng[1], "days": rng[2],
            "size_mb": round(dest.stat().st_size / 1024 / 1024, 2)}


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(ROOT / "data" / "olist" / "olist.db"))
    ap.add_argument("--out", default=str(ROOT / "data" / "sample" / "olist_sample.db"))
    ap.add_argument("--per-day", type=int, default=5,
                    help="每天最多抽多少单（默认 5；样本库要覆盖每一天）")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"[!!] 找不到真实库 {src}；请先 python tools/build_olist_db.py")
        return 1
    info = build(src, Path(args.out), args.per_day)
    print(f"已生成样本库 {args.out}")
    print(f"  覆盖: {info['days']} 个有数据的日子（{info['first']} ~ {info['last']}）")
    print(f"  行数: {info['tables']}")
    print(f"  体积: {info['size_mb']} MB")
    if info["size_mb"] > 4:
        print("  提示：体积偏大，可减小 --per-day 后重新生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
