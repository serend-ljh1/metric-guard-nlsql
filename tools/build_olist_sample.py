"""
tools/build_olist_sample.py
===========================
生成一个与 Olist 同表结构的最小样本库，用于**离线验证业务组装器**产出的 SQL 能否正确执行。

⚠️ 这不是真实 Olist 数据，只是"结构对齐 + 几行数据"，用于证明组装逻辑正确；
   真实评测/业务演示请用 build_olist_db.py 导入完整 Olist 数据。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "olist_sample" / "sample.db"


def build(path: Path = OUT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    c = conn.cursor()
    c.executescript("""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT, order_status TEXT,
        order_purchase_timestamp TEXT, order_delivered_customer_date TEXT, order_estimated_delivery_date TEXT);
      CREATE TABLE order_items(order_id TEXT, order_item_id INTEGER, product_id TEXT, seller_id TEXT, price REAL, freight_value REAL);
      CREATE TABLE customers(customer_id TEXT PRIMARY KEY, customer_unique_id TEXT,
        customer_zip_code_prefix TEXT, customer_city TEXT, customer_state TEXT);
      CREATE TABLE products(product_id TEXT PRIMARY KEY, product_category_name TEXT);
      CREATE TABLE reviews(review_id TEXT PRIMARY KEY, order_id TEXT, review_score REAL);
    """)
    c.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", [
        ("o1", "c1", "delivered", "2026-08-05 10:00:00", "2026-08-09", "2026-08-12"),
        ("o2", "c2", "delivered", "2026-08-18 09:00:00", "2026-08-30", "2026-08-25"),
        ("o3", "c1", "canceled", "2026-08-21 12:00:00", None, None),
        ("o4", "c3", "delivered", "2026-08-25 08:00:00", "2026-08-29", "2026-08-26"),
    ])
    c.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", [
        ("o1", 1, "p1", "s1", 100.0, 10.0),
        ("o1", 2, "p2", "s1", 40.0, 5.0),
        ("o2", 1, "p1", "s1", 100.0, 10.0),
        ("o3", 1, "p2", "s2", 40.0, 5.0),
        ("o4", 1, "p1", "s2", 100.0, 10.0),
        ("o4", 2, "p2", "s2", 40.0, 5.0),
    ])
    c.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", [
        ("c1", "u1", "01000", "Sao Paulo", "SP"),
        ("c2", "u2", "20000", "Rio de Janeiro", "RJ"),
        ("c3", "u1", "30000", "Belo Horizonte", "MG"),
    ])
    c.executemany("INSERT INTO products VALUES (?,?)", [
        ("p1", "electronics"), ("p2", "books"),
    ])
    c.executemany("INSERT INTO reviews VALUES (?,?,?)", [
        ("r1", "o1", 5.0), ("r2", "o2", 3.0), ("r3", "o4", 4.0),
    ])
    conn.commit()
    conn.close()
    print("written:", path)


if __name__ == "__main__":
    build()
