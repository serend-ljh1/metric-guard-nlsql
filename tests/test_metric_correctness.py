"""指标口径正确性回归 —— 用**自建小库**做确定性真值对照（不依赖真实 Olist）。

为什么单独一个文件：`test_semantic_compiler.py` 验的是"能不能编译/能不能跑"，
但"编译出来的数字对不对"此前没有任何用例守。于是出现了下面这类缺陷仍然全绿：

  - 评价类指标的 join_clause 写死了 `LEFT JOIN order_items`，聚合在 reviews 上 →
    行被 fan-out：真实库上 review_count 从 100,000 涨到 114,100、平均评分从 4.0709
    掉到 3.9998，而结果仍盖着"口径已认证"。
  - 过滤条件所需的 JOIN 从不注入 → `gmv` 加 `state` 过滤直接报 no such column。
  - 支持度只在 matcher 层校验 → 编译器能编出未认证组合（aov + category 甚至能跑出数）。

本文件的判据是**手写 SQL 算出的真值**，与被测编译路径相互独立。
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from sqlpa.business.compiler import CompileError, QuerySpec, compile_spec
from sqlpa.business.metric_config import load_config
from sqlpa.business import metric_store
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

CFG = load_config()
_CFG_PATH = Path(__file__).resolve().parents[1] / "src" / "sqlpa" / "business" / "business_config.yaml"


# ---------------------------------------------------------------- 自建真值小库

_SCHEMA = """
CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT, order_status TEXT,
  order_purchase_timestamp TEXT, order_delivered_customer_date TEXT,
  order_estimated_delivery_date TEXT);
CREATE TABLE order_items(order_id TEXT, order_item_id INTEGER, product_id TEXT,
  seller_id TEXT, price REAL, freight_value REAL);
CREATE TABLE customers(customer_id TEXT PRIMARY KEY, customer_unique_id TEXT,
  customer_zip_code_prefix TEXT, customer_city TEXT, customer_state TEXT);
CREATE TABLE products(product_id TEXT PRIMARY KEY, product_category_name TEXT);
CREATE TABLE reviews(review_id TEXT, order_id TEXT, review_score REAL);
"""

# 关键构造：o1 有 **2 个 order_item 但只有 1 条评价**。
# 任何"聚合在 reviews、JOIN 又连了 order_items"的写法都会把这条评价数成 2 条。
_ROWS = """
INSERT INTO orders VALUES
  ('o1','c1','delivered','2018-01-05 10:00:00',NULL,NULL),
  ('o2','c2','delivered','2018-01-06 10:00:00',NULL,NULL),
  ('o3','c1','canceled','2018-01-07 10:00:00',NULL,NULL);
INSERT INTO order_items VALUES
  ('o1',1,'p1','s1',10.0,2.0),
  ('o1',2,'p2','s1',20.0,3.0),
  ('o2',1,'p1','s1',5.0,1.0),
  ('o3',1,'p1','s1',99.0,9.0);
INSERT INTO customers VALUES
  ('c1','u1','01151','sao paulo','SP'),
  ('c2','u2','09790','rio','RJ');
