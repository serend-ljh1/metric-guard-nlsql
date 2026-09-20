"""归因可加性回归 —— "维度贡献"对比率类指标在数学上不成立，必须自动降级。

真实反例（AOV 按州，本文件用自建库精确复现）：
    上期 SP 2 单×50 = 100、RJ 1 单×50 = 50  → AOV = 50
    当期 SP 3 单×50 = 110、RJ 1 单×40 = 40  → AOV = 37.5
    分段 delta：SP +10、RJ −10（合计 0），而真实变化是 −12.5。
旧实现在这里会输出"主因 SP 占波动 −80%"，并把它写进结论、Markdown 报告与告警。
"""
from __future__ import annotations

import sqlite3

import pytest

from sqlpa.business.attribution import analyze, is_additive
from sqlpa.business.metric_config import load_config
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

CFG = load_config()

_SCHEMA = """
CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT, order_status TEXT,
  order_purchase_timestamp TEXT, order_delivered_customer_date TEXT,
  order_estimated_delivery_date TEXT);
CREATE TABLE order_items(order_id TEXT, order_item_id INTEGER, product_id TEXT,
  seller_id TEXT, price REAL, freight_value REAL);
CREATE TABLE customers(customer_id TEXT PRIMARY KEY, customer_unique_id TEXT,
  customer_zip_code_prefix TEXT, customer_city TEXT, customer_state TEXT);
CREATE TABLE products(product_id TEXT PRIMARY KEY, product_category_name TEXT);
CREATE TABLE reviews(review_id TEXT, order_id TEXT, review_score REAL);
"""

# 2018-01（上期）与 2018-02（当期）：恰好构造出"分段之和 = 0、总变化 ≠ 0"
_ROWS = """
INSERT INTO orders VALUES
  ('p1','c1','delivered','2018-01-10',NULL,NULL),
  ('p2','c1','delivered','2018-01-11',NULL,NULL),
  ('p3','c2','delivered','2018-01-12',NULL,NULL),
  ('c1','c1','delivered','2018-02-10',NULL,NULL),
  ('c2','c1','delivered','2018-02-11',NULL,NULL),
  ('c3','c1','delivered','2018-02-12',NULL,NULL),
  ('c4','c2','delivered','2018-02-13',NULL,NULL);
INSERT INTO order_items VALUES
  ('p1',1,'x','s',50.0,0.0),('p2',1,'x','s',50.0,0.0),('p3',1,'x','s',50.0,0.0),
  ('c1',1,'x','s',50.0,0.0),('c2',1,'x','s',50.0,0.0),('c3',1,'x','s',50.0,0.0),
  ('c4',1,'x','s',40.0,0.0);
INSERT INTO customers VALUES ('c1','u1','1','sp','SP'),('c2','u2','2','rj','RJ');
INSERT INTO products VALUES ('x','catA');
"""


@pytest.fixture
def aov_db(tmpdir_clean) -> str:
    p = tmpdir_clean / "aov.db"
    conn = sqlite3.connect(p)
    conn.executescript(_SCHEMA)
    conn.executescript(_ROWS)
    conn.commit()
    conn.close()
    return str(p)


def _sb(path):
    return SqlSandbox(path, ExecConfig(max_rows=100))


def test_aov_is_not_additive():
    assert is_additive(CFG.metrics["aov"]) is False
    assert is_additive(CFG.metrics["gmv"]) is True


def test_ratio_metric_contribution_is_degraded(aov_db):
    """比率类指标：分段 delta 之和对不上总变化 → 不允许出现"占波动 X%"。"""
    res = analyze(CFG, sqlite3.connect(aov_db), "aov",
                  current_spec="2018-02", previous_spec="2018-01",
                  dims=["state"], threshold_pct=0.05)
    assert res["ok"] and res["is_abnormal"]
    # 真值：上期 AOV=50（3 单共 150）；当期 AOV=47.5（4 单共 190）
    # 分段：SP 的 AOV 50→50（delta 0）、RJ 50→40（delta −10）→ 合计 −10 ≠ 总变化 −2.5
    assert res["previous_total"] == pytest.approx(50.0)
    assert res["current_total"] == pytest.approx(47.5)
    assert res["change"] == pytest.approx(-2.5)
    # 降级：不给出占比，且显式标注不可靠 + 原因
    assert res["contribution_reliable"] is False
    assert res["contribution_note"]
    assert all(c["pct_of_change"] is None for c in res["top_contributors"])
    assert all(c["pct_reliable"] is False for c in res["top_contributors"])
    # 但分段对照仍然保留（这是能说的事实）
    assert res["top_contributors"], "分段对照不应被一起丢掉"


def test_additive_metric_keeps_percentage(aov_db):
    """可加指标（GMV）不受影响：守恒通过 → 占比照常给出，且分段之和 == 总变化。"""
    res = analyze(CFG, sqlite3.connect(aov_db), "gmv",
                  current_spec="2018-02", previous_spec="2018-01",
                  dims=["state"], threshold_pct=0.05)
    assert res["ok"]
    assert res["contribution_reliable"] is True
    assert "contribution_note" not in res
    chk = res["additivity_checks"]["state"]
    assert chk["additive"] is True
    assert chk["delta_sum"] == pytest.approx(res["change"])
    assert all(c["pct_reliable"] is True for c in res["top_contributors"])
    assert res["top_contributors"][0]["pct_of_change"] is not None


