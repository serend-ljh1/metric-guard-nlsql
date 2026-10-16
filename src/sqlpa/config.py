"""
sqlpa.config
============
加载 config/settings.yaml（业务/工程配置），并提供带默认值的取值。

设计意图：打分、阈值、重试上限、安全、记忆开关等都配置化，改规则不动代码。
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Dict, Optional
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
    """按点号路径取值，如 get('security.timeout_seconds', 5.0)。"""
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


# ---------------------------------------------------------------- 数据基准日（相对时间的锚点）
# 为什么必须有：Olist 数据窗口是 2016-09~2018-10，而「本月 / 上月 / 最近 30 天」这类
# 相对时间词若按系统当天（date.today()）解析，会落到 2026 年 —— 落在数据窗口之外，
# 于是任何相对时间提问都必然查空，表现为"能跑通流程但永远没有结果"。
# 正确做法：相对时间词一律锚定**数据自身的最新日期**（max(order_purchase_timestamp)）。
# 该锚点由入口层（api.py）按当前业务库探测并绑定；未绑定时回落系统当天（保持
# 纯离线/单元测试里"本月 = 系统当月"的语义不变）。
_DATA_ASOF: Dict[str, Any] = {"date": None, "source": "", "path": None}


def set_data_asof(day: Optional[datetime.date | str],
                  source: str = "") -> None:
    """显式设置数据基准日；传 None 表示清除（回落 date.today()）。"""
    if day is None:
        _DATA_ASOF["date"] = None
        _DATA_ASOF["source"] = ""
        _DATA_ASOF["path"] = None
        return
    if isinstance(day, str):
        day = datetime.date.fromisoformat(str(day)[:10])
    _DATA_ASOF["date"] = day
    _DATA_ASOF["source"] = source or ""


def data_asof() -> datetime.date:
    """当前生效的数据基准日：已绑定则用绑定值，否则用系统当天。"""
    return _DATA_ASOF["date"] or datetime.date.today()


def resolve_data_asof(db_path: str | Path, *,
                      table: str = "orders",
                      column: str = "order_purchase_timestamp") -> Optional[datetime.date]:
    """**只读**探测业务库的数据基准日（最新订单日期）。探测失败/空表 → None。

    用 URI `mode=ro` 打开，保证这一步不会（也不该）改动业务库。
    """
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
        try:
            row = con.execute(f"SELECT max({column}) FROM {table}").fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 —— 探测失败不阻塞取数
        return None
    val = row[0] if row else None
    if not val:
        return None
    try:
        return datetime.date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


def bind_data_asof(db_path: str | Path) -> Optional[datetime.date]:
    """探测并绑定基准日；探测不到则保持原有绑定（不误清）。"""
    day = resolve_data_asof(db_path)
    if day is not None:
        set_data_asof(day, source=f"{Path(db_path).name}::max(order_purchase_timestamp)")
    return day


def bind_data_asof_once(db_path: str | Path) -> Optional[datetime.date]:
    """按库路径缓存绑定：同一个库只探测一次，换库则重绑（幂等、零重复 IO）。"""
    p = str(db_path or "")
    if not p:
        return None
    if _DATA_ASOF.get("path") == p and _DATA_ASOF.get("date") is not None:
        return _DATA_ASOF["date"]
    day = bind_data_asof(p)
    _DATA_ASOF["path"] = p
    return day


_ISO_YMD_RE = re.compile(r"(\d{4})[年\-/](\d{1,2})(?:[月\-/]\d{1,2})?")


def canonical_time_spec(spec: Any) -> str:
    """把"能识别的显式时间语义"归一成**规范月份 token**（`YYYY-MM`）。

    为什么必须做：LLM 意图层对"2018年6月 GMV 为什么比上月跌？"这类**含比较语义**的问题，
    常把时间窗回成"上一期+当期"的拼接串，例如 `2018-05-01,2018-06-30` /
    `2018-05-01~2018-06-30`。这类串在 `compiler.resolve_time_range` 与
    `attribution._resolve_window` 里**都解析不了**，旧实现会**静默回落到"最近30天"**
    （数据基准日前 30 天）—— 于是当期被查到用户根本没问的窗口上，归因恒报"当期无数据"
    （表现为"流程跑通了、但结果永远是数据不足"）。

    归一规则（确定性、可复核）：
      - 串里出现"年月"（`2018年6月` / `2018-06` / `2018-05-01,2018-06-30`）→ 取**最后一个月**
        作为当期。含比较语义的拼接串总以"用户所问当月"结尾（…-05-01,2018-06-30 → 2018-06），
        与系统既定的"当期 vs 上月"比较模型一致；
      - 相对时间词（上月/本月/最近N天）原样保留，交给下游按数据基准日解析；
      - 无法识别 → 原样返回（保持既有默认行为，不新增语义）。

    放在解析**之前**（compiler 取数与 attribution 归因两个入口都调用），保证同一句话在
    "取数"和"归因"两条链上落到**同一个窗口**，不会一边查 6 月、一边查 9 月。
    """
    s = str(spec or "").strip()
    if not s:
        return s
    if any(k in s for k in ("上月", "上个月", "上上", "本月", "这个月", "天")):
        return s
    hits = _ISO_YMD_RE.findall(s)
    if hits:
        y, mo = hits[-1]
        return f"{int(y):04d}-{int(mo):02d}"
    return s


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
