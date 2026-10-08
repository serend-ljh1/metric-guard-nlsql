"""交付物层测试（导出报告 / CSV / 订阅告警）。

锁定"取数要能变成可交付的产物"：报告必须**自带口径说明**（否则数字不可信、
也无法转发复核），订阅告警必须与归因共用同一套阈值判定（避免两处口径不一致）。
"""
from __future__ import annotations

import sqlite3

import pytest

from sqlpa.business import deliver
from sqlpa.business.metric_config import load_config

CFG = load_config()


@pytest.fixture
def db(tmpdir_clean, business_months):
    cur, prev, prev_late = (business_months["current"], business_months["previous"],
                            business_months["previous_late"])
    p = tmpdir_clean / "biz.db"
    con = sqlite3.connect(p)
    con.executescript(f"""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','{prev}','delivered'),('o2','c1','{prev_late}','delivered'),
        ('o3','c2','{cur}','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """)
    con.commit()
    return con


def _fake_answer(**over):
    a = {
        "ok": True, "path": "semantic", "metric": "gmv", "metric_name": "GMV成交总额",
        "metric_expr": "SUM(oi.price)", "dims": ["state"],
        "columns": ["gmv", "state"], "rows": [[80.0, "RJ"], [0.0, "SP"]],
        "sql": "SELECT SUM(oi.price) AS gmv, c.customer_state AS state FROM orders o ...",
        "compile": {"metric_expr": "SUM(oi.price)", "owner": "数据组-财务线",
                    "version": "v3", "sources": ["orders", "order_items"],
                    "filters": {"time_range": "本月"}},
    }
    a.update(over)
    return a


# ---------------- 报告 ----------------

def test_report_contains_provenance(tmpdir_clean):
    """报告必须带口径说明——这是数字可被信任、可被转发复核的前提。"""
    rp = tmpdir_clean / "r.md"
    out = deliver.export_report(rp, "各州本月的GMV", _fake_answer())
    text = rp.read_text(encoding="utf-8")
    assert "口径说明" in text
    assert "SUM(oi.price)" in text           # 公式
    assert "数据组-财务线" in text            # 负责人
    assert "v3" in text                      # 版本
    assert "语义层确定性编译" in text          # 访问路径
    assert "复现方式" in text and "SELECT" in text
    assert out["report"] == str(rp)


def test_report_includes_results_table(tmpdir_clean):
    rp = tmpdir_clean / "r.md"
    deliver.export_report(rp, "各州GMV", _fake_answer())
    text = rp.read_text(encoding="utf-8")
    assert "| gmv | state |" in text
    assert "RJ" in text


def test_report_marks_uncertified_fallback(tmpdir_clean):
    """口径外降级的结果必须明确标注"未经口径认证"，不能与认证结果长得一样。"""
    rp = tmpdir_clean / "r.md"
    deliver.export_report(rp, "每个客服响应时长", _fake_answer(
        path="fallback", certified=False, compile={}))
    text = rp.read_text(encoding="utf-8")
    assert "未经口径认证" in text


def test_report_includes_attribution_and_drill(tmpdir_clean):
    rp = tmpdir_clean / "r.md"
    deliver.export_report(rp, "各州GMV", _fake_answer(
        attribution={"top_contributors": [{"desc": "州「SP」变化-70.00", "pct_of_change": 1.0}]},
        attribution_summary="GMV 下跌主要来自 SP。",
        drill={"path_desc": "state=SP", "top_contributors": [{"desc": "品类「alimentos」变化-50.00"}]},
        drill_suggestion={"dim": "category", "value": "alimentos"},
        hitl_id="abc123"))
    text = rp.read_text(encoding="utf-8")
    assert "波动归因" in text and "SP" in text
    assert "下钻分析" in text
    assert "可继续下钻" in text
    assert "abc123" in text                  # 异常已推送负责人


# ---------------- CSV ----------------

def test_csv_export_roundtrip(tmpdir_clean):
    import csv
    p = tmpdir_clean / "d.csv"
    deliver.export_csv(p, ["gmv", "state"], [[80.0, "RJ"], [0.0, "SP"]])
    rows = list(csv.reader(open(p, encoding="utf-8-sig")))
    assert rows[0] == ["gmv", "state"]
    assert rows[1] == ["80.0", "RJ"]
    assert len(rows) == 3


def test_export_report_also_writes_csv(tmpdir_clean):
    out = deliver.export_report(tmpdir_clean / "r.md", "各州GMV", _fake_answer())
    assert "csv" in out
    from pathlib import Path
    assert Path(out["csv"]).exists()


# ---------------- 订阅 ----------------

def test_subscription_crud(tmpdir_clean):
    sp = tmpdir_clean / "subs.json"
    s = deliver.add_subscription("gmv", 0.05, owner="财务线", time_spec="本月", path=sp)
    assert s["id"] and len(deliver.load_subscriptions(sp)) == 1
    assert deliver.remove_subscription(s["id"], sp) is True
    assert deliver.load_subscriptions(sp) == []
    assert deliver.remove_subscription("nope", sp) is False


def test_check_subscription_alerts_on_abnormal(db, tmpdir_clean):
    """8 月 vs 9 月 GMV 明显下跌 → 订阅应产出 alert。"""
    sp = tmpdir_clean / "subs.json"
    deliver.add_subscription("gmv", 0.05, owner="财务线", time_spec="本月", path=sp)
    alerts = deliver.check_subscriptions(CFG, db, path=sp)
    assert len(alerts) == 1
    assert alerts[0]["status"] == "alert"
    assert alerts[0]["change_pct"] < 0
    assert alerts[0]["owner"] == "财务线"


def test_check_subscription_ok_below_threshold(db, tmpdir_clean):
    """阈值设得极高 → 不应告警（避免告警疲劳）。"""
    sp = tmpdir_clean / "subs.json"
    deliver.add_subscription("gmv", 10.0, time_spec="本月", path=sp)
    alerts = deliver.check_subscriptions(CFG, db, path=sp)
    assert alerts[0]["status"] == "ok"


def test_check_subscription_unknown_metric_is_reported(db, tmpdir_clean):
    """未知指标不应静默跳过——要显式报 skipped，方便排查配置。"""
    sp = tmpdir_clean / "subs.json"
    deliver.save_subscriptions([{"id": "x", "metric": "no_such", "threshold_pct": 0.05}], sp)
    alerts = deliver.check_subscriptions(CFG, db, path=sp)
    assert alerts[0]["status"] == "skipped"
    assert "no_such" in alerts[0]["reason"]


def test_subscriptions_share_threshold_semantics_with_attribution(db, tmpdir_clean):
    """订阅告警与人工归因必须用同一套判定，避免两处口径不一致。"""
    from sqlpa.business import attribution
    sp = tmpdir_clean / "subs.json"
    deliver.add_subscription("gmv", 0.05, time_spec="本月", path=sp)
    alert = deliver.check_subscriptions(CFG, db, path=sp)[0]
    r = attribution.analyze(CFG, db, "gmv", current_spec="本月", threshold_pct=0.05)
    assert alert["change_pct"] == r["change_pct"]
    assert alert["status"] == ("alert" if r["is_abnormal"] else "ok")
