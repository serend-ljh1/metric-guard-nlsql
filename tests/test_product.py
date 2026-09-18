"""
tests/test_product.py
=====================
产品化功能测试（离线、无需 API Key，CI 可复现）：
  - 自由查询分级放行（口径内=认证 / 口径外=降级标注）
  - 多轮追问上下文改写（离线启发式）
  - 结果自动图表推荐
  - 指标中心 CRUD（临时配置副本，不污染真实配置）
  - 数据源注册中心
"""
import shutil

import pytest

from sqlpa.business.metric_config import load_config
from sqlpa.business.service import answer
from sqlpa.llm.mock_llm import MockLLM
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

REAL_CFG = __import__("pathlib").Path(__file__).resolve().parents[1] / \
    "src" / "sqlpa" / "business" / "business_config.yaml"


@pytest.fixture
def env(sample_db):
    sb = SqlSandbox(sample_db, ExecConfig(max_rows=2000))
    return sb, load_config(), sample_db


# ---------------- 1) 分级放行 ----------------

def test_in_scope_certified(env):
    sb, cfg, db = env
    a = answer("各个品类的GMV", cfg, sb, db, llm=None)
    assert a["matched"] and a["mode"] == "metric"
    assert a["ok"] and a["certified"]


def test_out_of_scope_offline_rejected(env):
    """口径外：语义层覆盖不到 → 离线（无 LLM）时明确拒绝，不做兜底。"""
    sb, cfg, db = env
    a = answer("每个客服的响应时长是多少", cfg, sb, db, llm=None)
    assert not a["ok"] and "自由查询" in a["reject"]


def test_out_of_scope_free_query(env):
    """口径外 + 有 LLM → 降级到多 Agent 自由生成，并标注未经口径认证。"""
    sb, cfg, db = env
    a = answer("每个客服的响应时长是多少", cfg, sb, db, llm=MockLLM())
    assert a["mode"] == "free" and a["ok"]
    assert a["certified"] is False   # 降级标注：未经口径认证
    assert a.get("path") == "fallback"


def test_in_scope_uses_semantic_path_not_llm(env):
    """架构反转回归：口径内必须走语义层确定性编译（path=semantic），不再让 LLM 写 SQL。

    修复前即便命中语义层也要引擎 LLM 生成 SQL（只加公式约束），既慢又可能被改坏。
    """
    sb, cfg, db = env
    a = answer("各个品类的GMV", cfg, sb, db, llm=MockLLM())
    assert a["ok"] and a["mode"] == "metric"
    assert a.get("path") == "semantic"
    assert a["source"].startswith("语义层确定性编译")
    assert a["certified"] is True


# ---------------- 2) 多轮追问 ----------------

def test_followup_uses_context(env):
    sb, cfg, db = env
    hist = [{"question": "各个品类的GMV", "metric": "gmv", "sql": ""}]
    a = answer("只看electronics", cfg, sb, db, llm=None, history=hist)
    assert a["used_context"]
    assert a["matched"] and a["mode"] == "metric"


# ---------------- 3) 图表推荐 ----------------

def test_chart_recommend():
    from sqlpa.business.charting import recommend
    assert recommend(["category", "gmv"], [("books", 100.0), ("elec", 200.0)])["kind"] == "bar"
    assert recommend(["dt", "gmv"], [("2026-08-01", 1.0), ("2026-08-02", 2.0)])["kind"] == "line"
    assert recommend(["gmv"], [(12345.0,)])["kind"] == "metric"
    assert recommend([], [])["kind"] == "table"


# ---------------- 4) 指标中心 CRUD ----------------

@pytest.fixture
def cfg_copy(tmpdir_clean):
    p = tmpdir_clean / "business_config.yaml"
    shutil.copy(REAL_CFG, p)
    return p


def test_metric_crud(cfg_copy):
    from sqlpa.business import metric_store as store
    ok, msg = store.upsert_metric(
        {"key": "demo_freight_x", "name": "演示运费指标", "desc": "CRUD 测试用",
         "metric_expr": "SUM(oi.freight_value)",
         "from_clause": "FROM orders o JOIN order_items oi ON o.order_id=oi.order_id",
         "where_core": "1=1", "support_dims": ["state"], "support_filters": ["time_range"]},
        path=cfg_copy)
    assert ok, msg
    assert "demo_freight_x" in load_config(cfg_copy).metrics

    ok, msg = store.upsert_metric(
        {"key": "demo_freight_x", "name": "x", "desc": "", "metric_expr": "",
         "from_clause": "FROM orders", "support_dims": [], "support_filters": []},
        editing_key="demo_freight_x", path=cfg_copy)
    assert not ok and "公式" in msg          # 空公式被校验拦截

    ok, _ = store.delete_metric("demo_freight_x", path=cfg_copy)
    assert ok


def test_dimension_crud(cfg_copy):
    from sqlpa.business import metric_store as store
    ok, _ = store.upsert_dimension({"key": "city", "name": "城市",
                                    "sql_fragment": "c.customer_city"}, path=cfg_copy)
    assert ok
    ok, msg = store.delete_dimension("state", path=cfg_copy)
    assert not ok                             # 被指标引用的维度拒绝删除


# ---------------- 5) 数据源注册中心 ----------------

def test_datasource_registry(tmpdir_clean, monkeypatch):
    from sqlpa.business import datasources as dss
    monkeypatch.setattr(dss, "_STORE", tmpdir_clean / "datasources.yaml")
    spec = dss.add_datasource({"kind": "mysql", "name": "测试库", "host": "h",
                               "port": 3306, "user": "u", "password": "p",
                               "database": "d"})
    assert dss.get_datasource(spec["id"]) is not None
    assert "***" in dss.display(spec)         # 密码掩码
    assert dss.delete_datasource(spec["id"])
    assert dss.get_datasource(spec["id"]) is None