def test_degradation_is_per_dimension(aov_db):
    """降级按**维度**生效：坏维度不给占比，好维度照常给。

    （旧写法是"任一维度不可加就全场降级"，会让按州这种可加维度也失去有用信息。）
    """
    res = analyze(CFG, sqlite3.connect(aov_db), "aov",
                  current_spec="2018-02", previous_spec="2018-01",
                  dims=["state"], threshold_pct=0.05)
    assert res["additivity_checks"]["state"]["additive"] is False
    # GMV 同维度可加 → 不受影响
    res2 = analyze(CFG, sqlite3.connect(aov_db), "gmv",
                   current_spec="2018-02", previous_spec="2018-01",
                   dims=["state"], threshold_pct=0.05)
    assert res2["additivity_checks"]["state"]["additive"] is True
    assert all(c["pct_reliable"] for c in res2["top_contributors"])


def test_ratio_mix_decomposition_is_exact_and_conserving(aov_db):
    """比率类指标不做"占波动"，但可以给出**量价/结构分解**（rate/mix/interaction）。

    本例（AOV 按州，2018-01 → 2018-02）：
      上期 SP 2 单×50、RJ 1 单×50 → AOV 50；当期 SP 3 单×50、RJ 1 单×40 → AOV 47.5
      价格效应 = (2/3)·0 + (1/3)·(−10) = −3.3333
      结构效应 = [(3/4)−(2/3)]·50 + [(1/4)−(1/3)]·50 = 0
      交互项   = −2.5 − (−3.3333) − 0 = +0.8333
    三项之和必须严格等于总变化 —— 这是"分解不是编故事"的判据。
    """
    res = analyze(CFG, sqlite3.connect(aov_db), "aov",
                  current_spec="2018-02", previous_spec="2018-01",
                  dims=["state"], threshold_pct=0.05)
    dec = (res.get("ratio_decomposition") or {}).get("state")
    assert dec and dec["valid"] is True, dec
    assert dec["weight_metric"] == "paid_order_count"
    assert dec["rate_effect"] == pytest.approx(-3.3333, abs=1e-3)
    assert dec["mix_effect"] == pytest.approx(0.0, abs=1e-3)
    assert dec["interaction"] == pytest.approx(0.8333, abs=1e-3)
    total = dec["rate_effect"] + dec["mix_effect"] + dec["interaction"]
    assert total == pytest.approx(dec["change"], abs=1e-3), "分解三项之和必须等于总变化"


def test_ratio_decomposition_refuses_when_weight_metric_mismatches(aov_db):
    """权重指标与该比率分母不一致时必须**拒绝**给分解（而不是给出精致的错数）。"""
    from dataclasses import replace

    bad = replace(CFG.metrics["aov"], weight_metric="canceled_order_count")
    cfg2 = load_config()
    cfg2.metrics["aov_bad"] = replace(bad, key="aov_bad")
    res = analyze(cfg2, sqlite3.connect(aov_db), "aov_bad",
                  current_spec="2018-02", previous_spec="2018-01",
                  dims=["state"], threshold_pct=0.05)
    dec = (res.get("ratio_decomposition") or {}).get("state") or {}
    assert dec.get("valid") is False, "权重指标与分母不一致时必须拒绝分解"
    assert dec.get("reason"), "拒绝时要说明原因（具体措辞取决于不一致类型）"


def test_decomposition_reaches_conclusion_evidence(aov_db):
    """分解结果要出现在结论依据里，而不是只躺在中间产物。"""
    from sqlpa.analysis.orchestrator import run_analysis

    events = []
    out = run_analysis("2018-02 的客单价为什么变化？", load_config(),
                       _sb(aov_db), aov_db, None,
                       dims_override=["state"], alert_threshold_pct=0.05,
                       emit=events.append)
    details = [(e.get("type"), e.get("detail", "")) for e in (out.get("evidence") or [])]
    kinds = [t for t, _ in details]
    assert "量价分解" in kinds, f"结论依据里缺少量价分解：{kinds}"
    assert any("价格效应" in d for _, d in details)


def test_calendar_note_flags_month_length_effect(aov_db):
    """单期环比必须暴露"月份天数差异"：2 月 28 天 vs 1 月 31 天不能当成真实波动。

    这里不静默改判阈值（那会引入新的口径不透明），而是把日均口径与"天数可解释比例"
    一并给出，让使用者自己决定要不要告警。
    """
    res = analyze(CFG, sqlite3.connect(aov_db), "gmv",
                  current_spec="2018-02", previous_spec="2018-01",
                  dims=[], threshold_pct=0.05)
    cal = res.get("calendar")
    assert cal, "缺少日历口径信息"
    assert (cal["current_days"], cal["previous_days"]) == (28, 31)
    assert cal["per_day_change_pct"] != res["change_pct"]
    assert cal.get("note"), "天数差异显著时必须给出提示"


def test_calendar_note_absent_for_equal_length_periods(aov_db):
    """等长周期不该被日历提示打扰。"""
    res = analyze(CFG, sqlite3.connect(aov_db), "gmv",
                  current_spec="2018-01", previous_spec="2017-12",
                  dims=[], threshold_pct=0.05)
    cal = res.get("calendar") or {}
    if cal.get("current_days") == cal.get("previous_days"):
        assert "note" not in cal


def test_contribution_note_reaches_conclusion_evidence(aov_db):
    """降级原因必须出现在最终结论的依据里，而不是只躺在中间产物里。"""
    from sqlpa.analysis.orchestrator import run_analysis

    cfg = load_config()
    events = []
    out = run_analysis("2018-02 的客单价为什么变化？", cfg, _sb(aov_db), aov_db, None,
                       dims_override=["state"], alert_threshold_pct=0.05,
                       emit=events.append)
    att = out.get("attribution") or {}
    assert att.get("contribution_reliable") is False
    types = [e.get("type") for e in (out.get("evidence") or [])]
    assert "口径提示" in types, f"结论依据里缺少口径提示：{types}"
