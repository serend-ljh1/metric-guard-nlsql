"""
tools/download_data.py
======================
下载/校验 Text-to-SQL 公开基准（Spider / BIRD），并确保目录结构能被 run_eval.py 读取。

⚠️ 诚实说明（重要）：
  - 本仓库/这台机器 **无法直接访问 raw.githubusercontent 与 huggingface.co**（网络受限），
    且预置的官方 zip 链接可能已失效(404)。因此不建议依赖自动下载。
  - **推荐的稳定方式：用浏览器手动下载 + `--verify` 校验结构**（见下方 usage）。

用法：
  # 方式 A（推荐）：浏览器下载 zip 后，用 --url 或直接放好再校验结构
  python tools/download_data.py --verify --dataset spider --dir data/spider

  # 方式 B：你有直链 zip，让脚本下载并解压
  python tools/download_data.py --dataset spider --dir data/spider --url <zip链接>

  # BIRD 需先到官网同意条款（https://bird-bench.github.io/）。
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
import zipfile
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0"}

# run_eval 期望的结构
EXPECTED_DB = ("database", "database/")          # sqlite 库目录
EXPECTED_JSON = ("dev.json", "test.json")        # 评测 split


def download(url: str, dest: Path) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        print(f"  下载: {url}")
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        dest.write_bytes(data)
        print(f"  -> OK {dest}  {len(data)/(1024*1024):.1f} MB")
        return dest
    except Exception as e:  # noqa: BLE001
        print(f"  下载失败: {type(e).__name__}: {str(e)[:100]}")
        return None


def extract(zp: Path, out: Path) -> None:
    with zipfile.ZipFile(zp) as z:
        # 若 zip 内含单个顶层目录，则去除该层，直接落到 out
        names = z.namelist()
        top = Path(names[0]).parts[0] if names else None
        same_root = all(n.startswith(top + "/") for n in names) if top else False
        target = out if same_root else out / top
        target.mkdir(parents=True, exist_ok=True)
        z.extractall(target)
        root = (out if same_root else out / top)
        print(f"  已解压到: {root}")


def verify_dir(root: Path, dataset: str) -> None:
    print(f"\n== 校验 {dataset} 结构（run_eval 读取所需）==")
    db_root = root / "database"
    if db_root.exists():
        n_db = sum(1 for _ in db_root.glob("*"))
        print(f"  [OK] database/ 存在，含 {n_db} 个库目录")
    else:
        print(f"  [!!] 缺少 {root / 'database'}（含各库的 .db 文件）")

    has_json = any((root / j).exists() for j in EXPECTED_JSON)
    json_present = [j for j in EXPECTED_JSON if (root / j).exists()]
    if json_present:
        print(f"  [OK] 评测 json 存在: {json_present}")
    else:
        print(f"  [!!] 缺少评测 json（如 dev.json/test.json）；"
              f"Spider 的 dev/test 需单独下载放入 {root}")

    print("\n  run_eval 将用: "
          f"python run_eval.py --dataset {dataset} --db-root {root} --split dev [--ablation --limit 50]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["spider", "bird", "all"], default="spider")
    ap.add_argument("--dir", default="data", help="数据集目标目录，如 data/spider")
    ap.add_argument("--url", default=None, help="直链 zip（可选）")
    ap.add_argument("--verify", action="store_true", help="只校验目录结构，不下载")
    args = ap.parse_args()
    root = Path(args.dir) / args.dataset

    if args.verify:
        verify_dir(root, args.dataset)
        return 0

    root.mkdir(parents=True, exist_ok=True)
    if args.url:
        zp = download(args.url, root / "dataset.zip")
        if zp:
            extract(zp, root)
    else:
        print("未提供 --url，跳过自动下载。")
        print("请用浏览器下载 Spider（官方站 / GitHub / HuggingFace 任一可达源），")
        print(f"然后解压到: {root}  使其包含: database/ 与 dev.json")

    verify_dir(root, args.dataset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
