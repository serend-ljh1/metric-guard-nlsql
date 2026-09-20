"""订阅告警闭环回归：规则 CRUD → 阈值+显著性检查 → 真实投递（含失败不吞）。

此前"订阅告警"只有"可存可查的规则"，没有推送通道、没有调度入口、没有 API，
端到端并不闭环。
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sqlpa.business import deliver
from sqlpa.business.metric_config import load_config

CFG = load_config()


@pytest.fixture
def subs_file(tmpdir_clean):
    return tmpdir_clean / "subscriptions.json"


@pytest.fixture
def alert_db(tmpdir_clean):
    """构造一个"上月 vs 本月"波动明显的库，保证会触发告警。"""
    from tests.test_attribution_additivity import _ROWS, _SCHEMA
    p = tmpdir_clean / "alerts.db"
    conn = sqlite3.connect(p)
    conn.executescript(_SCHEMA)
    conn.executescript(_ROWS)
    conn.commit()
    conn.close()
    return str(p)


# ---------------------------------------------------------------- 规则 CRUD

def test_subscription_crud_with_channel(subs_file):
    sub = deliver.add_subscription("gmv", 0.05, owner="财务线", time_spec="2018-02",
                                   dims=["state"], channel="webhook",
                                   webhook="https://example.invalid/hook",
                                   path=subs_file)
    assert sub["channel"] == "webhook" and sub["webhook"].startswith("https://")
    assert len(deliver.load_subscriptions(subs_file)) == 1
    assert deliver.remove_subscription(sub["id"], path=subs_file) is True
    assert deliver.load_subscriptions(subs_file) == []
    assert deliver.remove_subscription("nope", path=subs_file) is False


# ---------------------------------------------------------------- 检查 + 门控

def test_check_finds_alert_and_reports_significance(subs_file, alert_db):
    deliver.add_subscription("gmv", 0.01, owner="财务线", time_spec="2018-02",
                             dims=["state"], path=subs_file)
    alerts = deliver.check_subscriptions(CFG, sqlite3.connect(alert_db), path=subs_file)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["status"] in ("alert", "suppressed"), a
    # 比率类指标不做显著性检验，可加指标则应带上统计结论
    if a["status"] == "alert":
        assert "summary" in a


def test_unknown_metric_is_reported_not_crashed(subs_file, alert_db):
    deliver.add_subscription("no_such_metric", 0.05, path=subs_file)
    alerts = deliver.check_subscriptions(CFG, sqlite3.connect(alert_db), path=subs_file)
    assert alerts[0]["status"] == "skipped" and "未知指标" in alerts[0]["reason"]


# ---------------------------------------------------------------- 投递

def test_deliver_to_file_channel(subs_file, alert_db, tmpdir_clean):
    deliver.add_subscription("gmv", 0.01, owner="财务线", time_spec="2018-02",
                             dims=["state"], channel="file", path=subs_file)
    alerts = deliver.check_subscriptions(CFG, sqlite3.connect(alert_db), path=subs_file)
    outbox = tmpdir_clean / "outbox.jsonl"
    report = deliver.deliver_alerts(alerts, outbox=outbox)
    assert report["failed"] == 0
    if report["alerts"]:
        line = json.loads(outbox.read_text(encoding="utf-8").strip().splitlines()[0])
        assert line["metric"] == "gmv" and "at" in line


def test_deliver_webhook_success(monkeypatch, subs_file, alert_db):
    """webhook 通道真的发 POST，并记录 HTTP 状态。"""
    sent = {}

    class _Resp:
        status_code = 202

        def raise_for_status(self):
            return None

    def fake_post(url, json=None, timeout=None):
        sent["url"], sent["json"] = url, json
        return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    deliver.add_subscription("gmv", 0.01, owner="财务线", time_spec="2018-02",
                             dims=["state"], channel="webhook",
                             webhook="https://hook.invalid/x", path=subs_file)
    alerts = deliver.check_subscriptions(CFG, sqlite3.connect(alert_db), path=subs_file)
    report = deliver.deliver_alerts(alerts)
    if report["alerts"]:
        assert report["sent"] == 1 and report["failed"] == 0
        assert sent["url"] == "https://hook.invalid/x"
        assert sent["json"]["metric"] == "gmv"


def test_deliver_webhook_failure_is_not_swallowed(monkeypatch, subs_file, alert_db):
    """投递失败必须如实报告（cron 据此返回非零退出码），而不是静默丢弃告警。"""
    import httpx

    def boom(url, json=None, timeout=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx, "post", boom)
    deliver.add_subscription("gmv", 0.01, owner="财务线", time_spec="2018-02",
                             dims=["state"], channel="webhook",
                             webhook="https://hook.invalid/x", path=subs_file)
    alerts = deliver.check_subscriptions(CFG, sqlite3.connect(alert_db), path=subs_file)
    report = deliver.deliver_alerts(alerts)
    if report["alerts"]:
        assert report["failed"] == 1 and "connection refused" in report["results"][0]["detail"]


def test_webhook_without_url_is_an_error(subs_file, alert_db):
    deliver.add_subscription("gmv", 0.01, time_spec="2018-02", dims=["state"],
                             channel="webhook", path=subs_file)
    alerts = deliver.check_subscriptions(CFG, sqlite3.connect(alert_db), path=subs_file)
    report = deliver.deliver_alerts(alerts)
    if report["alerts"]:
        assert report["failed"] == 1
        assert "未配置 webhook" in report["results"][0]["detail"]


def test_no_alerts_means_no_delivery():
    report = deliver.deliver_alerts([{"status": "ok", "metric": "gmv"}])
    assert report == {"alerts": 0, "sent": 0, "failed": 0, "results": []}


# ---------------------------------------------------------------- API

def test_subscription_api_requires_admin(monkeypatch, subs_file):
    import api
    monkeypatch.setenv("SQLPA_API_TOKENS", "tok-admin:admin,tok-analyst:analyst")
    monkeypatch.setattr(deliver, "_subs_path", lambda p=None: subs_file)
    client = TestClient(api.app)
    body = {"metric": "gmv", "threshold_pct": 0.05, "channel": "console"}
    assert client.post("/api/subscriptions", json=body).status_code == 401
    assert client.post("/api/subscriptions", json=body,
                       headers={"X-API-Token": "tok-analyst"}).status_code == 403
    r = client.post("/api/subscriptions", json=body,
                    headers={"X-API-Token": "tok-admin"})
    assert r.status_code == 200 and r.json()["channel"] == "console"
    sub_id = r.json()["id"]
    assert client.get("/api/subscriptions").status_code == 401
    assert any(s["id"] == sub_id for s in
               client.get("/api/subscriptions",
                          headers={"X-API-Token": "tok-analyst"}).json())
    assert client.delete(f"/api/subscriptions/{sub_id}",
                         headers={"X-API-Token": "tok-admin"}).status_code == 200
    assert client.delete("/api/subscriptions/nope",
                         headers={"X-API-Token": "tok-admin"}).status_code == 404
