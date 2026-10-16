"""告警质量评测「注入式构造真值」的尺子回归。

背景：`evaluation/eval_alert_quality.py` 的段 B 用**注入动作**定义真值
（真异常=整期缩放价格/取消部分订单；真正常=缩放使日均持平、总值只剩月长差），
据此度量假阳性率 / 假阴性率。这层逻辑一旦写错（例如把带引号的字面量当绑定参数，
或对计数类指标用价格缩放），真值就会失真，评测结论也就不可信。

这里用**自建迷你库**把三件关键事钉死，不依赖真实 Olist 数据、可离线复现：
  1. 混淆矩阵与 FP/FN 率的口径正确；
  2. `_period_total`/`_scale_period` 的"期段匹配"正确（含去引号约束）；
  3. 日历假象构造的不变量：**缩放后总值 = 上期总值 × 天数比**（即 change_pct 恒为 天数比−1）。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_SCHEMA = """
CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT, order_status TEXT,
  order_purchase_timestamp TEXT, order_delivered_customer_date TEXT,
  order_estimated_delivery_date TEXT);
CREATE TABLE order_items(order_id TEXT, order_item_id INTEGER, product_id TEXT,
  seller_id TEXT, price REAL, freight_value REAL);
"""


def _load():
    spec = importlib.util.spec_from_file_location(
        "eval_alert_quality_mod", _ROOT / "evaluation" / "eval_alert_quality.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod


@pytest.fixture
def mod():
    return _load()


def test_confusion_matrix_and_rates(mod):
    """阈值-only 与门控两套系统的 TP/FP/TN/FN 与比率口径必须正确。"""
    cases = [
        {"threshold_alert": True, "gated_alert": True, "truth_abnormal": True},    # TP(阈值)/TP(门控)
        {"threshold_alert": False, "gated_alert": False, "truth_abnormal": True},  # FN(阈值)/FN(门控)
        {"threshold_alert": True, "gated_alert": False, "truth_abnormal": False},  # FP(阈值)/TN(门控)
        {"threshold_alert": False, "gated_alert": False, "truth_abnormal": False},  # TN(阈值)/TN(门控)
    ]
    a = mod._confusion(cases, "threshold_alert")
    assert (a["tp"], a["fp"], a["tn"], a["fn"]) == (1, 1, 1, 1)
    assert a["recall"] == 0.5              # 真异常只抓到 1/2
    assert a["false_positive_rate"] == 0.5  # 真正常里误报 1/2
    g = mod._confusion(cases, "gated_alert")
    assert g["fn"] == 1                    # 门控没造成额外假阴
    assert g["false_positive_rate"] == 0.0  # 门控把假阳压到 0
    assert g["recall"] == 0.5


def test_days_in_month_and_unquote(mod):
    assert mod._days_in_month("2017-02") == 28
    assert mod._days_in_month("2018-02") == 28
    assert mod._days_in_month("2017-03") == 31
    assert mod._unq("'2018-02-01'") == "2018-02-01"


def _build(path: Path) -> Path:
    """上期 2017-01（31 天）：2 单各 100 = 200；当期 2017-02（28 天）：2 单各 200 = 400。"""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    for oid, ts, price in (("o1", "2017-01-10 10:00:00", 100.0),
                           ("o2", "2017-01-20 10:00:00", 100.0),
                           ("o3", "2017-02-10 10:00:00", 200.0),
                           ("o4", "2017-02-20 10:00:00", 200.0)):
        conn.execute("INSERT INTO orders VALUES(?,?,?,?,NULL,NULL)", (oid, "c1", "delivered", ts))
        conn.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)", (oid, 1, "p1", "s1", price, 0.0))
    conn.commit()
    conn.close()
    return path


def test_period_total_and_scale_are_period_scoped(mod, tmpdir_clean):
    """`_period_total` 只统计期段内、非取消行；`_scale_period` 不误伤相邻期。"""
    p = _build(tmpdir_clean / "alert_scope.db")
    conn = sqlite3.connect(p)
    assert mod._period_total(conn, "2017-01-01", "2017-02-01") == pytest.approx(200.0)
    assert mod._period_total(conn, "2017-02-01", "2017-03-01") == pytest.approx(400.0)
    mod._scale_period(conn, "2017-02-01", "2017-03-01", 0.5)
    conn.commit()
    # 当期减半，上期不动
    assert mod._period_total(conn, "2017-02-01", "2017-03-01") == pytest.approx(200.0)
    assert mod._period_total(conn, "2017-01-01", "2017-02-01") == pytest.approx(200.0)
    conn.close()


def test_calendar_construction_leaves_only_day_count_gap(mod, tmpdir_clean):
    """**核心不变量**：日历假象构造后，总值只随天数比变化（change_pct = 天数比−1）。"""
    p = _build(tmpdir_clean / "alert_calendar.db")
    conn = sqlite3.connect(p)
    prev_lo, prev_hi = "2017-01-01", "2017-02-01"
    cur_lo, cur_hi = "2017-02-01", "2017-03-01"
    pt = mod._period_total(conn, prev_lo, prev_hi)
    ct = mod._period_total(conn, cur_lo, cur_hi)
    pd = mod._days_in_month("2017-01")     # 31
    cd = mod._days_in_month("2017-02")     # 28
    factor = (pt / pd) / (ct / cd)
    mod._scale_period(conn, cur_lo, cur_hi, factor)
    conn.commit()
    new_ct = mod._period_total(conn, cur_lo, cur_hi)
    conn.close()
    assert new_ct == pytest.approx(pt * cd / pd)
    assert (new_ct / pt - 1) == pytest.approx(cd / pd - 1)   # ≈ −9.68% = 28/31 − 1


def test_cancel_fraction_drops_count_metrics(mod, tmpdir_clean):
    """取消构造把计数类指标按比例顶下去（对计数指标必须用取消、而非价格缩放）。"""
    p = tmpdir_clean / "alert_cancel.db"
    conn = sqlite3.connect(p)
    conn.executescript(_SCHEMA)
    for i in range(10):
        oid = f"o{i}"
        conn.execute("INSERT INTO orders VALUES(?,?,?,?,NULL,NULL)",
                     (oid, "c1", "delivered", f"2017-02-{i + 1:02d} 10:00:00"))
        conn.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)", (oid, 1, "p1", "s1", 10.0, 0.0))
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM orders WHERE order_status='delivered'").fetchone()[0]
    mod._cancel_fraction(conn, "2017-02-01", "2017-03-01", 0.4)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM orders WHERE order_status='delivered'").fetchone()[0]
    canceled = conn.execute("SELECT COUNT(*) FROM orders WHERE order_status='canceled'").fetchone()[0]
    conn.close()
    assert before == 10
    assert canceled == 4 and after == 6     # rowid%10<4 → 确定性 4 条
