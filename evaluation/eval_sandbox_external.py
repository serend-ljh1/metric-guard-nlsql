#!/usr/bin/env python
"""沙箱外部对抗语料评测（SQL 注入 / 越权写入 / 编码绕过）。

**为什么需要**：此前"沙箱只读、语句级拦截"的证据全部来自项目自己的测试（自证）。
本脚本用一份**显式标注来源**的对抗语料打进去，分别报三个数：

  1. **危险语料拦截率**（intent=destructive）：写入/DDL/多语句/危险函数 → 必须被拦；
  2. **只读语料误杀率**（intent=readonly）：合法的只读查询（含 UNION、CTE、字符串里出现
     高危词）→ 不该被拦。**这条是旧评测完全没测过的方向**（关键字黑名单天然会误杀字面量）；
  3. **库不变性**：跑完整个语料后，原库文件 sha256 与各表行数必须一字不变。

另有第 4 组：**过滤值注入**（走 compiler 的 `render_filter` → `_quote`），验证注入串被当作
字面量而不是 SQL 片段。

语料来源诚实声明：本仓库的 `evaluation/sandbox_corpus/sqli_corpus.jsonl` 是**按公开类别
（OWASP WSTG / CWE-89 / sqlmap tamper 类别 / 各数据库官方危险功能文档）构造**的，
不是官方语料文件本身。要换成 sqlmap / PayloadsAllTheThings 的原始语料，在有网机器上跑：
    python tools/fetch_external_corpus.py --source payloadsallthethings
再用 `--corpus data/external/<file>` 复跑（来源 URL 与 sha256 会记进报告）。

跑法：
    python evaluation/eval_sandbox_external.py                    # 用自带语料（离线、可进 CI）
    python evaluation/eval_sandbox_external.py --corpus <file>     # 换外部语料
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.config import db_kind_note, ensure_utf8_console, repo_relative, resolve_db_path  # noqa: E402
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox  # noqa: E402

_CORPUS = ROOT / "evaluation" / "sandbox_corpus" / "sqli_corpus.jsonl"


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _table_counts(db: str) -> dict:
    con = sqlite3.connect(db)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in names}
    finally:
        con.close()


def _load_corpus(path: Path) -> list:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="业务库（默认：全量库 > 样本库）")
    ap.add_argument("--corpus", default=str(_CORPUS))
    ap.add_argument("--report-dir", default=str(ROOT / "evaluation" / "reports"))
    args = ap.parse_args()

    try:
        db, kind = resolve_db_path(args.db)
    except FileNotFoundError as e:
        print(f"[SKIP] {e}")
        return 0
    if db_kind_note(kind):
        print(db_kind_note(kind))

    corpus_path = Path(args.corpus)
    corpus = _load_corpus(corpus_path)
    sb = SqlSandbox(db, ExecConfig.from_settings(max_rows=200))

    before_hash, before_counts = _file_sha256(Path(db)), _table_counts(db)

    results = []
    # 策略拦截的 reason 取值（与 sql_executor 的 SqlSecurityError.reason 对齐）；
    # 其它失败（如 sqlite_error）是"SQL 本身写错/片段不完整"，**不能算作被拦**，
    # 否则会把度量做成"随便写条错 SQL 也算防御成功"。
    POLICY_REASONS = {"empty", "multi_statement", "not_select", "blocked_keyword",
                      "blocked_function"}
    from sqlpa.sandbox.sql_executor import SqlSecurityError, _sanitize  # noqa: E402
    for item in corpus:
        payload = item["payload"]
        # ---- 第 1 层：策略层（方言无关的语句级校验）----
        try:
            _sanitize(payload, sb.config)
            policy_reason = ""
        except SqlSecurityError as e:
            policy_reason = e.reason
        except Exception as e:  # noqa: BLE001
            policy_reason = f"unexpected:{type(e).__name__}"
        # ---- 第 2 层：执行层（当前连接是 SQLite）----
        r = sb.execute(payload)
        policy_block = policy_reason in POLICY_REASONS
        results.append({**item, "blocked": policy_block, "policy_reason": policy_reason,
                        "executed": bool(r.ok), "reason": r.reason,
                        "error": (r.error or "")[:120]})
        mark = ("BLOCK" if policy_block else
                ("ALLOW" if r.ok else "SQLERR"))
        print(f"  [{mark:6s}] {item['id']:10s} {item['family']:20s} {payload[:52]}")

    after_hash, after_counts = _file_sha256(Path(db)), _table_counts(db)

    dangerous = [r for r in results if r["intent"] == "destructive"]
    readonly = [r for r in results if r["intent"] == "readonly"]
    blocked_dangerous = [r for r in dangerous if r["blocked"]]
    blocked_readonly = [r for r in readonly if r["blocked"]]      # 误杀
    slipped = [r for r in dangerous if not r["blocked"]]          # 绕过
    sql_errors = [r for r in results if not r["blocked"] and not r["executed"]]

    # ---- 第 4 组：过滤值注入（走 compiler 的转义）----
    value_cases = ["SP' OR '1'='1", "SP'; DROP TABLE orders;--", "SP\\'", "SP%'", "SP''"]
    from sqlpa.business.compiler import CompileError, QuerySpec, compile_spec  # noqa: E402
    from sqlpa.business.metric_config import load_config  # noqa: E402
    cfg = load_config()
    fv_results = []
    for v in value_cases:
        try:
            cq = compile_spec(cfg, QuerySpec(metric="gmv", dims=[], filters=[("state", v)],
                                             top=1))
            exec_ok = sb.execute(cq.sql).ok
            # 值必须以字面量形式出现（单引号被转义成两个），且不得出现分号注入
            safe = "'" + v.replace("'", "''") + "'" in cq.sql
            fv_results.append({"value": v, "compiled_ok": True, "executed": exec_ok,
                               "value_is_literal": safe, "sql": cq.sql.splitlines()[-1][:90]})
        except CompileError as e:
            fv_results.append({"value": v, "compiled_ok": False, "error": str(e)[:80]})

    report = {
        "suite": "sandbox_external_corpus",
        "corpus": str(corpus_path),
        "corpus_sha256": _file_sha256(corpus_path),
        "corpus_size": len(corpus),
        "corpus_note": ("按 OWASP WSTG / CWE-89 / sqlmap tamper 类别 / 各库官方危险功能文档"
                        "构造（非官方语料文件）；可用 tools/fetch_external_corpus.py 换官方语料"),
        "db": repo_relative(db), "db_kind": kind,
        "dangerous_total": len(dangerous), "dangerous_blocked": len(blocked_dangerous),
        "dangerous_block_rate": round(len(blocked_dangerous) / len(dangerous), 4) if dangerous else None,
        "bypasses": [r["id"] for r in slipped],
        "readonly_total": len(readonly), "readonly_blocked": len(blocked_readonly),
        "readonly_false_positive_rate": round(len(blocked_readonly) / len(readonly), 4) if readonly else None,
        "false_positives": [{"id": r["id"], "payload": r["payload"], "reason": r["reason"]}
                            for r in blocked_readonly],
        "db_unchanged_hash": before_hash == after_hash,
        "db_unchanged_counts": before_counts == after_counts,
        "sql_error_count": len(sql_errors),
        "sql_errors": [{"id": r["id"], "error": r["error"]} for r in sql_errors],
        "filter_value_injection": fv_results,
        "details": results,
    }
    out_dir = Path(args.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sandbox_external.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 68)
    print(f"语料 {len(corpus)} 条（sha256 {report['corpus_sha256'][:12]}…）  库: {kind}")
    print(f"  策略层·危险语料拦截: {len(blocked_dangerous)}/{len(dangerous)}"
          f" = {(report['dangerous_block_rate'] or 0):.1%}"
          + (f"   ⚠ 绕过: {report['bypasses']}" if slipped else "   无绕过"))
    print(f"  策略层·只读语料误杀: {len(blocked_readonly)}/{len(readonly)}"
          f" = {(report['readonly_false_positive_rate'] or 0):.1%}"
          + (f"   ⚠ 误杀: {[r['id'] for r in blocked_readonly]}" if blocked_readonly else ""))
    print(f"  库不变性: 文件 hash {'一致' if report['db_unchanged_hash'] else '⚠ 变了'}"
          f" / 表行数 {'一致' if report['db_unchanged_counts'] else '⚠ 变了'}")
    if sql_errors:
        print(f"  语料本身非完整 SQL（不计入拦截/误杀）: {len(sql_errors)} 条"
              f" {[r['id'] for r in sql_errors]}")
    print(f"  过滤值注入: {sum(1 for x in fv_results if x.get('value_is_literal'))}"
          f"/{len(fv_results)} 被当作字面量")
    print(f"产物 -> {out_dir / 'sandbox_external.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
