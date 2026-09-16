"""
tools/build_olist_db.py
=======================
把 Olist 巴西电商公开数据集导入 SQLite，作为业务语义层的底座库。

用法：
  1) 下载 Olist 数据（Kaggle 或 GitHub 镜像），解压出 9 个 CSV。
  2) 放到 data/olist/ 目录。
  3) 运行：python tools/build_olist_db.py --src data/olist --out data/olist/olist.db

说明：本模块按 Olist 的字段名构建 orders/order_items/customers/products/reviews 表，
  字段与 business_config.yaml 里的指标/维度 SQL 片段对齐。
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path
from typing import Dict

# 目标表 -> (csv文件名特征, 要导入的列)
TABLES: Dict[str, tuple] = {
    "orders": ("orders_dataset", ["order_id", "customer_id", "order_status",
                                    "order_purchase_timestamp",
                                    "order_delivered_customer_date",
                                    "order_estimated_delivery_date"]),
    "order_items": ("order_items_dataset", ["order_id", "order_item_id", "product_id",
                                             "seller_id", "price", "freight_value"]),
    "customers": ("customers_dataset", ["customer_id", "customer_unique_id",
                                         "customer_zip_code_prefix", "customer_city",
                                         "customer_state"]),
    "products": ("products_dataset", ["product_id", "product_category_name"]),
    "reviews": ("order_reviews_dataset", ["review_id", "order_id", "review_score"]),
}

_SCHEMA = {
    "orders": ("CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT, "
               "order_status TEXT, order_purchase_timestamp TEXT, "
               "order_delivered_customer_date TEXT, order_estimated_delivery_date TEXT)"),
    "order_items": ("CREATE TABLE order_items(order_id TEXT, order_item_id INTEGER, "
                     "product_id TEXT, seller_id TEXT, price REAL, freight_value REAL)"),
    "customers": ("CREATE TABLE customers(customer_id TEXT PRIMARY KEY, "
                  "customer_unique_id TEXT, customer_zip_code_prefix TEXT, "
                  "customer_city TEXT, customer_state TEXT)"),
    "products": ("CREATE TABLE products(product_id TEXT PRIMARY KEY, "
                 "product_category_name TEXT)"),
    "reviews": ("CREATE TABLE reviews(review_id TEXT, order_id TEXT, review_score REAL)"),
}


def build(src: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(str(out))
    c = conn.cursor()
    for tbl, ddl in _SCHEMA.items():
        c.execute(ddl)
    files = {p.name.lower(): p for p in src.glob("*.csv")}
    for tbl, (feature, cols) in TABLES.items():
        # 按文件名特征匹配
        csvfile = next((p for name, p in files.items() if feature in name), None)
        if not csvfile:
            print(f"  [!!] 未找到 {tbl} 的 CSV（需文件名含 {feature} 特征, 在 {src} 下）")
            continue
        insert = (f"INSERT INTO {tbl} VALUES ({','.join('?' * len(cols))})")
        with open(csvfile, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            batch = []
            for row in reader:
                batch.append(tuple(row.get(col) for col in cols))
                if len(batch) >= 5000:
                    c.executemany(insert, batch)
                    batch = []
            c.executemany(insert, batch)
        print(f"  [OK] {tbl} <- {csvfile.name}")
    conn.commit()
    # 建索引（查询更快）
    for idx, table, col in [("ix_orders_ts", "orders", "order_purchase_timestamp"),
                            ("ix_oi_order", "order_items", "order_id"),
                            ("ix_cust_state", "customers", "customer_state")]:
        try:
            c.execute(f"CREATE INDEX {idx} ON {table}({col})")
        except sqlite3.Error:
            pass
    conn.commit()
    conn.close()
    print(f"完成 -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="data/olist")
    ap.add_argument("--out", default="data/olist/olist.db")
    args = ap.parse_args()
    build(Path(args.src), Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
