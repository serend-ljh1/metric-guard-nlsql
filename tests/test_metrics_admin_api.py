"""指标中心写接口回归：口径 CRUD 受 admin 门控，且写入隔离的配置副本。

背景：`metric_store` 早有指标/维度/别名的增删改与**校验**（公式非空、来源须 FROM、
维度/过滤须已登记），却**没有任何端点**调用它 —— 语义层只能靠手改 YAML。这里回归
新接上的写接口，重点是：写操作**必须 admin**（口径是治理资产），且落盘经校验。

测试隔离：写接口默认落回权威 `business_config.yaml`，若直接打真配置，跑一次用例就
改了仓库语义层。故用例把 `SQLPA_METRIC_CONFIG_PATH` 指向**临时副本**。
"""
from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from sqlpa.business import metric_store

_ADMIN = {"X-API-Token": "tok-admin"}
_ANALYST = {"X-API-Token": "tok-analyst"}
_NEW = {"key": "demo_metric", "name": "演示指标", "metric_expr": "SUM(o.price)",
        "from_clause": "FROM order_items o", "desc": "测试用"}


@pytest.fixture
def cfg_path(tmpdir_clean, monkeypatch):
    dst = tmpdir_clean / "business_config.yaml"
    shutil.copyfile(metric_store._DEFAULT, dst)
    monkeypatch.setenv("SQLPA_METRIC_CONFIG_PATH", str(dst))
    return dst


@pytest.fixture
def admin_client(monkeypatch, cfg_path):
    import api
    monkeypatch.setenv("SQLPA_API_TOKENS", "tok-admin:admin,tok-analyst:analyst")
    return TestClient(api.app)


def test_all_write_endpoints_require_admin(admin_client):
    calls = [
        lambda h: admin_client.post("/api/metrics/upsert", json=_NEW, headers=h),
        lambda h: admin_client.delete("/api/metrics/demo_metric", headers=h),
        lambda h: admin_client.post("/api/metrics/dimensions/upsert",
                                    json={"key": "d", "name": "D", "sql_fragment": "1"}, headers=h),
        lambda h: admin_client.delete("/api/metrics/dimensions/d", headers=h),
        lambda h: admin_client.post("/api/metrics/aliases",
                                    json={"name": "x", "table": "t"}, headers=h),
        lambda h: admin_client.delete("/api/metrics/aliases/x", headers=h),
    ]
    for call in calls:
        assert call({}).status_code == 401            # 无凭据 → 拒绝
        assert call(_ANALYST).status_code == 403      # analyst → 越权


def test_admin_creates_then_deletes_metric_with_validation(admin_client, cfg_path):
    ok = admin_client.post("/api/metrics/upsert", json=_NEW, headers=_ADMIN)
    assert ok.status_code == 200 and ok.json()["ok"]
    keys = [m["key"] for m in metric_store.load_raw(cfg_path)["metrics"]]
    assert "demo_metric" in keys

    # 校验兜底：公式为空 → 400（口径的核心不能空）
    bad = dict(_NEW, key="bad_metric", metric_expr="   ")
    assert admin_client.post("/api/metrics/upsert", json=bad, headers=_ADMIN).status_code == 400
    assert "bad_metric" not in [m["key"] for m in metric_store.load_raw(cfg_path)["metrics"]]

    assert admin_client.delete("/api/metrics/demo_metric", headers=_ADMIN).status_code == 200
    assert "demo_metric" not in [m["key"] for m in metric_store.load_raw(cfg_path)["metrics"]]
    # 再删同一 key → 404（已不存在）
    assert admin_client.delete("/api/metrics/demo_metric", headers=_ADMIN).status_code == 404


def test_admin_can_upsert_dimension_and_alias(admin_client, cfg_path):
    d = {"key": "demo_dim", "name": "演示维度", "sql_fragment": "c.customer_state"}
    assert admin_client.post("/api/metrics/dimensions/upsert", json=d,
                             headers=_ADMIN).status_code == 200
    assert "demo_dim" in [x["key"] for x in metric_store.load_raw(cfg_path)["dimensions"]]

    a = {"name": "演示别名", "table": "customers", "col": "customer_state"}
    assert admin_client.post("/api/metrics/aliases", json=a, headers=_ADMIN).status_code == 200
    assert "演示别名" in (metric_store.load_raw(cfg_path).get("business_alias") or {})

    assert admin_client.delete("/api/metrics/aliases/演示别名", headers=_ADMIN).status_code == 200
    assert admin_client.delete("/api/metrics/dimensions/demo_dim", headers=_ADMIN).status_code == 200
    assert "demo_dim" not in [x["key"] for x in metric_store.load_raw(cfg_path)["dimensions"]]


def test_metric_edit_preserves_join_clause(admin_client, cfg_path):
    """回归历史事故：在指标中心保存一次，gmv 的 JOIN 被静默丢掉 → SUM(oi.price) 报错。

    编辑既有指标（editing_key=gmv）时，join_clause 必须原样保留。
    """
    before = next(m for m in metric_store.load_raw(cfg_path)["metrics"] if m["key"] == "gmv")
    assert before.get("join_clause"), "前置：gmv 应有 join_clause"

    payload = dict(before, editing_key="gmv")     # 原样提交（真实保存路径）
    assert admin_client.post("/api/metrics/upsert", json=payload, headers=_ADMIN).status_code == 200
    after = next(m for m in metric_store.load_raw(cfg_path)["metrics"] if m["key"] == "gmv")
    assert after["join_clause"] == before["join_clause"]
