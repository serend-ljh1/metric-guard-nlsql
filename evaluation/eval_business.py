"""评测 6：业务语义层与治理护栏（离线、确定性、无需 API Key）。

为什么需要这个：本项目 README 反复强调"核心不是 NL2SQL，而是语义层 + 治理"，
但此前**整个仓库没有任何针对语义层的评测**——口径命中、口径外降级、权限拦截、
PII 掩码、公式防篡改、审计留痕全都没有数字支撑。本脚本补上这块。

覆盖的指标（全部确定性，不调用 LLM）：
  1. 口径内命中率        —— 配置内的指标+维度组合能否被正确识别
  2. 指标识别准确率      —— 命中时 metric_key 是否与标注一致
  3. 维度识别准确率      —— 识别出的维度集合是否与标注一致
  4. 口径外拦截率        —— 无对应指标 / 维度组合不受支持时是否正确拒绝并给提示
  5. 拒绝原因可读性      —— 拒绝时是否给出可操作提示（而非空原因）
  6. 端到端执行成功率    —— 口径内问题是否真的跑出结果（确定性组装器）
  7. 公式防篡改拦截率    —— verify_formula 能否拦住被改动的指标公式
  8. 权限拦截率          —— 越权 SQL 是否被 check_access 拦下
  9. PII 掩码覆盖率      —— 敏感列（含 AS 别名改写）是否被掩码
 10. 审计完整性          —— 每次取数是否都写入审计留痕

用法：
    python evaluation/eval_business.py [--report-dir evaluation/reports]

产物：<report-dir>/business.json（可复核的逐例明细）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from sqlpa.eval.console import ensure_utf8_console  # noqa: E402

# ---------------------------------------------------------------- 标注集
# 每条：(问题, 期望)
#   expect="in"      → 口径内，metric/dims 必须匹配
#   expect="reject"  → 口径外（无指标 或 指标-维度组合不受支持），必须被拒绝
# reasons: 该题考查的点（写在报告里，便于人看）
CASES = [
    # ---- 口径内：指标 + 维度组合（均取自 business_config.yaml 的 support_dims）----
    {"q": "各个品类的GMV", "expect": "in", "metric": "gmv", "dims": ["category"],
     "note": "gmv 支持 category"},
    {"q": "每个州的GMV", "expect": "in", "metric": "gmv", "dims": ["state"],
     "note": "gmv 支持 state"},
    {"q": "订单数是多少", "expect": "in", "metric": "order_count", "dims": [],
     "note": "order_count 无维度"},
    {"q": "每个品类的订单数", "expect": "in", "metric": "order_count", "dims": ["category"],
     "note": "order_count 支持 category"},
    {"q": "客单价是多少", "expect": "in", "metric": "aov", "dims": [],
     "note": "aov 无维度"},
    {"q": "每个州的取消率", "expect": "in", "metric": "cancellation_rate", "dims": ["state"],
     "note": "取消率 支持 state"},
    {"q": "平均评分", "expect": "in", "metric": "avg_review", "dims": [],
     "note": "avg_review 无维度"},

    # ---- 口径外：指标-维度组合不受支持（必须拒绝，而不是硬生成）----
    {"q": "客单价按品类", "expect": "reject", "metric": "aov", "dims": ["category"],
     "note": "aov.support_dims 只有 dt/state，不含 category"},
    {"q": "超时送达率按品类", "expect": "reject", "metric": "late_delivery_rate",
     "dims": ["category"], "note": "late_delivery_rate 不支持 category"},

    # ---- 口径外：无对应指标（必须拒绝并给可读提示）----
    {"q": "每个客服的响应时长是多少", "expect": "reject", "note": "配置里没有这类指标"},
    {"q": "今天天气怎么样", "expect": "reject", "note": "完全无关问题"},

    # ---- 时间过滤 ----
    {"q": "最近30天的GMV", "expect": "in", "metric": "gmv", "dims": [], "filters": ["time_range"],
     "note": "时间范围过滤"},
]


def _build_sandbox(db: str):
    from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox
    return SqlSandbox(db, ExecConfig.from_settings(max_rows=200))


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "olist_sample" / "sample.db"),
                    help="业务库（默认用同结构小样本，离线确定性）")
    ap.add_argument("--report-dir", default=str(ROOT / "evaluation" / "reports"))
    args = ap.parse_args()

    from sqlpa.business.metric_config import load_config
    from sqlpa.business.metric_guard import build_constraint, formula_of, verify_formula
    from sqlpa.business.permissions import check_access, mask_result
    from sqlpa.business.service import answer
    from sqlpa.business import storage

    cfg = load_config()
    db = args.db
    if not Path(db).exists():
        print(f"[!!] 业务库不存在：{db}（可先跑 tools/build_olist_sample.py）")
        return 1
    sb = _build_sandbox(db)

    print("=" * 64)
    print("评测：业务语义层与治理护栏（离线确定性）")
    print(f"业务库: {db}")
    print(f"指标数: {len(cfg.metrics)}  维度数: {len(cfg.dimensions)}")
    print("=" * 64)

    details = []
    n_in = n_in_hit = n_in_exec = 0
    n_metric_ok = n_dims_ok = 0
    n_rej = n_rej_ok = n_rej_readable = 0
    n_audit_ok = 0

    print("\n[1-6] 口径识别 / 分级放行 / 执行")
    for c in CASES:
        q = c["q"]
        t0 = time.time()
        a = answer(q, cfg, sb, db, llm=None, role="analyst", username="eval")
        ms = (time.time() - t0) * 1000

        matched = bool(a.get("matched"))
        ok = bool(a.get("ok"))
        reject = a.get("reject") or ""
        row = {"q": q, "expect": c["expect"], "note": c.get("note", ""),
               "matched": matched, "ok": ok, "mode": a.get("mode"),
               "metric": (a.get("metric") or ""), "dims": a.get("dims") or [],
               "reject": reject, "rows": len(a.get("rows") or []),
               "reject_reasons": a.get("reject_reasons") or [],
               "latency_ms": round(ms, 1)}

        if c["expect"] == "in":
            n_in += 1
            # 命中判定：必须 matched 且指标一致
            metric_ok = matched and (a.get("metric") == c.get("metric"))
            dims_ok = matched and (set(a.get("dims") or []) == set(c.get("dims") or []))
            row["metric_ok"] = metric_ok
            row["dims_ok"] = dims_ok
            n_metric_ok += int(metric_ok)
            n_dims_ok += int(dims_ok)
            n_in_hit += int(matched)
            n_in_exec += int(ok)
            flag = "✓" if (metric_ok and dims_ok) else "✗"
        else:
            n_rej += 1
            rejected = (not matched) and (not ok) and bool(reject)
            readable = bool(reject) and len(reject) >= 8
            row["rejected"] = rejected
            row["readable"] = readable
            n_rej_ok += int(rejected)
            n_rej_readable += int(readable)
            flag = "✓" if rejected else "✗"

        # 审计留痕
        audits = storage.list_audit(limit=500)
        row["audited"] = any(x.get("user_input") == q for x in audits)
        n_audit_ok += int(row["audited"])
        details.append(row)
        print(f"  {flag} [{c['expect']:6s}] {q:16s} metric={row['metric'] or '-':18s} "
              f"dims={row['dims']} rows={row['rows']} {ms:.0f}ms")

    # ---------------------------------------------------------------- 7) 公式防篡改
    print("\n[7] 公式防篡改（verify_formula）")
    gmv_expr = formula_of(cfg, "gmv")
    # 第 4 条是本项目**已知的、真实的**局限：verify_formula 是"表达式子串包含"判定，
    # 只要保留 SUM(oi.price) 这个子串、再在外面乘系数，校验就会放行。
    # 这里如实测出来（预期"漏"，用来量化局限），而不是假装它 100% 拦住。
    guard_cases = [
        (f"SELECT {gmv_expr} FROM orders o JOIN order_items oi ON o.order_id=oi.order_id", False,
         "原样使用配置公式 → 应通过"),
        ("SELECT SUM(oi.price)*0.5 FROM orders o JOIN order_items oi ON o.order_id=oi.order_id", True,
         "私自改系数（子串仍在，预期**拦不住**——已知局限）"),
        ("SELECT COUNT(*) FROM orders", True, "换成完全不同的口径 → 应拦截"),
        ("SELECT 1 FROM orders WHERE 1=0", True, "空壳 SQL（公式被删）→ 应拦截"),
    ]
    n_guard = n_guard_ok = 0
    guard_details = []
    for sql, should_block, note in guard_cases:
        issues = verify_formula(sql, gmv_expr)
        blocked = bool(issues)
        good = (blocked == should_block)
        n_guard += 1
        n_guard_ok += int(good)
        guard_details.append({"sql": sql[:70], "should_block": should_block,
                              "blocked": blocked, "issues": issues, "note": note})
        print(f"  {'✓' if good else '✗'} {note}")

    # ---------------------------------------------------------------- 8) 权限拦截
    print("\n[8] 权限拦截（check_access）")
    from sqlpa.data.schema_extractor import extract_from_sqlite
    schema = extract_from_sqlite(db, "olist").to_dict()
    # 注意：必须用**该库真实存在**的敏感列。配置里 sensitive_columns 还列了
    # customer_phone，但 Olist 的 customers 表没有 phone 列，用它做用例会因为
    # 解析不到归属表而"通过"，那是在测一个不存在的场景。
    perm_cases = [
        ("SELECT customer_zip_code_prefix FROM customers", "analyst", True,
         "非限定敏感列（历史绕过点）"),
        ("SELECT c.customer_zip_code_prefix FROM customers c", "analyst", True, "限定敏感列"),
        ("SELECT customer_zip_code_prefix AS zip FROM customers", "analyst", True,
         "别名改写敏感列"),
        ("SELECT customer_city FROM customers", "analyst", False, "允许列不应被拦"),
        ("SELECT customer_zip_code_prefix FROM customers", "admin", False, "admin 全放行"),
    ]
    n_perm = n_perm_ok = 0
    perm_details = []
    for sql, role, should_block, note in perm_cases:
        bad = check_access(role, cfg.permissions, sql, schema=schema)
        blocked = bool(bad)
        good = (blocked == should_block)
        n_perm += 1
        n_perm_ok += int(good)
        perm_details.append({"sql": sql, "role": role, "should_block": should_block,
                             "blocked": blocked, "issues": bad, "note": note})
        print(f"  {'✓' if good else '✗'} [{role}] {note}")

    # ---------------------------------------------------------------- 9) PII 掩码
    print("\n[9] PII 掩码覆盖（含别名绕过）")
    import sqlite3
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    mask_cases = [
        ("SELECT customer_zip_code_prefix FROM customers",
         ["customer_zip_code_prefix"], "原列名"),
        ("SELECT customer_zip_code_prefix AS zip FROM customers", ["zip"], "AS 别名"),
    ]
    n_mask = n_mask_ok = 0
    mask_details = []
    for sql, headers, note in mask_cases:
        try:
            cur = conn.execute(sql)
            rows = [tuple(r) for r in cur.fetchmany(3)]
        except Exception as e:  # noqa: BLE001
            mask_details.append({"sql": sql, "note": note, "error": str(e)})
            n_mask += 1
            print(f"  ✗ {note}: 查询失败 {e}")
            continue
        masked = mask_result(headers, rows, cfg.permissions.get("sensitive_columns", {}),
                             sql=sql, perms=cfg.permissions, schema=schema)
        raw_vals = {str(r[0]) for r in rows}
        masked_vals = {str(r[0]) for r in masked}
        leaked = raw_vals & masked_vals
        good = not leaked
        n_mask += 1
        n_mask_ok += int(good)
        mask_details.append({"sql": sql, "note": note, "raw": [list(r) for r in rows],
                             "masked": [list(r) for r in masked],
                             "leaked": sorted(leaked)})
        print(f"  {'✓' if good else '✗'} {note}: raw={sorted(raw_vals)} masked={sorted(masked_vals)}")
    conn.close()

    # ---------------------------------------------------------------- 汇总
    def pct(a, b):
        return round(a / b, 4) if b else 0.0

    report = {
        "db": str(db),
        "n_cases": len(CASES),
        "in_scope_hit_rate": pct(n_in_hit, n_in),
        "metric_accuracy": pct(n_metric_ok, n_in),
        "dims_accuracy": pct(n_dims_ok, n_in),
        "in_scope_exec_rate": pct(n_in_exec, n_in),
        "reject_accuracy": pct(n_rej_ok, n_rej),
        "reject_readability": pct(n_rej_readable, n_rej),
        "guard_block_rate": pct(n_guard_ok, n_guard),
        "permission_block_rate": pct(n_perm_ok, n_perm),
        "pii_mask_rate": pct(n_mask_ok, n_mask),
        "audit_coverage": pct(n_audit_ok, len(CASES)),
        "counts": {"in": n_in, "in_hit": n_in_hit, "reject": n_rej, "reject_ok": n_rej_ok,
                   "guard": n_guard, "guard_ok": n_guard_ok,
                   "perm": n_perm, "perm_ok": n_perm_ok,
                   "mask": n_mask, "mask_ok": n_mask_ok, "audited": n_audit_ok},
        "details": details,
        "guard_details": guard_details,
        "permission_details": perm_details,
        "mask_details": mask_details,
    }

    print("\n" + "=" * 64)
    print("===== 业务语义层评测汇总 =====")
    print(f"  口径内命中率      : {report['in_scope_hit_rate']:.1%} ({n_in_hit}/{n_in})")
    print(f"  指标识别准确率    : {report['metric_accuracy']:.1%} ({n_metric_ok}/{n_in})")
    print(f"  维度识别准确率    : {report['dims_accuracy']:.1%} ({n_dims_ok}/{n_in})")
    print(f"  口径内执行成功率  : {report['in_scope_exec_rate']:.1%} ({n_in_exec}/{n_in})")
    print(f"  口径外拦截率      : {report['reject_accuracy']:.1%} ({n_rej_ok}/{n_rej})")
    print(f"  拒绝原因可读率    : {report['reject_readability']:.1%} ({n_rej_readable}/{n_rej})")
    print(f"  公式防篡改拦截率  : {report['guard_block_rate']:.1%} ({n_guard_ok}/{n_guard})")
    print(f"  权限拦截率        : {report['permission_block_rate']:.1%} ({n_perm_ok}/{n_perm})")
    print(f"  PII 掩码覆盖率    : {report['pii_mask_rate']:.1%} ({n_mask_ok}/{n_mask})")
    print(f"  审计覆盖率        : {report['audit_coverage']:.1%} ({n_audit_ok}/{len(CASES)})")
    print("=" * 64)

    out_dir = Path(args.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "business.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
