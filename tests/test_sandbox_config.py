"""沙箱配置化与超时护栏测试（离线）。

锁定两个历史问题：
  1) `config/settings.yaml` 的 security.* 键**从未被读取**（死配置）；
  2) 文档声称"强制查询超时"，实际只设置了 SQLite 的 busy_timeout（等锁），
     长查询仍会把进程挂住。
"""
from __future__ import annotations

import time

from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox


def test_exec_config_from_settings_reads_yaml():
    cfg = ExecConfig.from_settings()
    # settings.yaml 里 timeout_seconds: 5.0 / allow_multi_statement: false
    assert cfg.timeout_seconds == 5.0
    assert cfg.allow_multi_statement is False
    assert cfg.max_rows == 500            # eval.max_rows
    assert isinstance(cfg.extra_blocked_keywords, frozenset)


def test_from_settings_max_rows_override():
    assert ExecConfig.from_settings(max_rows=2000).max_rows == 2000


def test_custom_forbidden_keyword_blocks_query(mini_db):
    """forbid_keywords 里的词必须真的被拦（而不是只写在 yaml 里）。"""
    cfg = ExecConfig(extra_blocked_keywords=frozenset({"SINGER"}))
    sb = SqlSandbox(mini_db, cfg)
    r = sb.execute("SELECT count(*) FROM singer")
    assert r.ok is False
    assert r.reason == "blocked_keyword"


def test_query_timeout_actually_interrupts_long_query(mini_db):
    """真实超时：超时后长查询必须被中断，而不是一直跑下去。

    构造一个足够重的递归 CTE；把 timeout 设得很短，断言返回超时错误。
    """
    cfg = ExecConfig(timeout_seconds=0.2, max_rows=100)
    sb = SqlSandbox(mini_db, cfg)
    heavy = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 3000000) "
        "SELECT count(*) FROM c"
    )
    t0 = time.monotonic()
    r = sb.execute(heavy)
    elapsed = time.monotonic() - t0
    assert r.ok is False, "长查询未被超时中断（timeout_seconds 形同虚设）"
    assert elapsed < 5.0, f"中断耗时过长：{elapsed:.1f}s"


def test_fast_query_unaffected_by_timeout(mini_db):
    cfg = ExecConfig(timeout_seconds=5.0, max_rows=100)
    sb = SqlSandbox(mini_db, cfg)
    r = sb.execute("SELECT count(*) FROM singer")
    assert r.ok is True
    assert r.rows == [(3,)]
