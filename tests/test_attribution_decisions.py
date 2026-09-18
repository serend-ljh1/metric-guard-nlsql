"""归因决策层（真 Agent）测试。

锁定"把下一步拆哪儿"从确定性规则升级为 Agent 决策这一层：
  - decide_drill：无追问→none；主因清晰且追问→沿主因下钻；贡献分散→主动换维度；
                  有 LLM 时可返回 factorize / 自定义下钻路径，且来源记为 llm。
  - factorize：GMV=订单量×客单价 的乘法因子分解，量化"跌来自单量还是客单价"。
  - 决策质量评测（evaluation.eval_decisions）：规则基线 2/3，接 LLM 可到 3/3，
    证明"Agent 决策"确实带来可用准确率衡量的提升。
全部离线（MockLLM/规则兜底），不碰真实 API。
"""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from sqlpa.business.attribution import decide_drill, factorize
from sqlpa.business.metric_config import load_config
from sqlpa.business.service import answer
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

CFG = load_config()


@pytest.fixture
def db(tmpdir_clean):
    """8 月(GMV150=2单×75) vs 9 月(GMV80=1单×80)：既支持维度归因也支持因子分解。"""
    p = tmpdir_clean / "decide.db"
    con = sqlite3.connect(p)
    con.executescript("""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','2026-08-01','delivered'),('o2','c1','2026-08-05','delivered'),
        ('o3','c2','2026-09-01','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """)
    con.commit()
    return con


class _Oracle:
    """模拟 LLM：按预设返回 JSON 决策。"""
    def __init__(self, text): self.text, self.calls = text, 0
    def complete(self, prompt):
        self.calls += 1
        return self.text


def _att(*, contribs, **kw):
    base = {"ok": True, "is_abnormal": True, "current_spec": "本月", "current_total": 80,
            "previous_total": 150, "change": -70.0, "change_pct": -0.467,
            "dims": [], "top_contributors": list(contribs)}
    base.update(kw)
    return base


# ---------------- decide_drill：规则兜底 ----------------

def test_none_without_followup():
    d = decide_drill(_att(contribs=[{"dim": "category", "key": "a", "desc": "品类「a」变化-1",
                                     "pct_of_change": 0.5}]), "各个品类的GMV是多少")
    assert d["action"] == "none" and d["decision_source"] == "rule"


def test_drill_clear_contributor_on_followup():
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00",
                                     "pct_of_change": 0.9}]), "那为什么单量少了？")
    assert d["action"] == "drill"
    assert d["path"] == [{"dim": "state", "value": "SP"}]


def test_switch_dim_when_scattered():
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "delta": -30,
                                     "desc": "州「SP」变化-30", "pct_of_change": 0.30},
                                    {"dim": "category", "key": "a", "delta": -28,
                                     "desc": "品类「a」变化-28", "pct_of_change": 0.28}]),
                     "那为什么呢？")
    assert d["action"] == "switch_dim" and d["dim"]


# ---------------- decide_drill：LLM 决策 ----------------

def test_llm_factorize_decision():
    oracle = _Oracle('{"action":"factorize","reason":"GMV疑由单量×客单价构成"}')
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00",
                                     "pct_of_change": 0.9}]), "那为什么跌？", llm=oracle)
    assert oracle.calls >= 1
    assert d["action"] == "factorize" and d["decision_source"] == "llm"


def test_llm_drill_uses_agent_chosen_value():
    oracle = _Oracle('{"action":"drill","dim":"category","value":"alimentos","reason":"按品类追"}')
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00",
                                     "pct_of_change": 0.9}]), "那为什么呢？", llm=oracle)
    assert d["action"] == "drill"
    assert d["path"] == [{"dim": "category", "value": "alimentos"}]


def test_llm_bad_json_falls_back_to_rule():
    oracle = _Oracle("这不是JSON")
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00",
                                     "pct_of_change": 0.9}]), "那为什么单量少了？", llm=oracle)
    assert d["decision_source"] == "rule"          # LLM 返回非法 → 程序不炸，回退规则


# ---------------- factorize：单量×客单价分解 ----------------

def test_factorize_decomposes_gmv(db):
    f = factorize(CFG, db, "gmv", current_spec="本月")
    assert f["ok"] and f["formula"] == "订单量 × 客单价"
    assert len(f["factors"]) == 2
    by = {x["factor"]: x for x in f["factors"]}
    assert by["order_count"]["current"] == 1 and by["order_count"]["previous"] == 2
    assert f["main_factor"] == "order_count"       # 主因是单量减少，不是客单价
    # 三部分分摊之和约等于总变动（±1 舍入容差）
    total = sum(x["contribution"] for x in f["factors"]) + f["interaction"]["contribution"]
    assert abs(total - f["change"]) < 1.0


