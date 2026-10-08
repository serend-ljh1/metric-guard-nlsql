"""
sqlpa.config
============
加载 config/settings.yaml（业务/工程配置），并提供带默认值的取值。

设计意图：打分、阈值、重试上限、安全、记忆开关等都配置化，改规则不动代码。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import yaml

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
_CACHE: Dict[str, Any] = {}
_initialized = False


def load(path: str | Path | None = None) -> Dict[str, Any]:
    """读取 YAML 配置（带缓存）。默认读项目 config/settings.yaml。"""
    p = Path(path or _DEFAULT_PATH)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get(path: str, default: Any = None) -> Any:
    """按点号路径取值，如 get('pipeline.max_repair_round', 3)。"""
    if not _CACHE:
        _CACHE.update(load())
    node: Any = _CACHE
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def reset() -> None:
    _CACHE.clear()


# ---------------------------------------------------------------- 数据底座解析
_ROOT = Path(__file__).resolve().parents[2]

# 优先真实全量库，其次仓库自带的样本库（让 clone 之后开箱即跑）
DB_CANDIDATES = (
    ("data/olist/olist.db", "full"),
    ("data/sample/olist_sample.db", "sample"),
)


def resolve_db_path(explicit: str | Path | None = None,
                    root: str | Path | None = None) -> tuple[str, str]:
    """定位业务库，返回 (路径, 类型)。

    顺序：显式路径（含环境变量 SQLPA_DB_PATH）→ 真实全量库 → 仓库自带样本库。
    类型为 "full" / "sample"：调用方据此提示"当前跑在样本库上，指标值仅示意"。
    两者都不存在时抛 FileNotFoundError，并给出获取数据的命令。
    """
    base = Path(root or _ROOT)
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = base / p
        if p.exists():
            return str(p), ("full" if "olist.db" in p.name and "sample" not in str(p) else "sample")
        raise FileNotFoundError(f"指定的业务库不存在: {p}")
    for rel, kind in DB_CANDIDATES:
        p = base / rel
        if p.exists():
            return str(p), kind
    raise FileNotFoundError(
        "找不到业务库。两种选择：\n"
        "  1) 直接用仓库自带样本库（应存在于 data/sample/olist_sample.db）——"
        "若缺失请 `python tools/build_olist_sample.py`（需先有全量库）；\n"
        "  2) 导入真实 Olist 全量数据：`python tools/build_olist_db.py --src data/olist "
        "--out data/olist/olist.db`")


def repo_relative(p: str | Path | None) -> str:
    """把路径转成**仓库相对**形式，用于写进评测报告。

    为什么必须这么做：报告 JSON 是**随仓库发布**的产物。若直接写绝对路径，
    发布出去的就是"某台机器的目录结构"（`D:\\ds harness\\...` 或 `/home/某人/...`），
    既是信息泄漏，也让报告看起来像本机快照而不是可复核的证据。
    仓库外的路径（如另存的 Spider 数据集）退化为**文件名**：够用（数据集名在里面），
    但不会暴露本机的父目录命名。
    """
    if not p:
        return ""
    try:
        path = Path(p)
        return path.resolve().relative_to(Path(_ROOT).resolve()).as_posix()
    except (ValueError, OSError):
        return Path(p).name


def db_kind_note(kind: str) -> str:
    """给样本库运行加一句显式提示（避免把示意值当成全量口径的结论）。"""
    if kind == "sample":
        return ("⚠ 当前使用**仓库自带样本库**（按月抽样，非全量数据窗口）："
                "用于跑通流程与看结构，具体数值不代表全量口径结果。")
    return ""


def ensure_utf8_console() -> None:
    """把 stdout/stderr 切到 UTF-8（失败则静默忽略，不影响主流程）。

    只在需要打印非 ASCII 的入口调用一次即可；幂等。
    Windows 默认 GBK，打印 "✓"/"¥" 等字符会抛 UnicodeEncodeError，导致
    进程在结果已算出/写盘后却以非 0 退出（CI 会判为失败）。
    """
    global _initialized
    if _initialized:
        return
    _initialized = True
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 —— 某些环境（如被重定向的管道）不支持
            pass
