"""
sqlpa.business.metric_store
===========================
指标中心的持久化层：对 business_config.yaml 做带校验的增删改（指标/维度/别名）。

这是「指标管理界面」的后端——语义层不再是只读配置文件，而是可运营、可维护的
指标中心；所有修改仍写回同一份权威配置，保证"公式只来自配置"的原则不变。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

_DEFAULT = Path(__file__).resolve().parent / "business_config.yaml"

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def load_raw(path: str | Path | None = None) -> Dict:
    p = Path(path or _DEFAULT)
    return yaml.safe_load(open(p, encoding="utf-8")) or {}


def save_raw(data: Dict, path: str | Path | None = None) -> None:
    p = Path(path or _DEFAULT)
    header = ("# ============================================================================\n"
              "# 业务语义层配置（配置化，防 LLM 篡改业务指标公式）\n"
              "# 本文件由「指标中心」维护：指标的分子分母公式、JOIN、依赖表全部写在配置里；\n"
              "# LLM 只识别\"用户要哪个指标/维度/过滤\"，不得自行编写业务公式。\n"
              "# ============================================================================\n")
    with open(p, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=120)


def _check_key(key: str) -> List[str]:
    if not key or not _KEY_RE.match(key):
        return [f"key「{key}」不合法：需以小写字母开头，仅含小写字母/数字/下划线"]
    return []


def validate_metric(data: Dict, metric: Dict, editing_key: str = "") -> List[str]:
    """校验一条指标定义，返回错误列表（空=合法）。"""
    errs: List[str] = []
    key = (metric.get("key") or "").strip()
    errs += _check_key(key)
    keys = {m.get("key") for m in data.get("metrics", [])}
    if key and key != editing_key and key in keys:
        errs.append(f"指标 key「{key}」已存在")
    if not (metric.get("name") or "").strip():
        errs.append("指标名称不能为空")
    expr = (metric.get("metric_expr") or "").strip()
    if not expr:
        errs.append("计算表达式（公式）不能为空——这是口径的核心")
    fc = (metric.get("from_clause") or "").strip()
    if not fc.upper().startswith("FROM"):
        errs.append("数据来源 from_clause 必须以 FROM 开头")
    dim_keys = {d.get("key") for d in data.get("dimensions", [])}
    ft_keys = set((data.get("filter_templates") or {}).keys())
    for d in metric.get("support_dims", []):
        if d not in dim_keys:
            errs.append(f"支持维度「{d}」未在维度表中定义")
    for f in metric.get("support_filters", []):
        if f not in ft_keys:
            errs.append(f"支持过滤「{f}」未在过滤模板中定义")
    return errs


def upsert_metric(metric: Dict, editing_key: str = "",
                  path: str | Path | None = None) -> Tuple[bool, str]:
    """新增或更新一条指标。返回 (成功?, 消息)。"""
    p = Path(path or _DEFAULT)
    data = load_raw(p)
    errs = validate_metric(data, metric, editing_key)
    if errs:
        return False, "；".join(errs)
    m = {
        "key": metric["key"].strip(),
        "name": metric["name"].strip(),
        "desc": (metric.get("desc") or "").strip(),
        "metric_expr": metric["metric_expr"].strip(),
        "from_clause": metric["from_clause"].strip(),
        "where_core": (metric.get("where_core") or "1=1").strip() or "1=1",
        "support_dims": list(metric.get("support_dims", [])),
        "support_filters": list(metric.get("support_filters", [])),
    }
    metrics = data.setdefault("metrics", [])
    if editing_key:
        metrics[:] = [x for x in metrics if x.get("key") != editing_key]
    metrics.append(m)
    save_raw(data, p)
    return True, f"指标「{m['name']}」已保存"


def delete_metric(key: str, path: str | Path | None = None) -> Tuple[bool, str]:
    p = Path(path or _DEFAULT)
    data = load_raw(p)
    before = len(data.get("metrics", []))
    data["metrics"] = [m for m in data.get("metrics", []) if m.get("key") != key]
    if len(data["metrics"]) == before:
        return False, f"未找到指标「{key}」"
    save_raw(data, p)
    return True, f"指标「{key}」已删除"


def upsert_dimension(dim: Dict, editing_key: str = "",
                     path: str | Path | None = None) -> Tuple[bool, str]:
    p = Path(path or _DEFAULT)
    data = load_raw(p)
    errs = _check_key((dim.get("key") or "").strip())
    if not (dim.get("name") or "").strip():
        errs.append("维度名称不能为空")
    if not (dim.get("sql_fragment") or "").strip():
        errs.append("维度 SQL 片段不能为空")
    keys = {d.get("key") for d in data.get("dimensions", [])}
    if dim.get("key") and dim["key"] != editing_key and dim["key"] in keys:
        errs.append(f"维度 key「{dim['key']}」已存在")
    if errs:
        return False, "；".join(errs)
    d = {"key": dim["key"].strip(), "name": dim["name"].strip(),
         "sql_fragment": dim["sql_fragment"].strip()}
    dims = data.setdefault("dimensions", [])
    if editing_key:
        dims[:] = [x for x in dims if x.get("key") != editing_key]
    dims.append(d)
    save_raw(data, p)
    return True, f"维度「{d['name']}」已保存"


def delete_dimension(key: str, path: str | Path | None = None) -> Tuple[bool, str]:
    p = Path(path or _DEFAULT)
    data = load_raw(p)
    used = [m.get("key") for m in data.get("metrics", [])
            if key in (m.get("support_dims") or [])]
    if used:
        return False, f"维度「{key}」正被指标 {','.join(used)} 使用，不能删除"
    before = len(data.get("dimensions", []))
    data["dimensions"] = [d for d in data.get("dimensions", []) if d.get("key") != key]
    if len(data["dimensions"]) == before:
        return False, f"未找到维度「{key}」"
    save_raw(data, p)
    return True, f"维度「{key}」已删除"


def upsert_alias(name: str, table: str, col: str = "",
                 path: str | Path | None = None) -> Tuple[bool, str]:
    p = Path(path or _DEFAULT)
    data = load_raw(p)
    name, table = (name or "").strip(), (table or "").strip()
    if not name or not table:
        return False, "别名与表名不能为空"
    alias = data.setdefault("business_alias", {})
    entry = {"table": table}
    if col.strip():
        entry["col"] = col.strip()
    alias[name] = entry
    save_raw(data, p)
    return True, f"别名「{name}」已保存"


def delete_alias(name: str, path: str | Path | None = None) -> Tuple[bool, str]:
    p = Path(path or _DEFAULT)
    data = load_raw(p)
    alias = data.get("business_alias", {})
    if name not in alias:
        return False, f"未找到别名「{name}」"
    del alias[name]
    save_raw(data, p)
    return True, f"别名「{name}」已删除"
