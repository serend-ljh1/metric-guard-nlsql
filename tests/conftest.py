"""
tests/conftest.py
=================
pytest 公共 fixture：
  - sample_db  : Olist 同结构小样本库（会话级，离线可用）
  - mini_db    : 内置迷你 schema（singer/concert），引擎测试不依赖本地 Spider 数据，
                 保证 CI 可复现
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # 让 `import api` 可用（api.py 在项目根）
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))


@pytest.fixture(scope="session")
def sample_db(tmp_path_factory) -> str:
    """Olist 同结构小样本库（业务层测试用）。"""
    from build_olist_sample import build
    p = tmp_path_factory.mktemp("olist") / "sample.db"
    build(p)
    return str(p)


@pytest.fixture(scope="session")
def mini_db(tmp_path_factory) -> str:
    """迷你两表库（引擎链路测试用，无需 Spider 数据）。"""
    p = tmp_path_factory.mktemp("mini") / "mini.sqlite"
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
