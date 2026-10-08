"""归因下钻（多轮分析）测试。

"逐层下钻"是真实业务分析的核心形态：
    GMV 跌了 → 主因是州 SP → 那 SP 为什么跌？按品类拆 → …
本文件锁定该链路：下钻沿路径叠加过滤、可继续拆的维度会排除已用维度、
且不会把用户反复导向同一个维度（死循环防护）。
"""
from __future__ import annotations

import sqlite3

import pytest

from sqlpa.business.attribution import (
    analyze,
    drill,
    next_drill_suggestion,
    summarize,
)
from sqlpa.business.metric_config import load_config

CFG = load_config()


@pytest.fixture
def db(tmpdir_clean, business_months):
    """上月(GMV150) vs 本月(GMV80)：SP 跌、品类 alimentos 跌，便于逐层下钻。

    日期用 business_months 动态锚点，与「本月」语义对齐，避免日期炸弹。
    """
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


# ---------------- 第一层归因 ----------------

def test_analyze_finds_top_contributor(db):
    r = analyze(CFG, db, "gmv", current_spec="本月", dims=["state", "category"],
                threshold_pct=0.0)
    assert r["ok"] and r["is_abnormal"]
    contribs = r["top_contributors"]
    assert contribs, "应给出主要贡献维度"
    assert contribs[0]["dim"] in ("state", "category")


def test_next_drill_suggestion_points_at_top_contributor(db):
    r = analyze(CFG, db, "gmv", current_spec="本月", dims=["state"], threshold_pct=0.0)
    sug = next_drill_suggestion(r)
    assert sug and sug["dim"] == "state" and sug["value"] == "SP"


def test_no_suggestion_when_no_contributors(db):
    r = {"ok": True, "top_contributors": []}
    assert next_drill_suggestion(r) is None


# ---------------- 第二层：沿路径下钻 ----------------

def test_drill_applies_path_filter(db):
    """下钻必须把上一层（dim=state, value=SP）变成过滤条件，作用到查询上。"""
    d = drill(CFG, db, "gmv", current_spec="本月",
              path=[{"dim": "state", "value": "SP"}])
    assert d["ok"]
    assert d["path_desc"] == "state=SP"
    # SP 在当月只有 0 值（9 月只有 RJ 下单），故各维度当期应为 0 或空
    for dim_block in d["dims"]:
        for v in dim_block["current"].values():
            assert float(v or 0) == 0.0


def test_drill_excludes_already_used_dimension(db):
    """已用于下钻的维度不应再出现在"可继续拆"的候选里。"""
    d = drill(CFG, db, "gmv", current_spec="本月",
              path=[{"dim": "state", "value": "SP"}])
    assert "state" not in d["next_dims"]


def test_drill_without_path_is_global(db):
    d = drill(CFG, db, "gmv", current_spec="本月", path=[])
    assert d["ok"] and d["path_desc"] == "（全局）"


def test_drill_returns_no_candidates_when_all_dims_used(db):
    """所有支持维度都拆过 → 明确说明"无可继续拆解"，而不是返回空得不明不白。"""
    all_dims = ["dt", "state", "category", "status"]
    d = drill(CFG, db, "gmv", current_spec="本月", path=[],
              next_dims=[])
    # 显式传 next_dims=[] 时没有候选
    assert d["ok"] and d["next_dims"] == []
    assert d.get("note")


# ---------------- 汇总话术 ----------------

def test_summarize_is_readable_without_llm(db):
    r = analyze(CFG, db, "gmv", current_spec="本月", dims=["state"], threshold_pct=0.0)
    text = summarize(r)
    assert "GMV" in text and "波动" in text
    assert "SQL" not in text          # 面向业务，不提 SQL


# ---------------- service 层：追问自动下钻 ----------------

def test_service_infers_drill_on_followup(db, tmpdir_clean):
    """追问语气（"那…为什么"）应被识别为下钻请求，且沿上一层建议的维度取值。"""
    from sqlpa.business.service import _infer_drill_path

    extra = {"drill_suggestion": {"dim": "state", "value": "SP"}}
    path = _infer_drill_path("那 SP 为什么跌", [], extra)
    assert path == [{"dim": "state", "value": "SP"}]
    # 提到取值即视为追问
    assert _infer_drill_path("看看 SP", [], extra) == path
    # 普通新问题不应触发下钻
    assert _infer_drill_path("各个品类的GMV", [], extra) == []


def test_service_no_drill_without_suggestion():
    from sqlpa.business.service import _infer_drill_path
    assert _infer_drill_path("那为什么跌", [], {}) == []


def test_service_uses_history_suggestion():
    """建议也可来自历史记录（UI 把上一轮结果放进 history）。"""
    from sqlpa.business.service import _infer_drill_path
    hist = [{"question": "各州GMV", "drill_suggestion": {"dim": "category", "value": "alimentos"}}]
    assert _infer_drill_path("那 alimentos 呢", hist, {}) == [
        {"dim": "category", "value": "alimentos"}]
