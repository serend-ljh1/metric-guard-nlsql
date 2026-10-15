"""HITL SLA 升级/催办的**真实投递**（webhook）回归。

背景：此前 SLA 升级只把结果写进**本地 outbox 文件** —— 没有任何"消息真的发出去了"
的动作，"催办"就只是落盘。这里回归新接的 webhook 投递：未配置地址 → 显式说明未投递
（不静默成功）；配置了 → 真的 POST；投递失败被**捕获**而非抛出（不影响巡检本身）。
"""
from __future__ import annotations

import pytest

from sqlpa.business import hitl


def test_deliver_skips_when_webhook_unconfigured(monkeypatch):
    monkeypatch.delenv("SQLPA_HITL_WEBHOOK", raising=False)
    out = hitl.deliver_webhook({"event": "x"})
    assert out["delivered"] is False and out["channel"] == "none"


def test_deliver_posts_and_reports_status(monkeypatch):
    calls: dict = {}

    def fake_post(url, payload, timeout):
        calls.update(url=url, payload=payload)
        return 201

    monkeypatch.setenv("SQLPA_HITL_WEBHOOK", "https://hooks.example/x")
    out = hitl.deliver_webhook({"event": "x", "n": 1}, post=fake_post)
    assert out == {"delivered": True, "channel": "webhook", "status": 201}
    assert calls["url"] == "https://hooks.example/x" and calls["payload"]["n"] == 1


def test_deliver_failure_is_captured_not_raised(monkeypatch):
    monkeypatch.setenv("SQLPA_HITL_WEBHOOK", "https://hooks.example/x")

    def boom(url, payload, timeout):
        raise RuntimeError("网络不可达")

    out = hitl.deliver_webhook({"event": "x"}, post=boom)
    assert out["delivered"] is False and "RuntimeError" in out["reason"]


def test_sweep_attaches_delivery_result(tmpdir_clean, monkeypatch):
    """巡检真的投递了：升级摘要进 webhook payload，投递结果挂回 summary。"""
    monkeypatch.setenv("SQLPA_HITL_WEBHOOK", "https://hooks.example/x")
    monkeypatch.setattr(hitl, "_sla_outbox", lambda *a, **k: None)   # 不写仓库 outbox
    sent: list = []
    monkeypatch.setattr(hitl, "_post_json",
                        lambda url, payload, timeout=5.0: sent.append(payload) or 200)

    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03", owner="alice")
    due = float(hitl.get(rid)["due_at"])
    out = hitl.sweep(now_ts=due + 3600)

    assert out["escalated_count"] == 1
    assert out["delivery"] == {"delivered": True, "channel": "webhook", "status": 200}
    assert sent and sent[0]["event"] == "hitl_sla_sweep"
    assert sent[0]["escalated"][0]["record_id"] == rid


def test_sweep_without_action_reports_no_delivery(tmpdir_clean, monkeypatch):
    """本轮无需升级/催办 → 不投递（避免"空转也发消息"骚扰）。"""
    monkeypatch.delenv("SQLPA_HITL_WEBHOOK", raising=False)
    rid = hitl.enqueue("GMV 异常", "gmv", "", "波动", time_spec="2018-03")
    due = float(hitl.get(rid)["due_at"])
    out = hitl.sweep(now_ts=due - 100000)          # 远未临期
    assert out["escalated_count"] == 0 and out["reminded_count"] == 0
    assert out["delivery"]["delivered"] is False
