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
