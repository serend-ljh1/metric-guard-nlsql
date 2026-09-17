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
def sample_db() -> str:
    """Olist 同结构小样本库（业务层测试用）。"""
    from build_olist_sample import build
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    p = _TMP_BASE / f"olist-{uuid.uuid4().hex[:8]}.db"
    build(p)
    return str(p)


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
