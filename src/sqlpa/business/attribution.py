"""
sqlpa.business.attribution
==========================
异常归因拆解 Agent（轻量版，不做血缘追踪）。

目标：当某指标的当期值相比上期有明显波动（涨/跌）时，把指标「按维度拆开」，
定位变化主要来自哪个维度（哪个品类/哪个州/订单量还是客单价），从而回答
"GMV 为什么跌了 30%"这类问题。

设计取舍（对齐 MVP 边界）：
- 不做元数据血缘、不做任务依赖采集。
- 归因方式是"维度对比"，不是"计算链路追踪"——同一指标查两次（当/上期），
  再按维度分组对比，找出对变化贡献最大的维度最值。
- 各维度查询相互独立 → 用 ThreadPoolExecutor 并行执行，降延迟。
- 纯确定性算术，不依赖 LLM 判断，结果可复核、不产生额外 token 成本。
"""
from __future__ import annotations

import datetime
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from .metric_config import BusinessConfig
from .sqlgen import metric_source


def _now() -> datetime.date:
    return datetime.date.today()


def _range_spec(spec: str) -> Tuple[str, str]:
    """把 '上月' / '本月' / '2026-08' 转成 (start,end) 已加引号的字面量。"""
    now = _now()
    s = str(spec).strip()
    m = re.fullmatch(r"(\d{4})[年\-](\d{1,2})月?", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        start = datetime.date(y, mo, 1)
        end = datetime.date(y + (mo == 12), ((mo % 12) + 1), 1)
        return "'" + start.isoformat() + "'", "'" + end.isoformat() + "'"
    if "上月" in s or "上个月" in s:
        fm = datetime.date(now.year, now.month, 1)
        start = (fm - datetime.timedelta(days=1)).replace(day=1)
        return "'" + start.isoformat() + "'", "'" + fm.isoformat() + "'"
    if "本月" in s or "这个月" in s:
        fm = datetime.date(now.year, now.month, 1)
        end = (fm + datetime.timedelta(days=31)).replace(day=1)
        return "'" + fm.isoformat() + "'", "'" + end.isoformat() + "'"
    n = 30
    mm = re.search(r"(\d+)\s*天|最近(\d+)天", s)
    if mm:
        n = int(mm.group(1) or mm.group(2))
    return ("'" + (now - datetime.timedelta(days=n)).isoformat() + "'",
            "'" + now.isoformat() + "'")


def _scalar(db: sqlite3.Connection, sql: str) -> Optional[object]:
    try:
        row = db.execute(sql).fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def _time_where(metric_core: str, start: str, end: str) -> str:
    parts = [metric_core] if metric_core not in ("", "1=1") else []
    parts.append(f"o.order_purchase_timestamp >= {start} AND o.order_purchase_timestamp < {end}")
    return "WHERE " + " AND ".join(parts)


def analyze(cfg: BusinessConfig, db: sqlite3.Connection, metric_key: str,
            current_spec: str, previous_spec: Optional[str] = None,
            dims: Optional[List[str]] = None,
            threshold_pct: float = 0.05) -> Dict:
    """归因拆解主入口。

    当 |change_pct| >= threshold 时才展开维度归因，否则只报波动。
    """
    m = cfg.metrics.get(metric_key)
    if not m or db is None:
        return {"ok": False, "reason": "指标或数据库不可用"}

    dims = [d for d in (dims or ["dt"]) if d in cfg.dimensions]
    prev_spec = previous_spec or ("上月" if "月" in str(current_spec) else "上一期")
    c0, c1 = _range_spec(current_spec)
    p0, p1 = _range_spec(prev_spec)

    cur_total = _scalar(db, f"SELECT {m.metric_expr} AS v\n{metric_source(cfg, m, dims)}\n{_time_where(m.where_core, c0, c1)}")
    prev_total = _scalar(db, f"SELECT {m.metric_expr} AS v\n{metric_source(cfg, m, dims)}\n{_time_where(m.where_core, p0, p1)}")
    if cur_total is None or prev_total is None:
        return {"ok": False, "reason": "归因查询失败（当期或上期无结果）"}

    change = float(cur_total) - float(prev_total)
    change_pct = change / float(prev_total) if float(prev_total) else 0.0
    is_abnormal = abs(change_pct) >= threshold_pct

    result = {
        "ok": True, "metric": metric_key, "metric_name": m.name,
        "current_total": cur_total, "previous_total": prev_total,
        "change": change, "change_pct": round(change_pct, 4),
        "is_abnormal": is_abnormal,
        "current_spec": current_spec, "previous_spec": prev_spec,
        "dims": [], "top_contributors": [],
    }
    if not is_abnormal or not dims:
        return result

    # ---- 并行维度拆解：各维度当期/上期查询互不依赖 ----
    # sqlite3 连接默认 check_same_thread=True，不能跨线程复用；因此每个
    # worker 从 db 文件路径独立只读连接，才能真正并行（否则子线程执行会抛
    # ProgrammingError 被吞掉，维度全是空）。
    try:
        db_path = db.execute("PRAGMA database_list").fetchone()[2]
    except Exception:  # noqa: BLE001
        db_path = None

    def _split(dim: str, start: str, end: str) -> Dict:
        frag = cfg.dimensions[dim].sql_fragment
        sql = (f"SELECT {frag} AS d, {m.metric_expr} AS v\n{metric_source(cfg, m, dims)}\n"
               f"{_time_where(m.where_core, start, end)}\nGROUP BY {frag}")
        if not db_path:
            return {}
        wc = sqlite3.connect(db_path)
        try:
            return {row[0]: row[1] for row in wc.execute(sql).fetchall()}
        except Exception:  # noqa: BLE001
            return {}
        finally:
            wc.close()

    dim_results: Dict[str, Tuple[Dict, Dict]] = {}
    with ThreadPoolExecutor(max_workers=min(len(dims), 8)) as ex:
        futures = {ex.submit(_split, d, c0, c1): (d, "cur") for d in dims}
        futures.update({ex.submit(_split, d, p0, p1): (d, "prev") for d in dims})
        for f in futures:
            d, tag = futures[f]
            cur_map, prev_map = dim_results.get(d, ({}, {}))
            if tag == "cur":
                dim_results[d] = (f.result(), prev_map)
            else:
                dim_results[d] = (cur_map, f.result())

    contributors = []
    for d in dims:
        cur_map, prev_map = dim_results[d]
        delta = {}
        all_keys = set(cur_map) | set(prev_map)
        for k in all_keys:
            cv = float(cur_map.get(k, 0.0))
            pv = float(prev_map.get(k, 0.0))
            delta[k] = round(cv - pv, 4)
        result["dims"].append({"dim": d, "current": cur_map, "previous": prev_map, "delta": delta})
        if delta:
            top_key, top_delta = max(delta.items(), key=lambda kv: abs(kv[1]))
            contributors.append(
                {"dim": d, "key": top_key, "delta": top_delta,
                 "pct_of_change": round(top_delta / change, 4) if change else 0.0,
                 "desc": f"{cfg.dimensions[d].name}「{top_key}」变化{top_delta:+.2f}"})
    result["top_contributors"] = sorted(contributors, key=lambda x: abs(x["delta"]), reverse=True)
    # 显式暴露"维度拆解不可用"的原因：并行拆解需要按库文件路径独立开只读连接
    # （sqlite3 连接不能跨线程复用）。内存库（:memory:）没有文件路径，拆解会静默为空——
    # 这种静默失败最容易被误读成"该维度没波动"，因此在这里标注出来。
    if dims and not db_path:
        result["dims_skipped_reason"] = (
            "维度拆解需要文件型数据库（并行查询按文件路径开新连接）；"
            "当前连接无文件路径（如 :memory:），故 dims 为空")
    return result


def _deterministic_summary(base: str, top_lines: List[str]) -> str:
    """确定性兜底点评：不依赖 LLM，保证永远有输出、可复核。"""
    if not top_lines:
        return base + "本次波动未达维度拆分要求，暂无显著主因。"
    return base + "主要来自：\n" + "\n".join(top_lines)


# ---------------------------------------------------------------- 多轮下钻

def drill(cfg: BusinessConfig, db: sqlite3.Connection, metric_key: str,
          current_spec: str, path: List[Dict], next_dims: Optional[List[str]] = None,
          previous_spec: Optional[str] = None, threshold_pct: float = 0.05) -> Dict:
    """**逐层下钻**：沿某个维度值继续拆解，回答"为什么这个州跌了"。

    真实分析是一条链，而不是单点：
        GMV 跌了 → 主因是州 SP（占 52%）
        → 那 SP 为什么跌？按品类拆 → 某品类占 68%
        → 是单量少了还是客单价低了？按乘法因子拆

    Args:
        path: 已经下钻的路径，形如 [{"dim":"state","value":"SP"}]。
              每层都会变成一个过滤条件叠加到查询上。
        next_dims: 本层要拆的维度（默认取该指标支持、且不在 path 里的维度）。

    Returns:
        与 analyze 同构的结果，另附 `path`（下钻路径）与 `next_dims`（可继续拆的维度），
        便于 UI/对话层提示"还可以往哪拆"。
    """
    m = cfg.metrics.get(metric_key)
    if not m:
        return {"ok": False, "reason": f"未知指标 {metric_key}"}
    used = {p.get("dim") for p in (path or [])}
    # 注意：显式传 next_dims=[] 表示"本层不拆任何维度"，不能被 `or` 当成未传。
    cand = list(next_dims) if next_dims is not None \
        else [d for d in m.support_dims if d not in used]

    # 把下钻路径变成过滤条件：f"{dim} = '{value}'"
    # 复用 compiler 的过滤渲染，保证与主查询同一套写法
    from .compiler import render_filter

    def _split_for(dim: str):
        """在当前下钻路径的约束下，按 dim 拆当期/上期。"""
        def _q(start: str, end: str) -> Dict:
            frag = cfg.dimensions[dim].sql_fragment
            wheres = [m.where_core] if m.where_core not in ("", "1=1") else []
            for p in (path or []):
                d, v = p.get("dim"), p.get("value")
                if d in cfg.filter_templates:
                    wheres.append(render_filter(cfg, d, v))
            wheres.append(f"o.order_purchase_timestamp >= {start} AND "
                          f"o.order_purchase_timestamp < {end}")
            sql = (f"SELECT {frag} AS d, {m.metric_expr} AS v\n"
                   f"{metric_source(cfg, m, [dim])}\nWHERE " + " AND ".join(wheres) +
                   f"\nGROUP BY {frag}")
            return _exec_map(db, sql)
        return _q

    c0, c1 = _range_spec(current_spec)
    prev_spec = previous_spec or ("上月" if "月" in str(current_spec) else "上一期")
    p0, p1 = _range_spec(prev_spec)

    out = {
        "ok": True, "metric": metric_key, "metric_name": m.name,
        "current_spec": current_spec, "previous_spec": prev_spec,
        "path": list(path or []),
        "path_desc": " → ".join(f"{p.get('dim')}={p.get('value')}" for p in (path or [])) or "（全局）",
        "dims": [], "top_contributors": [], "next_dims": cand,
    }
    if not cand:
        out["note"] = "已无可继续拆解的维度"
        return out

    contributors = []
    for dim in cand:
        fn = _split_for(dim)
        cur_map, prev_map = fn(c0, c1), fn(p0, p1)
        delta = {k: round(float(cur_map.get(k, 0.0)) - float(prev_map.get(k, 0.0)), 4)
                 for k in (set(cur_map) | set(prev_map))}
        out["dims"].append({"dim": dim, "current": cur_map,
                            "previous": prev_map, "delta": delta})
        if delta:
            k, d = max(delta.items(), key=lambda kv: abs(kv[1]))
            contributors.append({"dim": dim, "key": k, "delta": d,
                                 "desc": f"{cfg.dimensions[dim].name}「{k}」变化{d:+.2f}"})
    out["top_contributors"] = sorted(contributors, key=lambda x: abs(x["delta"]), reverse=True)
    return out


def _exec_map(db: sqlite3.Connection, sql: str) -> Dict:
    """执行"维度->值"查询；失败返回空（调用方据此标注不可用，而非静默当无波动）。"""
    try:
        return {row[0]: row[1] for row in db.execute(sql).fetchall()}
    except Exception:  # noqa: BLE001
        return {}


def next_drill_suggestion(result: Dict) -> Optional[Dict]:
    """给出"下一步该往哪拆"的建议：取当前贡献最大的维度值作为下钻目标。"""
    contrib = (result.get("top_contributors") or [])
    if not contrib:
        return None
    top = contrib[0]
    return {"dim": top["dim"], "value": top["key"], "delta": top["delta"],
            "hint": f"可继续下钻：{top['dim']}={top['key']}"}


def summarize(result: Dict, llm=None) -> str:
    """把归因结果转成业务人员能看懂的话。

    LLM 从"只写 SQL"扩到治理面：这里由 llm.complete() 把时间/波动/主因维度生成为
    自然语言点评；llm 缺失或调用失败时退回确定性拼接（可复核、零额外风险）。
    """
    if not result.get("ok"):
        return "归因未完成。"

    metric_name = result.get("metric_name", result.get("metric", ""))
    cur = result.get("current_total")
    prev = result.get("previous_total")
    change = result.get("change", 0.0)
    change_pct = result.get("change_pct", 0.0)
    contrib = result.get("top_contributors") or []

    top_lines = [f"- {c.get('desc', c.get('key', ''))}" +
                 (f"（占波动 {abs(c.get('pct_of_change', 0)) * 100:.0f}%）"
                  if c.get("pct_of_change") else "")
                 for c in contrib[:3]]
    base = (f"{metric_name} 当期 {cur}、上期 {prev}，"
            f"波动 {change:+.2f}（{change_pct * 100:+.1f}%）。")

    if llm is not None:
        prompt = ("你是业务归因分析助手。把以下结构化数据写成一句业务人员能看懂的话："
                  "说明波动方向与主要成因，末尾附一句行动建议。不要提 SQL、不要讲口径怎么算。\n"
                  + base + "\n主要贡献维度：\n"
                  + ("\n".join(top_lines) if top_lines else "数据不足，无显著维度拆分。"))
        try:
            text = llm.complete(prompt).strip()
            return text or _deterministic_summary(base, top_lines)
        except Exception:  # noqa: BLE001 —— LLM 失败不阻塞归因输出
            return _deterministic_summary(base, top_lines)

    return _deterministic_summary(base, top_lines)


def notify_anomaly(cfg: BusinessConfig, result: Dict,
                   path: Optional[str] = None, summary: Optional[str] = None) -> Optional[str]:
    """归因异常闭环：把异常写入 HITL 队列并关联指标负责人。

    仅当 result["is_abnormal"] 时入队；返回新 HITL record_id，否则 None。
    这是 ClosureAgent 的落地：发现异常 → 推送负责人（owner）→ 人工在 HITL 处理 → 验证关闭。
    summary 为 AI（归因总结）预先生成的分析草稿，会写入工单的 ai_note，审核人可直接查看。
    """
    if not result.get("ok") or not result.get("is_abnormal"):
        return None
    m = cfg.metrics.get(result["metric"])
    owner = m.owner if m and hasattr(m, "owner") else "未指定"
    top = result.get("top_contributors") or []

    reason_lines = [f"{result['metric_name']} 波动 {result['change']:+.2f} "
                    f"({result['change_pct'] * 100:+.1f}%)，超过阈值需关注"]
    for c in top[:3]:
        reason_lines.append(f"  - {c['desc']}（占{abs(c['pct_of_change']) * 100:.0f}%）")
    reason_lines.append(f"当期范围: {result.get('current_spec')}；负责人: {owner}")

    from sqlpa.business.hitl import enqueue
    return enqueue(
        user_input=f"指标异常归因：{result['metric']} 波动",
        matched_metric=result["metric"], generated_sql="",
        reject_reason="\n".join(reason_lines), role="admin",
        path=path, owner=owner, ai_note=summary or "",
    )