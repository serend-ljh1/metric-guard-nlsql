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
