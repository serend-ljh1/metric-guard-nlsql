"""
tests/conftest.py
=================
pytest 公共 fixture：
  - tmpdir_clean: 仓库内自管理的临时目录（见下方说明）
  - sample_db  : Olist 同结构小样本库（离线可用）
  - mini_db    : 内置迷你 schema（singer/concert），引擎测试不依赖本地 Spider 数据，
                 保证 CI 可复现

为什么不用 pytest 的 tmp_path：
  受限/沙箱环境与部分容器里，pytest 对 basetemp 的创建/清理会被拒绝
  （PermissionError / mkdir 失败），导致所有依赖 tmp_path 的用例在 setup 阶段直接报错。
  这里改用 uuid 命名、建在仓库内 ._test_tmp/ 的目录，本地与 CI 行为一致。
  该目录已被 .gitignore 忽略。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # 让 `import api` 可用（api.py 在项目根）
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

_TMP_BASE = ROOT / "._test_tmp"


@pytest.fixture
def tmpdir_clean():
    """独立的临时目录（用完即删）。

    注意：不要用 tempfile.mkdtemp —— 它会收紧目录 ACL，导致 SQLite 无法在其中建库
    （OperationalError: unable to open database file）。
    """
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    d = _TMP_BASE / f"t-{uuid.uuid4().hex[:12]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="session")
def real_olist_db() -> str:
    """可用的业务库路径：优先真实全量库，其次**仓库自带样本库**。

    clone 之后没有 data/olist/olist.db 也能跑（样本库随仓库提交），
    只有两者都不存在时才 skip——"开箱即跑"是简历项目的第一道门槛。
    """
    from sqlpa.config import resolve_db_path
    try:
        path, kind = resolve_db_path()
    except FileNotFoundError:
        pytest.skip("既无真实 Olist 库也无样本库，跳过依赖业务数据的用例")
    if kind == "sample":
        # 样本库是随仓库提交的**只读资产**：拷一份出来，避免用例写坏它
        _TMP_BASE.mkdir(parents=True, exist_ok=True)
        copy = _TMP_BASE / f"sample-{uuid.uuid4().hex[:8]}.db"
        shutil.copyfile(path, copy)
        return str(copy)
    return path


@pytest.fixture(scope="session")
def sample_db() -> str:
    """业务层测试库：有真实全量库就切片，否则退化为**仓库自带样本库**的副本。

    测试不再依赖"捏造日期"的假样本：数据全部来自真实 Olist（2016-09~2018-10），
    业务语义层的指标/维度因此能真正跑出结果。
    """
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    real = ROOT / "data" / "olist" / "olist.db"
    if real.exists():
        p = _TMP_BASE / f"olist-{uuid.uuid4().hex[:8]}.db"
        _build_olist_slice(p)
        return str(p)
    # 没有全量库 → 用随仓库提交的样本库（拷副本，避免写坏只读资产）
    from sqlpa.config import resolve_db_path
    path, kind = resolve_db_path()
    if kind != "sample":
        pytest.skip("既无全量库也无样本库，跳过依赖业务数据的用例")
    p = _TMP_BASE / f"sample-{uuid.uuid4().hex[:8]}.db"
    shutil.copyfile(path, p)
    return str(p)


def _build_olist_slice(dest: Path) -> None:
    """从真实 data/olist/olist.db 抽一小撮记录组成测试库（保持外键引用完整）。"""
    real = ROOT / "data" / "olist" / "olist.db"
    if not real.exists():
        pytest.skip("缺少真实 Olist 数据 data/olist/olist.db，跳过依赖业务库的用例")
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
    conn = sqlite3.connect(dest)
    conn.executescript(_SCHEMA)
    conn.execute('ATTACH DATABASE ? AS sr', (str(real.resolve()),))
    conn.execute("""
        INSERT INTO orders SELECT * FROM sr.orders
        WHERE order_id IN (SELECT order_id FROM sr.orders
                           ORDER BY order_purchase_timestamp LIMIT 1200)""")
    conn.execute("INSERT INTO order_items SELECT * FROM sr.order_items "
                 "WHERE order_id IN (SELECT order_id FROM orders)")
    conn.execute("INSERT INTO customers SELECT * FROM sr.customers "
                 "WHERE customer_id IN (SELECT customer_id FROM orders)")
    conn.execute("INSERT INTO products SELECT * FROM sr.products "
                 "WHERE product_id IN (SELECT product_id FROM order_items)")
    conn.execute("INSERT INTO reviews SELECT * FROM sr.reviews "
                 "WHERE order_id IN (SELECT order_id FROM orders)")
    conn.commit()
    conn.execute("DETACH DATABASE sr")
    conn.close()


@pytest.fixture(scope="session")
def mini_db() -> str:
    """迷你两表库（引擎链路测试用，无需 Spider 数据）。"""
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    p = _TMP_BASE / f"mini-{uuid.uuid4().hex[:8]}.sqlite"
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE singer (singer_id INTEGER PRIMARY KEY, name TEXT, country TEXT);
        CREATE TABLE concert (concert_id INTEGER PRIMARY KEY, singer_id INTEGER,
                              year INTEGER, FOREIGN KEY(singer_id) REFERENCES singer(singer_id));
        INSERT INTO singer VALUES (1, 'Alice', 'US'), (2, 'Bob', 'UK'), (3, 'Cindy', 'US');
        INSERT INTO concert VALUES (1, 1, 2024), (2, 1, 2025), (3, 2, 2024);
    """)
    conn.commit()
    conn.close()
    return str(p)
