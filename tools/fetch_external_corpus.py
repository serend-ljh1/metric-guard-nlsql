#!/usr/bin/env python
"""拉取**官方**对抗语料，替换仓库自带的"按公开类别构造"版本。

为什么需要：`evaluation/sandbox_corpus/sqli_corpus.jsonl` 是按 OWASP WSTG / CWE-89 /
sqlmap tamper 类别**自己构造**的，能覆盖类别但不等于是社区维护的语料文件。
本脚本在有网机器上把官方语料拉下来、转成本项目的 JSONL 格式，并记录
**来源 URL + 抓取时间 + sha256**，让报告里的数字可追溯到外部来源。

用法（需联网）：
    python tools/fetch_external_corpus.py --source payloadsallthethings
    python tools/fetch_external_corpus.py --source payloadbox
    python tools/fetch_external_corpus.py --source sqlmap --sqlmap-dir <本地 sqlmap 仓库>
然后：
    python evaluation/eval_sandbox_external.py --corpus data/external/sqli_<source>.jsonl

说明：本脚本**不伪造**语料 —— 拉不到就报错退出，不会用生成的内容冒充官方语料。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlpa.config import ensure_utf8_console  # noqa: E402

SOURCES = {
    "payloadsallthethings": [
        "https://raw.githubusercontent.com/swisskyrepo/PayloadsAllTheThings/master/"
        "SQL%20Injection/Intruder/SQL-Injection.txt",
    ],
    "payloadbox": [
        "https://raw.githubusercontent.com/payloadbox/sql-injection-payload-list/master/"
        "Intruder/detect/Generic_SQLI.txt",
        "https://raw.githubusercontent.com/payloadbox/sql-injection-payload-list/master/"
        "Intruder/detect/MySQL.txt",
    ],
}

# 判定语料"意图"的机械规则（基于 payload 文本本身，不依赖来源的人工标注）
_DESTRUCTIVE = re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|TRUNCATE|REPLACE|MERGE|ATTACH|DETACH"
    r"|PRAGMA|VACUUM|REINDEX|GRANT|REVOKE|EXEC|EXECUTE|OUTFILE|DUMPFILE|SLEEP|BENCHMARK"
    r"|PG_SLEEP|LOAD_FILE|PG_READ_FILE|DBLINK|XP_CMDSHELL|LOAD_EXTENSION)\b", re.I)
_READONLY_HEAD = re.compile(r"^\s*(SELECT|WITH)\b", re.I)


def _classify(payload: str) -> str:
    if _DESTRUCTIVE.search(payload):
        return "destructive"
    if _READONLY_HEAD.match(payload):
        return "readonly"
    return "destructive"      # 既不是只读开头、又不含高危词 → 保守按危险算（期望被拦）


def _fetch(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "metric-guard-nlsql/corpus"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=sorted(SOURCES) + ["sqlmap"])
    ap.add_argument("--sqlmap-dir", default=None,
                    help="本地 sqlmap 仓库路径（--source sqlmap 时用；读 data/xml/payloads/*.xml）")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "external"))
    ap.add_argument("--max", type=int, default=400, help="最多保留多少条（控体积）")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_lines: list[str] = []
    provenance = {"source": args.source, "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "urls": [], "sha256": {}}

    if args.source == "sqlmap":
        if not args.sqlmap_dir:
            print("[!!] --source sqlmap 需要 --sqlmap-dir 指向本地 sqlmap 仓库")
            return 2
        base = Path(args.sqlmap_dir) / "data" / "xml" / "payloads"
        files = sorted(base.glob("*.xml"))
        if not files:
            print(f"[!!] 在 {base} 下没找到 payload XML")
            return 2
        for f in files:
            data = f.read_bytes()
            provenance["urls"].append(f"file://{f}")
            provenance["sha256"][f.name] = hashlib.sha256(data).hexdigest()
            # sqlmap 的 payload XML 里 payload 常带 <test> 标签；粗提取文本即可
            txt = re.sub(r"<[^>]+>", "\n", data.decode("utf-8", "replace"))
            raw_lines += [ln.strip() for ln in txt.splitlines() if ln.strip()]
    else:
        for url in SOURCES[args.source]:
            try:
                data = _fetch(url)
            except Exception as e:  # noqa: BLE001
                print(f"[!!] 拉取失败 {url}\n     {type(e).__name__}: {e}\n"
                      f"     本脚本不会用生成内容冒充官方语料，已退出。")
                return 2
            provenance["urls"].append(url)
            provenance["sha256"][url.rsplit("/", 1)[-1]] = hashlib.sha256(data).hexdigest()
            raw_lines += [ln.strip() for ln in data.decode("utf-8", "replace").splitlines()]

    # 过滤注释/空行，去重，截断
    payloads = [ln for ln in raw_lines
                if ln and not ln.startswith(("#", "//", "<!--", "--"))]
    seen, uniq = set(), []
    for p in payloads:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)

    rows = []
    for i, p in enumerate(uniq[:args.max], 1):
        rows.append({"id": f"{args.source}-{i:04d}", "family": f"external:{args.source}",
                     "intent": _classify(p), "expect": "block",
                     "payload": p, "source": f"{args.source}（外部语料，见 provenance）"})
    out = out_dir / f"sqli_{args.source}.jsonl"
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    (out_dir / f"sqli_{args.source}.provenance.json").write_text(
        json.dumps({**provenance, "rows": len(rows),
                    "corpus_sha256": hashlib.sha256(out.read_bytes()).hexdigest()},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {out}（{len(rows)} 条，截取自 {len(uniq)} 条）")
    print(f"来源与哈希见 {out_dir / f'sqli_{args.source}.provenance.json'}")
    print("注意：外部语料多为片段而非完整 SQL，跑评测时主要看『策略层拦截率』"
          "（执行层对片段必然报 SQL 错，属正常）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
