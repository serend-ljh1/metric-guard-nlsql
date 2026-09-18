"""
tests/test_service_governance_link.py
=====================================
打通"取数 → 口径可追溯 → 波动归因 → 异常闭环"端到端链路的测试。

验证 service.answer() 在口径内成功分支自动附带治理面结果：
  - 恒附加指标口径解释 metric_explain；
  - 命中时间过滤时做异常归因 attribution + LLM/确定性的归因总结 attribution_summary；
  - 归因判定异常时自动入 HITL 闭环（hitl_id 非空、队列文件落盘）。

全部离线（llm=None 走确定性组装，MockLLM/打桩不碰真实 API）。
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from sqlpa.business.attribution import summarize
from sqlpa.business.metric_config import load_config
from sqlpa.business.service import answer
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox


def _build_db(tmpdir_clean) -> str:
    """造一个小 olist 库，8 月(GMV150) vs 9 月(GMV80) → 明显下跌，触发归因。"""
    p = tmpdir_clean / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript("""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','2026-08-10','delivered'),('o2','c1','2026-08-15','delivered'),
        ('o3','c2','2026-09-05','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """)
    conn.commit()
    conn.close()
    return str(p)


@pytest.fixture
def db(tmpdir_clean):
    return _build_db(tmpdir_clean)


def _answer(db, question="本月各个品类的GMV", hitl_path=None):
    sb = SqlSandbox(db, ExecConfig(max_rows=2000))
    return answer(question, load_config(), sb, db, llm=None, hitl_path=hitl_path)


def test_in_scope_attaches_explain_and_attribution(db):
    """口径内成功分支应自动附带口径解释 + 异常归因 + 人话点评 + Supervisor 路由。"""
    a = _answer(db)
    assert a["matched"] and a["mode"] == "metric" and a["ok"]
    # Supervisor：语义层优先 → direct（无追问不叠加 drill）
    assert a["supervisor"]["decision"] == "direct"
    assert a["supervisor"]["reason"]            # 决策理由对用户可见
    assert "metric_explain" in a and a["metric_explain"]["ok"] is True
    assert a["metric_explain"]["metric_expr"] == "SUM(oi.price)"   # 口径可追溯
    assert "attribution" in a and a["attribution"]["ok"] is True
    assert "attribution_summary" in a and a["attribution_summary"].strip()


def test_attribution_abnormal_auto_hitl_loop(db, tmpdir_clean):
    """归因判异常应自动入 HITL 闭环：hitl_id 非空、队列文件落盘。"""
    fake_abnormal = {
        "ok": True, "is_abnormal": True, "metric": "gmv",
        "metric_name": "GMV成交总额", "change": -70.0, "change_pct": -0.467,
        "current_total": 80.0, "previous_total": 150.0,
        "current_spec": "本月", "previous_spec": "上月",
        "dims": [{"dim": "category", "current": {}, "previous": {}, "delta": {}}],
        "top_contributors": [{"dim": "category", "key": "alimentos",
                              "delta": -70.0, "pct_of_change": 1.0,
                              "desc": "品类「alimentos」变化-70.00"}],
    }
    hitl_file = tmpdir_clean / "hitl.jsonl"
    with patch("sqlpa.business.attribution.analyze", return_value=fake_abnormal):
        a = _answer(db, hitl_path=str(hitl_file))
    assert a["ok"]
    assert a["attribution"]["is_abnormal"] is True
    assert a["hitl_id"]
    assert hitl_file.exists()
    payload = open(hitl_file, encoding="utf-8").read()
    assert 'GMV' in payload                       # 异常描述里带指标名
    assert '数据组-财务线' in payload               # 自动关联负责人


def test_attribution_not_abnormal_no_hitl(db, tmpdir_clean):
    """归因不异常 → 不入 HITL 队列、无 hitl_id。"""
    hitl_file = tmpdir_clean / "nohitl.jsonl"
    fake_calm = {"ok": True, "is_abnormal": False, "metric": "gmv",
                 "current_total": 1.0, "previous_total": 1.0,
                 "change": 0.0, "change_pct": 0.0,
                 "current_spec": "本月", "top_contributors": []}
    with patch("sqlpa.business.attribution.analyze", return_value=fake_calm):
        a = _answer(db, hitl_path=str(hitl_file))
    assert "hitl_id" not in a
    assert not hitl_file.exists()


def test_enhancement_failure_does_not_block(db):
    """治理面任一步失败都必须降级，不阻塞主取数结果。"""
    with patch("sqlpa.business.attribution.analyze", side_effect=RuntimeError("boom")):
        a = _answer(db)
    assert a["ok"] is True                        # 主结果不受影响
    assert "attribution" not in a


def test_summarize_deterministic_mentions_fluctuation():
    """无 LLM 时，确定性点评应包含波动信息。"""
    s = summarize({"ok": True, "metric_name": "GMV成交总额", "current_total": 80,
                   "previous_total": 150, "change": -70.0, "change_pct": -0.467,
                   "top_contributors": [{"desc": "品类「alimentos」变化-70.00",
                                         "pct_of_change": 1.0}]})
    assert "波动" in s and "GMV成交总额" in s


def test_attribution_summary_written_to_hitl_ticket(db, tmpdir_clean):
    """#3 归因总结应作为 AI 分析草稿写入 HITL 工单 ai_note，审核人可直接查看。"""
    fake_abnormal = {
        "ok": True, "is_abnormal": True, "metric": "gmv",
        "metric_name": "GMV成交总额", "change": -70.0, "change_pct": -0.467,
        "current_total": 80.0, "previous_total": 150.0,
        "current_spec": "本月", "previous_spec": "上月",
        "dims": [{"dim": "category", "current": {}, "previous": {}, "delta": {}}],
        "top_contributors": [{"dim": "category", "key": "alimentos",
                              "delta": -70.0, "pct_of_change": 1.0,
                              "desc": "品类「alimentos」变化-70.00"}],
    }
    hitl_file = tmpdir_clean / "hitl_ai.jsonl"
    with patch("sqlpa.business.attribution.analyze", return_value=fake_abnormal):
        a = _answer(db, hitl_path=str(hitl_file))
    assert a["attribution_summary"].strip()
    rec = json.loads(open(hitl_file, encoding="utf-8").read().strip().splitlines()[-1])
    assert rec.get("ai_note") == a["attribution_summary"]   # AI 归因总结草稿已写入工单 ai_note


def test_detect_conflicts_llm_analysis():
    """#1 detect_conflicts 传 llm 时可为冲突生成 ai_analysis；不传默认留空、不阻塞。"""
    from sqlpa.business.metric_config import BusinessConfig, Metric
    from sqlpa.business.governance import detect_conflicts

    m1 = Metric("gmv", "GMV成交总额", "支付金额", "SUM(oi.price)", "FROM orders o", "1=1",
                support_dims=[], support_filters=[], owner="财务线", version="v3")
    m2 = Metric("net_gmv", "净成交额", "扣退款后的支付金额", "SUM(oi.price)-SUM(refund.amount)",
                "FROM orders o", "1=1", support_dims=[], support_filters=[], owner="运营线", version="v1")
    cfg = BusinessConfig(metrics={"gmv": m1, "net_gmv": m2}, dimensions={})

    plain = detect_conflicts(cfg)
    assert plain, "含语义相近但公式不同的指标对 → 应检出冲突"
    assert "ai_analysis" in plain[0]
    assert plain[0]["ai_analysis"] == ""          # 无 LLM → 空串，规则检测仍可用

    rec = _Recorder(text="两者口径一个含退款、一个不含，导致量级口径不一致，建议统一。")
    llm_cfg = detect_conflicts(cfg, llm=rec)
    assert rec.calls >= 1                          # 确实调用了 LLM
    assert any(c.get("ai_analysis") == rec.text for c in llm_cfg)


class _Recorder:
    """记录是否被调用过，返回固定文本模拟 LLM 点评。"""
    def __init__(self, text="本月GMV下跌主要因品类alimentos拖累，建议核查该品类供给。"):
        self.text = text
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        return self.text


def test_summarize_with_llm_uses_narrative():
    """有 LLM 时，summarize 应调用 llm.complete 产出自然语言点评（不只写 SQL）。"""
    rec = _Recorder()
    s = summarize({"ok": True, "metric_name": "GMV成交总额", "current_total": 80,
                   "previous_total": 150, "change": -70.0, "change_pct": -0.467,
                   "top_contributors": [{"desc": "品类「alimentos」变化-70.00",
                                         "pct_of_change": 1.0}]}, llm=rec)
    assert rec.calls == 1
    assert s == rec.text