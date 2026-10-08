"""外部对抗语料回归：沙箱策略层必须挡住注入，且不能把只读查询误杀成一片。

这套用例守的是**本轮由外部语料发现的三个真缺口**（此前项目的安全自证完全没覆盖）：
  1. `pragma_*` 表值函数绕过「PRAGMA 一律拦截」（`\\bPRAGMA\\b` 匹配不到 `pragma_table_info`）；
  2. **`_BLOCKED_FUNCTIONS` 因大小写永不匹配 → 整份函数黑名单形同虚设**
     （`load_extension` 之前"看似被拦"，实际是 SQLite 默认禁用扩展加载而报错）；
  3. 跨方言危险函数（`SLEEP`/`BENCHMARK`/`LOAD_FILE`/`pg_sleep`/`dblink`/`INTO OUTFILE`）
     在 SQLite 上只是"函数不存在"，接到真实 MySQL/PG 就变成时间盲注与文件读取。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from sqlpa.sandbox.sql_executor import (ExecConfig, SqlSecurityError, SqlSandbox,
                                        _sanitize)

_ROOT = Path(__file__).resolve().parents[1]
_CORPUS = _ROOT / "evaluation" / "sandbox_corpus" / "sqli_corpus.jsonl"
_POLICY_REASONS = {"empty", "multi_statement", "not_select", "blocked_keyword",
                   "blocked_function"}
# 已知且**有意保留**的误杀：字符串字面量里含高危词（关键字黑名单的取舍）。
# 若这个集合变了，说明误杀面发生变化，需要重新评估而不是默默放过。
_KNOWN_FALSE_POSITIVES = {"ro-05"}


def _corpus() -> list:
    return [json.loads(l) for l in _CORPUS.read_text(encoding="utf-8").splitlines() if l.strip()]


def _policy(payload: str) -> str:
    try:
        _sanitize(payload, ExecConfig(max_rows=10))
        return ""
    except SqlSecurityError as e:
        return e.reason


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def corpus():
    return _corpus()


def test_dangerous_payloads_are_all_blocked(corpus):
    """危险语料（写入/DDL/多语句/危险函数/跨方言函数）策略层必须 100% 拦下。"""
    dangerous = [c for c in corpus if c["intent"] == "destructive"]
    assert len(dangerous) >= 40, "语料规模异常"
    slipped = [c["id"] for c in dangerous if _policy(c["payload"]) not in _POLICY_REASONS]
    assert not slipped, f"以下危险语料未被策略层拦截：{slipped}"


def test_readonly_false_positives_are_bounded_and_known(corpus):
    """只读语料不该被大面积误杀；已知误杀必须**显式列白**。

    这条同时是一道提醒：新增误杀会让用例变红，逼着人判断"这是取舍还是回归"。
    """
    readonly = [c for c in corpus if c["intent"] == "readonly"]
    blocked = {c["id"] for c in readonly if _policy(c["payload"]) in _POLICY_REASONS}
    assert blocked == _KNOWN_FALSE_POSITIVES, (
        f"只读误杀集合变化：{blocked}（已知 {_KNOWN_FALSE_POSITIVES}）")


def test_pragma_table_valued_functions_are_blocked():
    """回归：`pragma_*` 表值函数能枚举 schema —— 词边界匹配不到 `pragma_xxx`。"""
    for payload in ("SELECT COUNT(*) FROM pragma_table_info('orders')",
                    "SELECT name FROM pragma_database_list",
                    "SELECT COUNT(*) FROM pragma_table_list"):
        assert _policy(payload) == "blocked_function", payload


def test_blocked_functions_blacklist_actually_matches():
    """回归：函数黑名单曾因**大小写**永不匹配而形同虚设。

    判据：必须命中 `blocked_function`（策略拦截），而不是 `sqlite_error`
    （后者只是 SQLite 没这个函数，换到 MySQL 就会真的执行）。
    """
    cases = {
        "SELECT load_extension('/tmp/evil.so')": "SQLite 扩展加载",
        "SELECT SLEEP(10)": "MySQL 时间盲注",
        "SELECT BENCHMARK(1000000,MD5(1))": "MySQL 时间盲注",
        "SELECT LOAD_FILE('/etc/passwd')": "MySQL 任意文件读",
        "SELECT pg_sleep(10)": "PostgreSQL 时间盲注",
        "SELECT pg_read_file('/etc/passwd')": "PostgreSQL 任意文件读",
        "SELECT dblink('host=evil','SELECT 1')": "PostgreSQL 外联",
    }
    for sql, why in cases.items():
        assert _policy(sql) == "blocked_function", f"{why} 未被策略层拦截: {sql}"


def test_into_outfile_blocked_as_keyword():
    """MySQL `SELECT ... INTO OUTFILE` 是**写服务端文件**，必须按关键字拦。"""
    assert _policy("SELECT * FROM orders INTO OUTFILE '/tmp/x.txt'") == "blocked_keyword"


# ---------------------------------------------------------------- 执行层与库不变性

@pytest.fixture
def work_db(tmpdir_clean, sample_db):
    """拷贝一份样本库，用于验证"跑完整套语料后库一字未变"。"""
    import shutil
    p = tmpdir_clean / "work.db"
    shutil.copyfile(sample_db, p)
    return p


def test_corpus_run_leaves_database_untouched(corpus, work_db):
    """把整套语料（含只读与危险）灌进沙箱，库文件 hash 与各表行数必须一致。"""
    before_hash = _sha256(work_db)
    con = sqlite3.connect(work_db)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    con.close()

    sb = SqlSandbox(str(work_db), ExecConfig.from_settings(max_rows=50))
    for c in corpus:
        sb.execute(c["payload"])

    assert _sha256(work_db) == before_hash, "沙箱执行后库文件被改动了"
    con = sqlite3.connect(work_db)
    after = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    con.close()
    assert after == before


def test_bench_query_still_runs_under_corpus_rules(work_db):
    """反向对照：拦住注入的同时，业务主路径同构的只读查询必须照常可跑。"""
    sb = SqlSandbox(str(work_db), ExecConfig(max_rows=50))
    r = sb.execute(
        "SELECT c.customer_state, SUM(oi.price) AS gmv FROM orders o "
        "JOIN order_items oi ON o.order_id=oi.order_id "
        "JOIN customers c ON o.customer_id=c.customer_id "
        "WHERE o.order_status != 'canceled' GROUP BY c.customer_state LIMIT 5")
    assert r.ok and r.rows


def test_filter_value_injection_is_escaped(tmpdir_clean, sample_db):
    """过滤值里的注入串必须被当作**字面量**（单引号转义），且不得改动数据库。

    这里会撞上一个**已知取舍**：值被正确转义后，沙箱的关键字黑名单仍可能因为
    *字面量里出现了 DROP* 而拒绝整条 SQL（过度拦截）。因此判据不是"必须执行成功"，
    而是两条更硬的性质：① 注入串以字面量形式出现（没有变成 SQL 片段）；
    ② 无论如何库文件与行数不变 —— 这才是"注入没生效"的真正判据。
    """
    import hashlib
    from sqlpa.business.compiler import QuerySpec, compile_spec
    from sqlpa.business.metric_config import load_config

    cfg = load_config()
    sb = SqlSandbox(sample_db, ExecConfig(max_rows=50))
    before = hashlib.sha256(Path(sample_db).read_bytes()).hexdigest()
    outcomes = {}
    for value in ("SP' OR '1'='1", "SP'; DROP TABLE orders;--", "SP\\'", "SP%'"):
        cq = compile_spec(cfg, QuerySpec(metric="gmv", dims=[], filters=[("state", value)],
                                         top=1))
        assert "'" + value.replace("'", "''") + "'" in cq.sql, f"值未被转义: {value}"
        r = sb.execute(cq.sql)
        # 允许两种结果：执行成功，或按策略拦下（不得是 SQL 被值破坏后的 sqlite_error）
        assert r.ok or r.reason == "blocked_keyword", (value, r.reason, r.error)
        outcomes[value] = r.reason
    assert hashlib.sha256(Path(sample_db).read_bytes()).hexdigest() == before, \
        "注入值改动了数据库文件"
    # 至少有一条真的执行成功，证明"转义后仍是合法 SQL"（不是所有都被过度拦截掩盖）
    assert any(reason == "ok" for reason in outcomes.values()), outcomes