INSERT INTO products VALUES ('p1','catA'),('p2','catB');
-- o1 有两条评价（5、3）→ 订单均值 4.0；o2 一条（3.0）
-- 这个构造同时能判别"直接 JOIN reviews"的错误口径（见 avg_review 按品类用例）
INSERT INTO reviews VALUES ('r1','o1',5.0),('r2','o2',3.0),('r3','o1',3.0);
"""


@pytest.fixture
def truth_db(tmpdir_clean) -> str:
    p = tmpdir_clean / "truth.db"
    conn = sqlite3.connect(p)
    conn.executescript(_SCHEMA)
    conn.executescript(_ROWS)
    conn.commit()
    conn.close()
    return str(p)


def _value(sb, metric, **kw):
    cq = compile_spec(CFG, QuerySpec(metric=metric, **kw))
    r = sb.execute(cq.sql)
    assert r.ok, f"{metric} 执行失败: {r.error}\n{cq.sql}"
    return r.rows[0][0]


# ---------------------------------------------------------------- 1) fan-out

def test_review_metrics_are_not_inflated_by_join_fanout(truth_db):
    """评价类指标必须与手写真值一致 —— 这是"口径已认证"的最低门槛。

    构造：3 条评价（o1 两条 5/3、o2 一条 3）。
    真值（订单粒度）：平均评分 = (4.0 + 3.0)/2 = 3.5；好评率(>=4) 1/3；差评率(<=2) 0。
    若把 reviews 直接 JOIN 进来再聚合（旧实现），这三个数都会被条目数放大。
    """
    sb = SqlSandbox(truth_db, ExecConfig(max_rows=100))
    assert _value(sb, "review_count") == 3
    assert _value(sb, "avg_review") == pytest.approx(3.5)
    assert _value(sb, "positive_review_rate") == pytest.approx(1 / 3)
    assert _value(sb, "negative_review_rate") == pytest.approx(0.0)


def test_avg_review_by_category_is_order_scoped(truth_db):
    """各品类评分：订单粒度预聚合，不被 order_items 的 1:多 放大。

    o1（catA+catB 各一个条目，评价均值 4.0）、o2（catA，3.0）
    正确口径：catA = orders{o1,o2} → (4.0+3.0)/2 = 3.5；catB = {o1} → 4.0
    旧的"直接 JOIN reviews"口径：catA = (5+3+3)/3 = 3.6667（被条目/评价数加权，错）
    """
    sb = SqlSandbox(truth_db, ExecConfig(max_rows=100))
    cq = compile_spec(CFG, QuerySpec(metric="avg_review", dims=["category"]))
    r = sb.execute(cq.sql)
    assert r.ok, r.error
    rows = {row[1]: row[0] for row in r.rows}
    assert rows["catA"] == pytest.approx(3.5)
    assert rows["catB"] == pytest.approx(4.0)


def test_order_level_metrics_still_correct(truth_db):
    """对照组：可加指标（GMV / 订单数）不受影响。"""
    sb = SqlSandbox(truth_db, ExecConfig(max_rows=100))
    assert _value(sb, "gmv") == pytest.approx(35.0)            # 排除 canceled 的 o3
    assert _value(sb, "order_count") == 2                       # delivered 的 o1/o2


def test_review_count_by_state_not_inflated(truth_db):
    """带维度时同样不能被 1:多 JOIN 放大。

    o1（SP）有 2 个 order_item、2 条评价：fan-out 写法会给 SP 算出 4（每条评价×每个条目）。
    """
    sb = SqlSandbox(truth_db, ExecConfig(max_rows=100))
    cq = compile_spec(CFG, QuerySpec(metric="review_count", dims=["state"]))
    r = sb.execute(cq.sql)
    assert r.ok, r.error
    # 编译产物的选择列表是「指标在前、维度在后」，按列位置取值
    rows = {row[1]: row[0] for row in r.rows}
    assert rows["SP"] == 2 and rows["RJ"] == 1


# ---------------------------------------------------------------- 2) 过滤 JOIN

def test_filter_only_query_injects_required_join(truth_db):
    """过滤条件引用 c./p. 时必须注入对应 JOIN（旧实现只按 dims 注入）。"""
    sb = SqlSandbox(truth_db, ExecConfig(max_rows=100))
    # c1 在 SP，其订单 o1(10+20) 计入、o3(99) 是 canceled 不计 → 30
    assert _value(sb, "gmv", filters=[("state", "SP")]) == pytest.approx(30.0)
    assert _value(sb, "gmv", filters=[("category", "catA")]) == pytest.approx(15.0)
    assert _value(sb, "order_count", filters=[("state", "RJ")]) == 1


def test_share_denominator_injects_filter_join(truth_db):
    """share 的分母子查询也必须能带上过滤所需的 JOIN。"""
    sb = SqlSandbox(truth_db, ExecConfig(max_rows=100))
    cq = compile_spec(CFG, QuerySpec(metric="share@canceled_order_count",
                                     dims=["state"], filters=[("state", "SP")]))
    r = sb.execute(cq.sql)
    assert r.ok, r.error
    # 过滤到 SP 后：SP 取消 1 单 / 全体(SP) 取消 1 单 = 1.0
    rows = {row[1]: row[0] for row in r.rows}
    assert rows["SP"] == pytest.approx(1.0)


# ---------------------------------------------------------------- 3) 支持度

def test_unsupported_dimension_rejected_at_compile_time():
    """支持度必须在编译器上强制，而不是只在 matcher 层"君子协定"。"""
    with pytest.raises(CompileError):
        compile_spec(CFG, QuerySpec(metric="cancellation_rate", dims=["category"]))
    with pytest.raises(CompileError):
        compile_spec(CFG, QuerySpec(metric="aov", dims=["category"]))
    with pytest.raises(CompileError):
        compile_spec(CFG, QuerySpec(metric="avg_review", dims=["status"]))


def test_unsupported_filter_rejected_at_compile_time():
    with pytest.raises(CompileError):
        compile_spec(CFG, QuerySpec(metric="review_count", filters=[("status", "delivered")]))


def test_derived_metric_validates_operands_support():
    """派生指标不能成为绕过支持度校验的后门。"""
    with pytest.raises(CompileError):
        # order_count 不支持 category（aov 也不支持）→ 派生路径同样要拒
        compile_spec(CFG, QuerySpec(metric="ratio@aov/order_count", dims=["category"]))


# ---------------------------------------------------------------- 4) ratio 同源

def test_ratio_rejects_mismatched_row_level_scope():
    """行级口径（where_core）不同就不许相除。

    gmv 的 where 是 `status != canceled`、order_count 的是 `status = delivered`。
    旧实现把两个 where 直接 AND → GMV 被悄悄改成"仅已送达"，编译成功、执行成功、
    数字错。这正是本项目声称要消灭的口径漂移，必须明确拒绝。
    """
    with pytest.raises(CompileError) as e:
        compile_spec(CFG, QuerySpec(metric="ratio@gmv/order_count"))
    assert "行级口径" in str(e.value)


def test_ratio_same_scope_still_works(truth_db):
    """同口径的 ratio 不受影响（运费/ GMV 都是 status != canceled）。"""
    sb = SqlSandbox(truth_db, ExecConfig(max_rows=100))
    assert _value(sb, "ratio@freight_cost/gmv") == pytest.approx(6.0 / 35.0)


# ---------------------------------------------------------------- 5) 指标编辑

def test_metric_store_preserves_join_clause(tmpdir_clean):
    """指标中心保存一次不能毁掉指标：join_clause 必须原样保留。

    历史缺陷：upsert_metric 重建固定字段 dict 时漏了 join_clause →
    保存 gmv 后 `SUM(oi.price)` 失去 `JOIN order_items`，指标永久不可用。
    """
    target = tmpdir_clean / "cfg.yaml"
    target.write_text(_CFG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    m = CFG.metrics["gmv"].to_dict()
    ok, msg = metric_store.upsert_metric(m, editing_key="gmv", path=target)
    assert ok, msg
    saved = metric_store.load_raw(target)
    gmv = next(x for x in saved["metrics"] if x["key"] == "gmv")
    assert gmv["join_clause"] == CFG.metrics["gmv"].join_clause
    # 未知自定义字段也不该被静默丢掉
    m2 = CFG.metrics["gmv"].to_dict()
    m2["custom_field"] = "keep-me"
    assert metric_store.upsert_metric(m2, editing_key="gmv", path=target)[0]
    saved = metric_store.load_raw(target)
    gmv = next(x for x in saved["metrics"] if x["key"] == "gmv")
    assert gmv.get("custom_field") == "keep-me"


def test_edit_roundtrip_keeps_metric_executable(truth_db, tmpdir_clean):
    """端到端：编辑保存后重新加载配置，gmv 仍然可编译可执行。"""
    target = tmpdir_clean / "cfg2.yaml"
    target.write_text(_CFG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    assert metric_store.upsert_metric(CFG.metrics["gmv"].to_dict(),
                                      editing_key="gmv", path=target)[0]
    cfg2 = load_config(target)
    cq = compile_spec(cfg2, QuerySpec(metric="gmv"))
    sb = SqlSandbox(truth_db, ExecConfig(max_rows=100))
    r = sb.execute(cq.sql)
    assert r.ok, f"编辑后指标不可用: {r.error}"
    assert r.rows[0][0] == pytest.approx(35.0)
