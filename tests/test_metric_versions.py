"""指标口径版本历史 / 血缘 / 回滚回归。

为什么必须有：语义层的核心承诺是"可追溯"，但此前只有一个手写的 `version` 字符串——
改公式没有 diff、没有操作者、不能回滚，报告里引用的旧口径无法复现。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sqlpa.business import metric_store, storage
from sqlpa.business.metric_config import load_config

_ROOT = Path(__file__).resolve().parents[1]
_CFG_PATH = _ROOT / "src" / "sqlpa" / "business" / "business_config.yaml"


@pytest.fixture
def store(monkeypatch, tmpdir_clean):
    """独立 YAML 副本 + 独立 app.db（版本记录写这里）。"""
    cfg_copy = tmpdir_clean / "cfg.yaml"
    cfg_copy.write_text(_CFG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(storage, "_DB_PATH", tmpdir_clean / "app.db")
    return cfg_copy


def _gmv(cfg_path: Path) -> dict:
    data = metric_store.load_raw(cfg_path)
    return next(m for m in data["metrics"] if m["key"] == "gmv")


def test_edit_records_version_with_before_and_after(store):
    before = _gmv(store)
    changed = dict(before, metric_expr="SUM(oi.price) * 1.0", version="v9")
    assert metric_store.upsert_metric(changed, editing_key="gmv", path=store,
                                      actor="alice")[0]
    hist = metric_store.metric_history("gmv")
    assert len(hist) == 1
    row = hist[0]
    assert row["action"] == "update" and row["actor"] == "alice"
    assert row["before"]["metric_expr"] == before["metric_expr"]
    assert row["after"]["metric_expr"] == "SUM(oi.price) * 1.0"
    assert row["content_hash"]


def test_noop_save_does_not_add_version_noise(store):
    before = _gmv(store)
    assert metric_store.upsert_metric(dict(before), editing_key="gmv", path=store)[0]
    assert metric_store.metric_history("gmv") == [], "内容没变不该产生版本记录"


def test_create_records_create_action(store):
    new = dict(_gmv(store), key="gmv_copy", name="GMV 副本")
    assert metric_store.upsert_metric(new, path=store, actor="bob")[0]
    hist = metric_store.metric_history("gmv_copy")
    assert hist[0]["action"] == "create" and hist[0]["before"] is None


def test_rollback_restores_previous_definition(store):
    v1_expr = _gmv(store)["metric_expr"]
    changed = dict(_gmv(store), metric_expr="SUM(oi.price) * 0.5", version="v-bad")
    assert metric_store.upsert_metric(changed, editing_key="gmv", path=store)[0]
    assert _gmv(store)["metric_expr"] == "SUM(oi.price) * 0.5"

    hist = metric_store.metric_history("gmv")
    target_seq = hist[0]["version_seq"]
    ok, msg = metric_store.rollback_metric("gmv", target_seq, path=store, actor="admin")
    assert ok, msg
    assert _gmv(store)["metric_expr"] == v1_expr, "回滚后应恢复原公式"
    # 回滚本身也要留痕
    assert metric_store.metric_history("gmv")[0]["action"] == "rollback"


def test_rollback_report_sql_is_reproducible(store):
    """回滚后的指标必须重新可编译（口径历史不是摆设）。"""
    from sqlpa.business.compiler import QuerySpec, compile_spec

    v1 = _gmv(store)
    assert metric_store.upsert_metric(dict(v1, metric_expr="SUM(oi.price) * 0.5", version="v2"),
                                      editing_key="gmv", path=store)[0]
    seq = metric_store.metric_history("gmv")[0]["version_seq"]
    assert metric_store.rollback_metric("gmv", seq, path=store)[0]
    cfg2 = load_config(store)
    sql = " ".join(compile_spec(cfg2, QuerySpec(metric="gmv", dims=[])).sql.split())
    assert f"{v1['metric_expr']} AS gmv" in sql


def test_rollback_of_creation_deletes_metric(store):
    """撤销一次"新建"= 删除该指标（而不是恢复成某个不存在的旧定义）。"""
    new = dict(_gmv(store), key="tmp_metric", name="临时指标")
    assert metric_store.upsert_metric(new, path=store, actor="bob")[0]
    seq = metric_store.metric_history("tmp_metric")[0]["version_seq"]
    ok, msg = metric_store.rollback_metric("tmp_metric", seq, path=store, actor="admin")
    assert ok, msg
    assert not any(m["key"] == "tmp_metric" for m in metric_store.load_raw(store)["metrics"])
    assert metric_store.metric_history("tmp_metric")[0]["action"] == "rollback"


def test_delete_is_recorded(store):
    new = dict(_gmv(store), key="gone", name="将被删除")
    assert metric_store.upsert_metric(new, path=store)[0]
    assert metric_store.delete_metric("gone", path=store, actor="alice")[0]
    hist = metric_store.metric_history("gone")
    assert hist[0]["action"] == "delete" and hist[0]["before"]["name"] == "将被删除"


def test_history_is_append_only(store):
    metric_store.upsert_metric(dict(_gmv(store), version="v7"), editing_key="gmv",
                               path=store)
    with storage._conn() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM metric_versions")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE metric_versions SET actor='x'")


def test_metric_lineage_lists_tables_and_columns():
    cfg = load_config()
    lin = metric_store.metric_lineage(cfg, "gmv")
    assert lin["tables"] == ["orders", "order_items"]
    assert "order_items.price" in lin["columns"]
    assert lin["support_dims"]
    assert "非数据库字段级血缘" in lin["note"]


def test_metric_lineage_for_derived_join_metric():
    """派生表 JOIN 的指标也要能解析出真实底层表（reviews / orders）。"""
    cfg = load_config()
    lin = metric_store.metric_lineage(cfg, "avg_review")
    assert "reviews" in lin["tables"] and "orders" in lin["tables"]
