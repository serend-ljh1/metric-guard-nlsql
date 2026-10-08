"""归因评测的"尺子"回归：命中判定必须来自**评测侧独立真值表**，而非被测引擎的自报。

背景（本轮评审发现的致命自指）：旧实现 `_delta_rank(result,...)` 从被测引擎自己算出的
`result["dims"][dim]["delta"]` 取排名，于是"命中"退化成"在引擎自己的表里排第几"。
实测连"delta 表里只放注入段"的假引擎都会被判 rank=1/命中 —— 该评测发现不了引擎算错。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load_eval():
    spec = importlib.util.spec_from_file_location(
        "eval_attribution_mod", _ROOT / "evaluation" / "eval_attribution.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod


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


def _build_db(path: Path) -> Path:
    """上期 SP=1000 / RJ=500 / MG=100；当期 SP=1200 / RJ=500 / MG=100。

    注入 MG ×0.4（当期）后：MG delta=-60，而 SP delta=+200 最大 →
    **MG 不是真值表的 top-1**，因此真值口径下不应算命中。
    """
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    rows = [("SP", "2018-01", 1000.0, "o1", "c1"), ("RJ", "2018-01", 500.0, "o2", "c2"),
            ("MG", "2018-01", 100.0, "o3", "c3"),
            ("SP", "2018-02", 1200.0, "o4", "c1"), ("RJ", "2018-02", 500.0, "o5", "c2"),
            ("MG", "2018-02", 100.0, "o6", "c3")]
    for state, month, price, oid, cid in rows:
        ts = f"{month}-10 10:00:00"
        conn.execute("INSERT INTO orders VALUES(?,?,?,?,NULL,NULL)", (oid, cid, "delivered", ts))
        conn.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)", (oid, 1, "p1", "s1", price, 0.0))
    for cid, state in (("c1", "SP"), ("c2", "RJ"), ("c3", "MG")):
        conn.execute("INSERT INTO customers VALUES(?,?,?,?,?)", (cid, cid, "0", "x", state))
    conn.execute("INSERT INTO products VALUES('p1','catA')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def eval_mod():
    return _load_eval()


@pytest.fixture
def real_db(tmpdir_clean):
    return str(_build_db(tmpdir_clean / "eval_truth.db"))


@pytest.fixture
def cfg():
    from sqlpa.business.metric_config import load_config
    return load_config()


def test_truth_delta_table_matches_hand_computation(eval_mod, real_db, tmpdir_clean):
    """真值表 = 评测侧 SQL 复算，且与手工计算一致（当期-上期，缺失按 0）。"""
    work = tmpdir_clean / "w.db"
    import shutil
    shutil.copyfile(real_db, work)
    conn = sqlite3.connect(work)
    eval_mod._INJECT_FN["state"]("MG", "'2018-02-01'", "'2018-03-01'", 0.4, conn)
    conn.commit()
    table = eval_mod._truth_delta_table(conn, "state", "2018-02", "2018-01")
    conn.close()
    assert table["SP"] == pytest.approx(200.0)      # 1200-1000
    assert table["RJ"] == pytest.approx(0.0)        # 500-500
    assert table["MG"] == pytest.approx(-60.0)      # 100*0.4-100
    # |delta| 排名：SP(200) > MG(60) > RJ(0)
    assert eval_mod._rank_in(table, "SP")[0] == 1
    assert eval_mod._rank_in(table, "MG")[0] == 2
    assert eval_mod._rank_in(table, "nope")[0] == len(table) + 1


def test_hit_comes_from_truth_not_from_engine_selfreport(eval_mod, real_db, cfg, monkeypatch):
    """**核心回归**：引擎自报"注入段是 top-1"时，只要真值表不同意，就不算命中。"""
    from sqlpa.business import attribution

    # 假引擎：delta 表里只放注入段 → 旧口径必然判 rank=1/命中
    def fake_analyze(*_a, **_k):
        return {"ok": True, "is_abnormal": True, "change_pct": -0.5,
                "dims": [{"dim": "state", "delta": {"MG": -999.0}}],
                "top_contributors": [{"dim": "state", "key": "MG", "delta": -999.0}]}

    monkeypatch.setattr(attribution, "analyze", fake_analyze)
    r = eval_mod._run_scenario(cfg, real_db, "state", "MG", "2018-02", "2018-01", 0.4)

    assert r["engine_rank"] == 1 and r["engine_hit"] is True, "前提：假引擎在自报 top-1"
    assert r["rank"] == 2, f"真值表里 MG 应排第 2，实际 {r['rank']}"
    assert r["hit"] is False, "命中判定必须来自真值表，不能被引擎自报带偏"
    assert r["delta_table_match"] is False, "引擎表只有一段，必然与真值表不一致"
    assert r["delta_table_max_abs_diff"] > 0


def test_honest_engine_still_scores_hits(eval_mod, real_db, cfg):
    """真引擎（未打桩）在真值口径下照常判命中：改造没有把评测变成"永远不命中"。"""
    r = eval_mod._run_scenario(cfg, real_db, "state", "SP", "2018-02", "2018-01", 0.4)
    assert r["is_abnormal"] is True
    assert r["rank"] == 1 and r["hit"] is True
    assert r["delta_table_match"] is True
    assert r["delta_table_max_abs_diff"] == 0.0
    # 两个口径一致时，自评与真值应给出相同结论
    assert r["engine_hit"] is True


def test_wrong_dim_uses_truth_tables(eval_mod, real_db, cfg):
    """维度误报也按真值表判定（而不是引擎的 top_contributors）。"""
    r = eval_mod._run_scenario(cfg, real_db, "state", "MG", "2018-02", "2018-01", 0.4)
    g = r["global_top"] or {}
    assert g.get("dim") == "state" and g.get("key") == "SP", g
    assert r["dim_dominant"] is False


# ---------------------------------------------------------------- LLM 基线的公平性

def _load_baseline():
    spec = importlib.util.spec_from_file_location(
        "eval_llm_baseline_mod",
        _ROOT / "evaluation" / "eval_attribution_llm_baseline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod


def test_llm_table_uses_same_universe_and_where_as_totals(tmpdir_clean):
    """**公平性回归**：喂给 LLM 的分段表必须与合计同口径，且含"当期塌成 0"的段。

    旧实现的两个偏差：表加 `frag IS NOT NULL` 而合计没有（分母不一致）；
    表只含"当期存在"的段 → 天然塌方归零的主因对 LLM 不可见（信息不对等）。
    """
    bl = _load_baseline()
    p = _build_db(tmpdir_clean / "llm_fair.db")
    # 造一个"上期有、当期没有"的段：上期增加一个州 XX
    conn = sqlite3.connect(p)
    conn.execute("INSERT INTO orders VALUES('o9','c9','delivered','2018-01-20 10:00:00',NULL,NULL)")
    conn.execute("INSERT INTO order_items VALUES('o9',1,'p1','s1',400.0,0.0)")
    conn.execute("INSERT INTO customers VALUES('c9','c9','0','x','XX')")
    conn.commit()
    conn.close()

    rows, totals, sums_match = bl._inject_and_query(
        str(p), "state", "MG", "2018-02", 0.4, 0)
    labels = {r[0]: (r[1], r[2]) for r in rows}
    assert "XX" in labels, "当期塌成 0 的段必须出现在表里（否则 LLM 看不到这类主因）"
    assert labels["XX"][0] == 0 and labels["XX"][1] == 400.0
    assert sums_match is True, "表内合计必须等于总合计（两臂同 WHERE/同分母）"
    # prompt 必须显式给出"变动量 = 本月 − 上月"，不能靠猜
    prompt = bl._prompt("state", rows, totals)
    assert "本月 − 上月" in prompt or "本月-上月" in prompt
    assert "绝对值最大" in prompt
