"""可观测性与审计单一 sink 回归。

两类问题：
  1. 生产路径**完全不计量**成本/延迟 —— 只能靠猜；评测里的 LLM 调用计数还读错了字段（恒为 0）。
  2. 审计双写（JSONL + SQLite）可能分叉，/api/audit 读 JSONL 而库内容不一致。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sqlpa.analysis.orchestrator import _llm_usage_snapshot, _observability, run_analysis
from sqlpa.business import storage
from sqlpa.business.audit import AuditRecord, append_audit
from sqlpa.business.metric_config import load_config
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox


class _FakeLLM:
    """带用量统计的假 LLM（与 OpenAICompatLLM 的接口一致）。"""

    def __init__(self):
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        self.cost = 0.0

    def _acc(self, pt, ct):
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += pt
        self.usage["completion_tokens"] += ct
        self.usage["total_tokens"] += pt + ct
        self.cost += pt / 1e6 * 0.14 + ct / 1e6 * 0.28

    def stats(self):
        return {"usage": dict(self.usage), "cost": round(self.cost, 6), "last_model": "fake-1"}

    def complete(self, prompt):  # 供归因/结论使用
        self._acc(100, 20)
        return "假结论"


def test_observability_delta_math():
    llm = _FakeLLM()
    before = _llm_usage_snapshot(llm)
    llm._acc(100, 20)
    llm._acc(50, 10)
    obs = _observability(before, _llm_usage_snapshot(llm), 0.0)
    assert obs["llm_available"] is True
    assert obs["llm_calls"] == 2
    assert obs["prompt_tokens"] == 150 and obs["completion_tokens"] == 30
    assert obs["total_tokens"] == 180
    assert obs["cost"] > 0
    assert obs["model"] == "fake-1"
    assert obs["latency_ms"] >= 0


def test_observability_reports_unavailable_without_llm():
    obs = _observability(None, None, 0.0)
    assert obs["llm_available"] is False and obs["llm_calls"] == 0


def test_analysis_result_carries_observability(sample_db):
    """分析结果必须带上本次的时延与 LLM 用量（此前完全没有）。"""
    cfg = load_config()
    sb = SqlSandbox(sample_db, ExecConfig(max_rows=2000))
    llm = _FakeLLM()
    out = run_analysis("2018-06 的 GMV 是多少", cfg, sb, sample_db, llm,
                       role="analyst", username="obs", dims_override=[])
    obs = out.get("observability") or {}
    assert obs.get("latency_ms", 0) > 0
    assert obs.get("llm_available") is True
    assert obs.get("llm_calls", 0) >= 1, "调用过 LLM 却计为 0 次"
    assert obs.get("total_tokens", 0) > 0


def test_query_path_records_usage_in_audit(monkeypatch, tmpdir_clean, sample_db):
    """/api/query 路径也要把用量写进审计（成本可回溯到具体一次查询）。"""
    from sqlpa.business.service import answer

    monkeypatch.setattr(storage, "_DB_PATH", tmpdir_clean / "app.db")
    cfg = load_config()
    sb = SqlSandbox(sample_db, ExecConfig(max_rows=2000))
    llm = _FakeLLM()
    res = answer("各个品类的GMV", cfg, sb, sample_db, llm=llm, role="analyst", username="obs")
    assert res["ok"] and res["path"] == "semantic"
    assert res["observability"]["llm_available"] is True
    rows = storage.list_audit(limit=5)
    row = next(r for r in rows if r["username"] == "obs")
    assert row["latency_ms"] is not None and row["latency_ms"] > 0
    assert row["total_tokens"] is not None


def test_audit_endpoint_reads_authoritative_sink(monkeypatch, tmpdir_clean):
    """审计端点的权威来源是 SQLite：JSONL 镜像缺失/滞后不影响读取。

    这样"两个 sink 各说各话"在结构上就不存在了。
    """
    import api

    monkeypatch.setattr(storage, "_DB_PATH", tmpdir_clean / "app.db")
    monkeypatch.setenv("SQLPA_API_TOKENS", "")
    assert append_audit(AuditRecord(query_id="q-sink", username="u1", user_input="问题",
                                    matched_metric="gmv", is_success=True),
                        path=tmpdir_clean / "mirror.jsonl") is True
    client = TestClient(api.app)
    rows = client.get("/api/audit?limit=10").json()
    assert any(r["query_id"] == "q-sink" for r in rows), "审计端点未从 SQLite 读到记录"


def test_audit_mirror_failure_does_not_break_authoritative(monkeypatch, tmpdir_clean):
    """JSONL 镜像写失败不应影响权威 sink 的成功语义（一致性优先）。"""
    from sqlpa.business import audit as audit_mod

    monkeypatch.setattr(storage, "_DB_PATH", tmpdir_clean / "app.db")
    bad = tmpdir_clean / "nope" / "x.jsonl"

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(audit_mod, "open", boom, raising=False)
    before = len(audit_mod.audit_mirror_failures())
    ok = audit_mod.append_audit(AuditRecord(query_id="q-mirror", username="u"), path=bad)
    assert ok is True, "权威 sink 成功就该返回 True"
    assert len(audit_mod.audit_mirror_failures()) == before + 1
    assert any(r["query_id"] == "q-mirror" for r in storage.list_audit(limit=5))


def test_health_exposes_audit_health(monkeypatch, tmpdir_clean):
    import api
    monkeypatch.setattr(storage, "_DB_PATH", tmpdir_clean / "app.db")
    body = TestClient(api.app).get("/health").json()
    assert body["status"] == "ok"
    assert "audit_write_failures" in body and "audit_mirror_failures" in body


def test_eval_decisions_call_counter_reads_usage():
    """评测里的调用计数此前读错字段恒为 0（README 的"花了 N 次调用"因此是错的）。"""
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "evaluation"))
    spec = importlib.util.spec_from_file_location("eval_decisions_mod",
                                                  root / "evaluation" / "eval_decisions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]

    llm = _FakeLLM()
    llm._acc(10, 5)
    assert mod._llm_call_count(llm) == 1
    assert mod._llm_call_count(None) == 0
    assert mod._llm_call_count(object()) == 0
