"""数据底座回退回归：没有真实全量库时，必须能用仓库自带样本库跑通。

背景：`data/olist/olist.db`（66MB）被 .gitignore 排除，克隆者 `git clone` 后本该
"开箱即跑"。此前的结果是 pytest 大量 skip、评测全部 SKIP、前端无数据可演示——
简历项目第一道门槛就倒了。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sqlpa.config import DB_CANDIDATES, db_kind_note, resolve_db_path

_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE = _ROOT / "data" / "sample" / "olist_sample.db"


# ---------------------------------------------------------------- 解析顺序

def test_sample_db_is_committed_and_usable():
    """样本库必须随仓库提交，且结构完整、能直接跑业务语义层。"""
    assert _SAMPLE.exists(), "缺少 data/sample/olist_sample.db（应随仓库提交）"
    assert _SAMPLE.stat().st_size < 5 * 1024 * 1024, "样本库不宜超过 5MB"
    conn = sqlite3.connect(_SAMPLE)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"orders", "order_items", "customers", "products", "reviews"} <= tables
    # 按天抽样 → 日粒度序列可用（显著性检验/按天分析依赖它）
    days = conn.execute("SELECT COUNT(DISTINCT DATE(order_purchase_timestamp)) "
                        "FROM orders").fetchone()[0]
    months = conn.execute("SELECT COUNT(DISTINCT strftime('%Y-%m', order_purchase_timestamp)) "
                          "FROM orders").fetchone()[0]
    assert days > 300, f"样本库只覆盖 {days} 天，日粒度分析会退化"
    assert months >= 20, f"样本库只覆盖 {months} 个月，环比演示会缺数据"

    from sqlpa.business.metric_config import load_config
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox
    from sqlpa.business.compiler import QuerySpec, compile_spec

    cfg = load_config()
    sb = SqlSandbox(str(_SAMPLE), ExecConfig(max_rows=200))
    for metric in ("gmv", "order_count", "avg_review", "cancellation_rate"):
        cq = compile_spec(cfg, QuerySpec(metric=metric, dims=["state"], top=5))
        r = sb.execute(cq.sql)
        assert r.ok and r.rows, f"{metric} 在样本库上跑不出结果: {r.error}"


def test_resolve_prefers_full_over_sample(tmpdir_clean):
    (tmpdir_clean / "data" / "olist").mkdir(parents=True)
    (tmpdir_clean / "data" / "sample").mkdir(parents=True)
    full = tmpdir_clean / "data" / "olist" / "olist.db"
    sample = tmpdir_clean / "data" / "sample" / "olist_sample.db"
    full.write_bytes(b"")
    sample.write_bytes(b"")
    path, kind = resolve_db_path(root=tmpdir_clean)
    assert kind == "full" and Path(path) == full


def test_resolve_falls_back_to_sample(tmpdir_clean):
    (tmpdir_clean / "data" / "sample").mkdir(parents=True)
    sample = tmpdir_clean / "data" / "sample" / "olist_sample.db"
    sample.write_bytes(b"")
    path, kind = resolve_db_path(root=tmpdir_clean)
    assert kind == "sample" and Path(path) == sample
    assert "样本库" in db_kind_note(kind)
    assert db_kind_note("full") == ""


def test_resolve_raises_with_actionable_message(tmpdir_clean):
    with pytest.raises(FileNotFoundError) as e:
        resolve_db_path(root=tmpdir_clean)
    msg = str(e.value)
    assert "build_olist_sample.py" in msg and "build_olist_db.py" in msg


def test_resolve_honours_explicit_path(tmpdir_clean):
    p = tmpdir_clean / "custom.db"
    p.write_bytes(b"")
    path, _kind = resolve_db_path(explicit=p, root=tmpdir_clean)
    assert Path(path) == p
    with pytest.raises(FileNotFoundError):
        resolve_db_path(explicit=tmpdir_clean / "nope.db", root=tmpdir_clean)


def test_candidate_order_matches_docs():
    """候选顺序 = 文档承诺的顺序（全量优先、样本兜底）。"""
    assert [k for _rel, k in DB_CANDIDATES] == ["full", "sample"]


# ---------------------------------------------------------------- 端到端回退

def test_api_uses_sample_when_full_missing(monkeypatch, tmpdir_clean):
    """真实库缺失时 /api/query 仍能出结果（走样本库），而不是 500。"""
    import api
    import sqlpa.config as cfgmod

    monkeypatch.delenv("SQLPA_DB_PATH", raising=False)
    # 把项目根指向只含样本库的临时目录
    (tmpdir_clean / "data" / "sample").mkdir(parents=True)
    import shutil
    shutil.copyfile(_SAMPLE, tmpdir_clean / "data" / "sample" / "olist_sample.db")
    monkeypatch.setattr(cfgmod, "_ROOT", tmpdir_clean)
    monkeypatch.setattr(api, "_DB_KIND_WARNED", False, raising=False)

    db = api._db_path()
    assert db.endswith("olist_sample.db"), db

    from sqlpa.business.metric_config import load_config
    from sqlpa.business.service import answer
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

    res = answer("各个品类的GMV", load_config(),
                 SqlSandbox(db, ExecConfig(max_rows=100)), db, llm=None,
                 role="analyst", username="clone")
    assert res["ok"] and res["path"] == "semantic" and res["rows"]
