"""治理 Agent 与异常归因拆解 Agent 的测试。"""
from sqlpa.business.metric_config import BusinessConfig, Metric, Dimension, load_config
from sqlpa.business import governance
from sqlpa.business.attribution import analyze


def _cfg() -> BusinessConfig:
    """造一个含口径冲突的配置：两个语义相近但公式不同的指标。"""
    m1 = Metric("gmv", "GMV成交总额", "支付金额", "SUM(oi.price)", "FROM orders o", "1=1",
                support_dims=["category"], support_filters=[], owner="财务线", version="v3")
    m2 = Metric("net_gmv", "净成交额", "扣退款后的支付金额", "SUM(oi.price) - SUM(refund.amount)",
                "FROM orders o", "1=1", support_dims=["category"], support_filters=[],
                owner="运营线", version="v1")
    m3 = Metric("order_count", "订单数", "已送达订单", "COUNT(DISTINCT o.order_id)",
                "FROM orders o", "1=1", support_dims=[], support_filters=[],
                owner="电商线", version="v2")
    return BusinessConfig(
        metrics={"gmv": m1, "net_gmv": m2, "order_count": m3},
        dimensions={"category": Dimension("category", "品类", "p.product_category_name")},
    )


def test_detect_conflicts_finds_near_duplicate_expr():
    cfg = _cfg()
    conflicts = governance.detect_conflicts(cfg)
    keys = [sorted(c["keys"]) for c in conflicts]
    assert ["gmv", "net_gmv"] in keys  # 语义相近但公式不同 → 命中冲突
    assert ["gmv", "order_count"] not in keys  # 语义不同 → 不报


def test_explain_metric_returns_owner_version():
    cfg = _cfg()
    ex = governance.explain_metric(cfg, "gmv")
    assert ex["ok"] and ex["owner"] == "财务线" and ex["version"] == "v3"
    assert governance.explain_metric(cfg, "nope")["ok"] is False


def _metric(key, name, expr, owner="财务线", version="v1"):
    return Metric(key, name, "测试口径", expr, "FROM orders o", "1=1",
                  support_dims=[], support_filters=[], owner=owner, version=version)


def test_detect_conflicts_same_name_diff_expr():
    """同名不同式：key 不同、展示名相同、公式不同 → error 级（同名不同数）。"""
    cfg = BusinessConfig(
        metrics={"gmv_a": _metric("gmv_a", "GMV成交总额", "SUM(oi.price)"),
                 "gmv_b": _metric("gmv_b", "GMV成交总额", "SUM(oi.price)+SUM(oi.freight_value)")},
        dimensions={})
    hits = [c for c in governance.detect_conflicts(cfg, history={})
            if c["type"] == "同名不同式"]
    assert hits and sorted(hits[0]["keys"]) == ["gmv_a", "gmv_b"]
    assert hits[0]["severity"] == "error"


def test_detect_conflicts_same_expr_diff_name():
    """同式不同名：公式（归一后）相同但名称不同 → info 级（口径冗余）。"""
    cfg = BusinessConfig(
        metrics={"gmv": _metric("gmv", "GMV成交总额", "SUM(oi.price)"),
                 "gmv_dup": _metric("gmv_dup", "支付金额", "  sum( oi.price )  ")},
        dimensions={})
    hits = [c for c in governance.detect_conflicts(cfg, history={})
            if c["type"] == "同式不同名"]
    assert hits and set(hits[0]["keys"]) == {"gmv", "gmv_dup"}
    assert hits[0]["severity"] == "info"


def test_detect_conflicts_version_drift_uses_metric_versions():
    """版本漂移：当前配置公式 ≠ metric_versions 里最后登记的公式 → warning；一致则不报。"""
    import json
    cfg = BusinessConfig(
        metrics={"gmv": _metric("gmv", "GMV成交总额", "SUM(oi.price)", version="v2")},
        dimensions={})
    drift = {"gmv": {"version_seq": 3, "action": "update", "actor": "admin",
                     "after_json": json.dumps(
                         {"metric_expr": "SUM(oi.price) - SUM(r.refund)"}, ensure_ascii=False)}}
    hits = [c for c in governance.detect_conflicts(cfg, history=drift)
            if c["type"] == "版本漂移"]
    assert hits and hits[0]["keys"] == ["gmv"] and hits[0]["severity"] == "warning"

    same = {"gmv": {"version_seq": 3, "action": "update", "actor": "admin",
                    "after_json": json.dumps({"metric_expr": "SUM(oi.price)"}, ensure_ascii=False)}}
    assert not [c for c in governance.detect_conflicts(cfg, history=same)
                if c["type"] == "版本漂移"]


def _sample_db(tmpdir_clean, months):
    """造一个最小 olist 结构库，含订单时间列，供归因拆解。

    日期用 business_months 动态锚点，与「本月」语义对齐，避免日期炸弹。
    """
    import sqlite3
    db = tmpdir_clean / "t.db"
    cur, prev, prev_late = (months["current"], months["previous"],
                            months["previous_late"])
    conn = sqlite3.connect(db)
    conn.executescript(f"""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES ('o1','c1','{prev}','delivered'),
        ('o2','c1','{prev_late}','delivered'),('o3','c2','{cur}','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """)
    conn.commit()
    return conn


def test_attribution_parallel_dimension_split(tmpdir_clean, business_months):
    cfg = load_config()
    conn = _sample_db(tmpdir_clean, business_months)
    r = analyze(cfg, conn, "gmv", current_spec="本月", dims=["category"])
    conn.close()
    assert r["ok"] is True
    assert "current_total" in r and r["current_total"] is not None


def test_attribution_returns_empty_when_unusable(tmpdir_clean):
    import sqlite3
    db = tmpdir_clean / "t.db"
    conn = sqlite3.connect(db)
    r = analyze(load_config(), conn, "gmv", current_spec="本月")
    conn.close()
    assert r["ok"] is False


def test_attribution_dims_populated_on_abnormal(tmpdir_clean, business_months):
    """回归：维度拆分须在并行线程里独立取到数据（sqlite3 跨线程连接问题）。"""
    conn = _sample_db(tmpdir_clean, business_months)
    r = analyze(load_config(), conn, "gmv", current_spec="本月", dims=["category"],
                threshold_pct=0.0)  # 强制触发拆分
    conn.close()
    assert r["ok"] is True
    # 当期（本月）orders 表有 o3 一条 → 至少 1 个维度有值
    assert any(r["dims"][i]["current"] for i in range(len(r["dims"])))


def test_notify_anomaly_only_on_abnormal(tmpdir_clean, business_months):
    import sqlite3
    from sqlpa.business.attribution import notify_anomaly
    conn = _sample_db(tmpdir_clean, business_months)
    r = analyze(load_config(), conn, "gmv", current_spec="本月", dims=["category"])
    conn.close()
    hitl_file = tmpdir_clean / "hitl.jsonl"

    # 强制置为低阈值，让波动成为异常 → 应入队并带负责人
    r_abn = dict(r)
    r_abn["is_abnormal"] = True
    rid = notify_anomaly(load_config(), r_abn, path=str(hitl_file))
    assert rid and hitl_file.exists()
    payload = open(hitl_file, encoding="utf-8").read()
    assert '"owner": "数据组-财务线"' in payload

    # is_abnormal=False → 不入队
    rid2 = notify_anomaly(load_config(), {"ok": True, "is_abnormal": False}, path=str(hitl_file))
    assert rid2 is None