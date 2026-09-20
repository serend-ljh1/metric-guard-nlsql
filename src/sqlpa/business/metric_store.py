"""
sqlpa.business.metric_store
===========================
指标中心的持久化层：对 business_config.yaml 做带校验的增删改（指标/维度/别名）。

这是「指标管理界面」的后端——语义层不再是只读配置文件，而是可运营、可维护的
指标中心；所有修改仍写回同一份权威配置，保证"公式只来自配置"的原则不变。
"""
from __future__ import annotations

import json
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
                  path: str | Path | None = None,
                  actor: str = "") -> Tuple[bool, str]:
    """新增或更新一条指标，并把这次变更写进**口径版本历史**（append-only、可回滚）。"""
    p = Path(path or _DEFAULT)
    data = load_raw(p)
    errs = validate_metric(data, metric, editing_key)
    if errs:
        return False, "；".join(errs)
    m = {
        "key": metric["key"].strip(),
        "name": metric["name"].strip(),
        "desc": (metric.get("desc") or "").strip(),
        "owner": (metric.get("owner") or "未指定").strip() or "未指定",
        "version": (metric.get("version") or "v1").strip() or "v1",
        "metric_expr": metric["metric_expr"].strip(),
        "from_clause": metric["from_clause"].strip(),
        # join_clause 必须原样保留：它承载"指标怎么连表"的口径。此前重建 dict 时漏了这个
        # 字段，于是在指标中心保存一次，gmv 就丢掉 `JOIN order_items oi`，SUM(oi.price)
        # 永久变成 `no such column: oi.price`——一次编辑毁掉一个指标。
        "join_clause": (metric.get("join_clause") or "").strip(),
        "where_core": (metric.get("where_core") or "1=1").strip() or "1=1",
        "support_dims": list(metric.get("support_dims", [])),
        "support_filters": list(metric.get("support_filters", [])),
    }
    # 未识别的自定义字段原样带过去，避免"保存一次就静默丢字段"这类数据损坏。
    for k, v in metric.items():
        if k not in m and k != "editing_key":
            m[k] = v
    metrics = data.setdefault("metrics", [])
    before = next((x for x in metrics if x.get("key") == editing_key), None)
    if editing_key:
        metrics[:] = [x for x in metrics if x.get("key") != editing_key]
    metrics.append(m)
    save_raw(data, p)
    # ---- 口径版本：只有内容真的变了（或新建）才记一条，避免无意义噪声 ----
    if before is None or metric_hash(before) != metric_hash(m):
        try:
            from sqlpa.business import storage
            storage.insert_metric_version(m["key"], "update" if before else "create",
                                          before, m, metric_hash(m), actor)
        except Exception:  # noqa: BLE001
            pass  # 版本记录失败不阻断保存（但会在 metric_history 里表现为缺失）
    return True, f"指标「{m['name']}」已保存"


def metric_hash(metric: Dict) -> str:
    """口径内容指纹：用于判断"这次保存是否真的改了口径"。"""
    import hashlib
    import json as _json
    canonical = _json.dumps({k: metric.get(k) for k in sorted(metric)
                             if k != "editing_key"}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def metric_history(metric_key: str, limit: int = 20) -> List[Dict]:
    """某指标的口径变更历史（倒序），含 before/after 定义（无则为 None）。"""
    from sqlpa.business import storage
    rows = storage.list_metric_versions(metric_key, limit=limit)
    for r in rows:
        for f, key in (("before_json", "before"), ("after_json", "after")):
            r[key] = None
            if r.get(f):
                try:
                    r[key] = json.loads(r[f])
                except Exception:  # noqa: BLE001
                    pass
    return rows


def rollback_metric(metric_key: str, version_seq: int,
                    path: str | Path | None = None,
                    actor: str = "") -> Tuple[bool, str]:
    """**撤销**某次口径变更：恢复到该版本的 `before` 定义。

    - 该版本是 update → 把指标恢复成改动前的定义；
    - 该版本是 create → 撤销即删除该指标；
    - 该版本是 rollback → 撤销这次回滚（恢复回滚前的定义）。
    无论哪种，都会再记一条 `rollback` 版本，保证"回滚也可追溯"。
    """
    from sqlpa.business import storage
    row = storage.get_metric_version(metric_key, version_seq)
    if not row:
        return False, f"未找到 {metric_key} 的版本 {version_seq}"
    who = actor or f"rollback<-v{version_seq}"
    before_json = row.get("before_json")
    if not before_json:
        ok, msg = delete_metric(metric_key, path=path, actor=who)
        if not ok:
            return False, msg
        try:
            storage.insert_metric_version(metric_key, "rollback", None, None, "", who)
        except Exception:  # noqa: BLE001
            pass
        return True, f"指标「{metric_key}」由版本 {version_seq} 创建，已撤销（删除）"
    try:
        target = json.loads(before_json)
    except Exception as e:  # noqa: BLE001
        return False, f"版本 {version_seq} 的定义无法解析: {e}"
    ok, msg = upsert_metric(target, editing_key=metric_key, path=path, actor=who)
    if not ok:
        return False, msg
    try:
        storage.insert_metric_version(metric_key, "rollback", None, target,
                                      metric_hash(target), who)
    except Exception:  # noqa: BLE001
        pass
    return True, f"指标「{metric_key}」已回滚到版本 {version_seq} 之前的口径"


def metric_lineage(cfg, metric_key: str) -> Dict:
    """指标的**表/列级血缘**：读了哪些表、哪些列、支持哪些维度。

    诚实边界：这是从定义（from_clause / join_clause / 表达式）**静态解析**出的清单，
    不是数据库字段级血缘，也不含上游加工链路与字段级影响分析。
    """
    import re
    m = getattr(cfg, "metrics", {}).get(metric_key)
    if not m:
        return {}
    text = f"{m.from_clause or ''} {m.join_clause or ''}"
    tables = list(dict.fromkeys(t.lower() for t in re.findall(
        r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", text, re.I)))
    stop = {"on", "where", "left", "join", "group", "order", "inner", "outer"}
    alias_map = {a.lower(): t.lower() for t, a in re.findall(
        r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)\s+(?:AS\s+)?([a-zA-Z_][\w]*)", text, re.I)
        if a.lower() not in stop}
    columns = [f"{alias_map.get(a.lower(), a.lower())}.{c}"
               for a, c in re.findall(r"\b([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)", m.metric_expr)]
    return {
        "metric": metric_key, "name": m.name, "owner": m.owner, "version": m.version,
        "tables": tables,
        "columns": sorted(dict.fromkeys(columns)),
        "support_dims": list(m.support_dims),
        "weight_metric": getattr(m, "weight_metric", ""),
        "note": "表/列级静态血缘（来自定义解析），非数据库字段级血缘",
    }


def delete_metric(key: str, path: str | Path | None = None,
                  actor: str = "") -> Tuple[bool, str]:
    p = Path(path or _DEFAULT)
    data = load_raw(p)
    before = next((m for m in data.get("metrics", []) if m.get("key") == key), None)
    n0 = len(data.get("metrics", []))
    data["metrics"] = [m for m in data.get("metrics", []) if m.get("key") != key]
    if len(data["metrics"]) == n0:
        return False, f"未找到指标「{key}」"
    save_raw(data, p)
    if before is not None:
        try:
            from sqlpa.business import storage
            storage.insert_metric_version(key, "delete", before, None,
                                          metric_hash(before), actor)
        except Exception:  # noqa: BLE001
            pass
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