def test_factorize_unknown_metric():
    f = factorize(CFG, sqlite3.connect(":memory:"), "aov", current_spec="本月")
    assert not f["ok"] and "未定义乘法因子" in f["reason"]


# ---------------- service 层接线：factorize 决策落地 ----------------

def test_service_factorize_wired(db, tmpdir_clean):
    """service.answer 用 decide_drill 决策出 factorize 时，应产出 factor_split 落到返回。"""
    p = tmpdir_clean / "svc.db"
    con = sqlite3.connect(p); con.executescript("""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','2026-08-01','delivered'),('o2','c1','2026-08-05','delivered'),
        ('o3','c2','2026-09-01','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """); con.commit(); con.close()
    sb = SqlSandbox(str(p), ExecConfig(max_rows=2000))
    with patch("sqlpa.business.attribution.decide_drill",
               return_value={"action": "factorize", "reason": "疑为单量×客单价", "decision_source": "llm"}):
        a = answer("本月GMV", load_config(), sb, str(p), llm=None)
    assert a["ok"]
    assert a["drill_decision"]["action"] == "factorize"
    assert a["factor_split"]["ok"] is True and a["factor_split"]["main_factor"] == "order_count"
    # Supervisor：命中语义层 + 归因 Agent 判定 factorize → 路由升级为 drill
    assert a["supervisor"]["decision"] == "drill"
    assert a["supervisor"]["reason"]


# ---------------- 决策质量评测（① 决策命中） ----------------

class _OracleSeq:
    """按调用顺序返回预设 JSON 决策，模拟 LLM（第 i 次调用回第 i 个回复）。"""
    def __init__(self, texts): self.texts, self.calls = list(texts), 0
    def complete(self, prompt):
        self.calls += 1
        return self.texts[min(self.calls - 1, len(self.texts) - 1)]


def test_eval_decision_rule_baseline():
    """规则兜底基线：纯规则永远选不出 factorize，故明显低于 LLM oracle。"""
    from evaluation.eval_decisions import run, ANNOTATED, action_tally, EXPERT_LABELED_BY
    # 每种动作用例数 ≥ 5（答辩要求：覆盖 4 种 action，不能平均不足一条）
    tally = action_tally(ANNOTATED)
    for a in ("drill", "switch_dim", "factorize", "none"):
        assert tally.get(a, 0) >= 5, f"action {a} 只有 {tally.get(a, 0)} 条"
    r = run(ANNOTATED, llm=None)
    assert r["total"] >= 20
    assert r["decision_accuracy"] == pytest.approx(19 / r["total"], abs=1e-3)
    assert 0.6 < r["decision_accuracy"] < 0.9      # 规则 ≈2/3，够看但不到家
    assert r["expert_labeled_by"] == EXPERT_LABELED_BY   # 标注协议：真值由谁标


def test_eval_decision_llm_lifts_accuracy():
    """接 LLM(oracle) 应全中 → 决策命中 1.0，且明显高于纯规则基线（区分度）。"""
    from evaluation.eval_decisions import run, ANNOTATED
    replies = {"drill": '{"action":"drill","dim":"state","value":"SP","reason":"r"}',
               "switch_dim": '{"action":"switch_dim","dim":"category","reason":"r"}',
               "factorize": '{"action":"factorize","reason":"r"}',
               "none": '{"action":"none","reason":"r"}'}
    oracle = _OracleSeq([replies[c["expert_action"]] for c in ANNOTATED])
    r = run(ANNOTATED, llm=oracle)
    assert oracle.calls == len(ANNOTATED)          # 每条用例各问一次 LLM
    assert r["llm_used"] is True
    assert r["decision_accuracy"] == 1.0
    base = run(ANNOTATED, llm=None)                 # 纯规则基线，证明提升确实存在
    assert base["decision_accuracy"] < r["decision_accuracy"]


# ---------------- 执行正确性评测（③ exec_accuracy） ----------------

def test_eval_exec_accuracy(tmpdir_clean):
    """exec_accuracy 与决策命中脱钩：只验'做完之后算得对不对'。
    factorize 分摊守恒/share≈1/主因方向；drill 沿主因下钻后分支内收敛。"""
    from evaluation.eval_decisions import run, ANNOTATED
    r = run(ANNOTATED, llm=None, tmp_dir=tmpdir_clean)
    e = r["exec"]
    assert e["total"] >= 3
    by = {c["check"]: c["ok"] for c in e["checks"]}
    assert by["factorize-分摊守恒"] is True
    assert by["factorize-份额合计≈1"] is True
    assert by["factorize-主因方向合理"] is True      # 样例主因是量(订单量)减少，不是价
    assert by["drill-主因隔离收敛"] is True
    assert e["exec_accuracy"] == 1.0
    assert "exec" in r and "decision_accuracy" in r   # 两个指标分开，不混