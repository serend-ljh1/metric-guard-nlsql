"""统计门控回归：阈值告警必须再过一道显著性检验，且"证据不足"不能伪装成"正常"。

背景：单期环比只看 |change_pct| >= 5%，而真实数据里日间波动很大 ——
在真实 Olist 上实测 56 个阈值告警里有 32 个（57%）过不了显著性检验，
其中就包括 README 曾引以为据的"2018-03 vs 2018-02 GMV +17.1%"（p=0.298，且 66% 可由月份天数解释）。
"""
from __future__ import annotations

import sqlite3

import pytest

from sqlpa.analysis.orchestrator import _Emitter, _decision
from sqlpa.business.attribution import analyze, significance_test
from sqlpa.business.metric_config import load_config

CFG = load_config()


# ---------------------------------------------------------------- 检验本身

def test_significance_detects_noise_vs_real_shift():
    """同样 +6% 的变化，在低噪声序列上显著、在高噪声序列上不显著。"""
    low_noise_prev = [1000.0, 1010.0, 990.0, 1000.0, 1000.0]
    low_noise_cur = [1060.0, 1070.0, 1050.0, 1060.0, 1060.0]
    high_noise_prev = [900.0, 900.0, 1000.0, 1100.0, 1400.0]
    high_noise_cur = [900.0, 900.0, 1000.0, 1100.0, 1400.0, 1060.0]
    assert significance_test(low_noise_cur, low_noise_prev)["is_significant"] is True
    sig = significance_test(high_noise_cur, high_noise_prev)
    assert sig["tested"] is True and sig["is_significant"] is False
    assert sig["p_value"] > 0.05


def test_significance_refuses_tiny_samples():
    """有效天数不足时必须说"没测"，而不是给一个假的显著性判断。"""
    sig = significance_test([1.0, 2.0], [1.0, 2.0])
    assert sig["tested"] is False and "天数不足" in sig["reason"]


def test_significance_handles_zero_variance():
    """日序列无方差（数据退化）→ 不宣称显著。"""
    sig = significance_test([100.0] * 5, [100.0] * 5)
    assert sig["tested"] is False


# ---------------------------------------------------------------- 分析链产出

def test_analyze_attaches_significance_for_additive_metric(aov_db):
    r = analyze(CFG, sqlite3.connect(aov_db), "gmv",
                current_spec="2018-02", previous_spec="2018-01",
                dims=["state"], threshold_pct=0.05)
    sig = r.get("significance")
    assert sig and sig.get("tested") is True
    assert "p_value" in sig and "days" in sig


def test_ratio_metric_skips_significance(aov_db):
    """比率类指标不满足"日值之和≈总值"，因此不做该检验（避免给出错误显著性）。"""
    r = analyze(CFG, sqlite3.connect(aov_db), "aov",
                current_spec="2018-02", previous_spec="2018-01",
                dims=["state"], threshold_pct=0.05)
    assert "significance" not in r


# ---------------------------------------------------------------- 决策层三态

def _decide(att_extra: dict, hitl_path=None):
    att = {"ok": True, "metric": "gmv", "metric_name": "GMV", "is_abnormal": True,
           "change": 15.0, "change_pct": 0.171, "current_total": 100.0, "previous_total": 85.0,
           "current_spec": "2018-03", "previous_spec": "2018-02",
           "top_contributors": [{"dim": "state", "key": "SP", "delta": 10.0,
                                 "desc": "州「SP」变化+10.00", "pct_of_change": 0.5,
                                 "pct_reliable": True}]}
    att.update(att_extra)
    merged = {"attribution": att, "query_res": {"ok": True}, "conclusion": {"text": "结论"}}
    routing = {"metric": "gmv", "metric_name": "GMV", "route": "analyze"}
    return _decision(merged, routing, None, CFG, hitl_path, _Emitter(lambda p: None), 0.05,
                     {"text": "结论"})


def test_decision_watch_level_when_not_significant(tmpdir_clean):
    """阈值过了但没过显著性检验 → alert_level=watch、不告警、不建工单。"""
    d = _decide({"significance": {"tested": True, "is_significant": False,
                                  "z": 1.04, "p_value": 0.298}})
    assert d["alert"] is False
    assert d["alert_level"] == "watch"
    assert d["hitl_id"] is None
    assert "未通过显著性检验" in d["reason"]
    assert d["significance"]["p_value"] == 0.298, "原始统计量必须保留在决策里"


def test_decision_alert_level_when_significant(tmpdir_clean):
    """阈值 + 显著性双通过 → 正常告警（alert_level=alert 且有工单）。"""
    hitl = tmpdir_clean / "hitl.jsonl"
    d = _decide({"significance": {"tested": True, "is_significant": True,
                                  "z": 4.2, "p_value": 0.0001}}, hitl_path=str(hitl))
    assert d["alert"] is True and d["alert_level"] == "alert"
    assert d["hitl_id"], "确认异常必须写入 HITL 工单"
    assert "通过显著性检验" in d["reason"]


def test_decision_without_significance_keeps_old_behaviour(tmpdir_clean):
    """没做检验（如比率类指标）时保持原行为，不因缺字段而改判。"""
    hitl = tmpdir_clean / "hitl2.jsonl"
    d = _decide({}, hitl_path=str(hitl))
    assert d["alert"] is True and d["alert_level"] == "alert"


@pytest.fixture
def aov_db(tmpdir_clean, request):
    """复用 test_attribution_additivity 的同一套小库（保持口径一致）。"""
    from tests.test_attribution_additivity import _ROWS, _SCHEMA
    p = tmpdir_clean / "sig.db"
    conn = sqlite3.connect(p)
    conn.executescript(_SCHEMA)
    conn.executescript(_ROWS)
    conn.commit()
    conn.close()
    return str(p)
