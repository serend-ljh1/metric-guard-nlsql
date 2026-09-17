"""
sqlpa.eval.console
==================
控制台编码兼容：Windows 默认 GBK，打印 "✓" / "¥" 等字符会抛 UnicodeEncodeError，
导致**所有结果都已算出/写盘后**进程却以非 0 退出（CI 会判为失败）。

修复前 `runner.py` 的进度行用了 "✓/✗"、`run_eval.py` 的汇总用了 "¥"，
在 Windows 控制台直接崩。
"""
from __future__ import annotations

import sys

_initialized = False


def ensure_utf8_console() -> None:
    """把 stdout/stderr 切到 UTF-8（失败则静默忽略，不影响主流程）。

    只在需要打印非 ASCII 的入口调用一次即可；幂等。
    """
    global _initialized
    if _initialized:
        return
    _initialized = True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 —— 某些环境（如被重定向的管道）不支持
            pass
