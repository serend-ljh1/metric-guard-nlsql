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
import json
import math
import re
import sqlite3
import statistics
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence, Tuple

from .metric_config import BusinessConfig
from .sqlgen import metric_source


def _now() -> datetime.date:
    return datetime.date.today()


def _default_previous_spec(current_spec: str) -> str:
    """取「上一期」的默认写法。

    关键：禁止把显式月份（如 "2018-06"）的上期默认成相对今天的 "上月" —— 那会解析成
    当前真实月（如 2026），在真实数据窗口（Olist 2016-09~2018-10）内必然查不到上期，
    归因会误报"当期或上期无结果"。对显式月份，上期应取**该月的前一个月**（字面量），
    时间语义始终落在同一数据窗口内。
    """
    s = str(current_spec or "")
    m = re.fullmatch(r"(\d{4})[年\-](\d{1,2})月?", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        prev = datetime.date(y, mo, 1) - datetime.timedelta(days=1)  # 退一个月
        return f"{prev.year:04d}-{prev.month:02d}"
    if "上月" in s or "上个月" in s:
        return "上月"
    if "本月" in s or "这个月" in s:
        return "上月"
    return "上一期"


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


def _calendar_note(cur: Tuple[str, str], prev: Tuple[str, str],
                   change: float, change_pct: float) -> Optional[Dict]:
    """当月天数差异提示（不静默改判，只把"这段波动的多少来自天数"摆出来）。

    为什么必须有：单期环比在月度上天然受**月份长度**影响。README 曾引以为据的
    "2018-03 vs 2018-02 GMV +17.1% 超阈值"就是 2 月 28 天 vs 3 月 31 天造成的假象。
    这里给出按日均值归一后的波动与"天数可解释比例"，让使用者自己决定要不要告警，
    而不是由代码偷偷改阈值口径（那样会引入新的口径不透明）。
    """
    try:
        cs = datetime.date.fromisoformat(str(cur[0]).strip("'"))
        ce = datetime.date.fromisoformat(str(cur[1]).strip("'"))
        ps = datetime.date.fromisoformat(str(prev[0]).strip("'"))
        pe = datetime.date.fromisoformat(str(prev[1]).strip("'"))
    except Exception:  # noqa: BLE001
        return None
    cur_days = (ce - cs).days
    prev_days = (pe - ps).days
    if cur_days <= 0 or prev_days <= 0:
        return None
    # 日均口径下的波动（天数差异被消掉）
    cur_total = 1.0 + change_pct
    per_day_ratio = (cur_total / cur_days) / (1.0 / prev_days)
    per_day_pct = per_day_ratio - 1.0
    explained = 0.0
    if abs(change_pct) > 1e-9:
        explained = (change_pct - per_day_pct) / change_pct
    out = {
        "current_days": cur_days, "previous_days": prev_days,
        "per_day_change_pct": round(per_day_pct, 4),
        "explained_by_calendar": round(explained, 3),
    }
    if cur_days == prev_days:
        return out
    if explained >= 0.5:
        out["note"] = (
            f"当期 {cur_days} 天 vs 上期 {prev_days} 天：总波动 {change_pct * 100:+.1f}%，"
            f"按日均只有 {per_day_pct * 100:+.1f}% —— 该波动的大部分可由月份天数差异解释，"
            f"建议按日均或同比复核后再决定是否告警")
    elif explained <= -0.5:
        out["note"] = (
            f"当期 {cur_days} 天 vs 上期 {prev_days} 天：总波动 {change_pct * 100:+.1f}%，"
            f"但按日均是 {per_day_pct * 100:+.1f}% —— 天数差异**掩盖**了更大的日均波动，"
            f"不要因为总波动看起来小而忽略")
    return out


def _segment_values(cfg, db_path: str, metric, dim: str, start: str, end: str) -> Dict:
    """按某维度取"指标按分段的取值"（每个分段一条；供权重指标复用）。"""
    frag = cfg.dimensions[dim].sql_fragment
    sql = (f"SELECT {frag} AS d, {metric.metric_expr} AS v\n"
           f"{metric_source(cfg, metric, [dim])}\n"
           f"{_time_where(metric.where_core, start, end)}\nGROUP BY {frag}")
    conn = sqlite3.connect(db_path)
    try:
        return {row[0]: row[1] for row in conn.execute(sql).fetchall()}
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()


def _ratio_decomposition(cfg, db_path: Optional[str], m, dims: List[str],
                         cur: Tuple[str, str], prev: Tuple[str, str],
                         prev_total, cur_total) -> Optional[Dict]:
    """比率类指标的 **rate/mix 量价分解**（先验证再给结论）。

    比率 R = Σ(w_i·r_i)/Σ(w_i)：变化可拆成
      - rate effect：各分段自身比率的变化（权重取上期结构）
      - mix  effect：分段结构（权重占比）的变化（比率取上期值）
      - interaction：其余交叉项
    为什么必须**自校验**：权重指标一旦与该比率的分母不一致（where 口径不同、粒度不同），
    重建出的总值就对不上真实值；此时任何分解都是精致的错误，直接拒绝而不是给出去。
    """
    if not db_path or not getattr(m, "weight_metric", ""):
        return None
    wm = cfg.metrics.get(m.weight_metric)
    if wm is None:
        return None
    out: Dict = {}
    for dim in dims:
        r0 = _segment_values(cfg, db_path, m, dim, *prev)
        r1 = _segment_values(cfg, db_path, m, dim, *cur)
        w0 = _segment_values(cfg, db_path, wm, dim, *prev)
        w1 = _segment_values(cfg, db_path, wm, dim, *cur)
        if not r0 or not r1 or not w0 or not w1:
            out[dim] = {"valid": False, "reason": "分段数据不足，无法分解"}
            continue
        W0, W1 = sum(v or 0 for v in w0.values()), sum(v or 0 for v in w1.values())
        if not W0 or not W1:
            out[dim] = {"valid": False, "reason": "权重合计为 0，无法分解"}
            continue
        keys = set(r0) | set(r1) | set(w0) | set(w1)
        rec0 = sum((w0.get(k) or 0) * (r0.get(k) or 0) for k in keys) / W0
        rec1 = sum((w1.get(k) or 0) * (r1.get(k) or 0) for k in keys) / W1
        tol = max(1e-6, abs(float(prev_total or 0)) * 1e-3, abs(float(cur_total or 0)) * 1e-3)
        if abs(rec0 - float(prev_total or 0)) > tol or abs(rec1 - float(cur_total or 0)) > tol:
            out[dim] = {"valid": False, "weight_metric": wm.key,
                        "reason": f"权重指标「{wm.name}」与该比率的分母不一致"
                                  f"（重建 {rec0:.4f}/{rec1:.4f} vs 实际 "
                                  f"{prev_total}/{cur_total}），拒绝给出分解",
                        "reconstructed": [round(rec0, 4), round(rec1, 4)]}
            continue
        rate = sum((w0.get(k) or 0) / W0 * ((r1.get(k) or 0) - (r0.get(k) or 0)) for k in keys)
        mix = sum(((w1.get(k) or 0) / W1 - (w0.get(k) or 0) / W0) * (r0.get(k) or 0)
                  for k in keys)
        change = float(cur_total or 0) - float(prev_total or 0)
        out[dim] = {"valid": True, "weight_metric": wm.key,
                    "change": round(change, 4), "rate_effect": round(rate, 4),
                    "mix_effect": round(mix, 4),
                    "interaction": round(change - rate - mix, 4),
                    "denominator": [round(W0, 4), round(W1, 4)]}
    return out or None


def _time_where(metric_core: str, start: str, end: str) -> str:
    parts = [metric_core] if metric_core not in ("", "1=1") else []
    parts.append(f"o.order_purchase_timestamp >= {start} AND o.order_purchase_timestamp < {end}")
    return "WHERE " + " AND ".join(parts)


# ---------------------------------------------------------------- 显著性

def daily_series(cfg, db_path: str, m, start: str, end: str,
                 dims: Sequence[str] = ()) -> List[float]:
    """取指标的**日粒度序列**（只对可加指标有意义：日值之和 ≈ 期间总值）。"""
    sql = (f"SELECT DATE(o.order_purchase_timestamp) AS d, {m.metric_expr} AS v\n"
           f"{metric_source(cfg, m, list(dims))}\n"
           f"{_time_where(m.where_core, start, end)}\n"
           f"GROUP BY DATE(o.order_purchase_timestamp)")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(sql).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()
    return [float(v) for _d, v in rows if v is not None]


def significance_test(current: Sequence[float], previous: Sequence[float],
                      alpha: float = 0.05) -> Dict:
    """日粒度均值差异的 **Welch z 检验**（近似，零依赖、零 token）。

    为什么需要一个统计护栏：单期环比只看 |change_pct| >= 阈值，而日间波动本身很大时，
    几个百分点的差异完全可能是噪声（尤其是"月度 31 天 vs 28 天"这种口径差）。
    这里用日序列的均值与方差做 Welch 检验（不假设等方差），p < alpha 才算"真实波动"。

    局限（必须一起说）：假设日之间独立、且分布近似正态；对强趋势/强周期数据是**保守估计的近似**，
    因此它只用来**抑制告警**（no false alarm），不用来"证明"波动存在。
    """
    if len(current) < 3 or len(previous) < 3:
        return {"tested": False, "reason": "有效天数不足（<3）"}
    m1, m0 = statistics.fmean(current), statistics.fmean(previous)
    v1, v0 = statistics.variance(current), statistics.variance(previous)
    se = math.sqrt(v1 / len(current) + v0 / len(previous))
    if se <= 0:
        return {"tested": False, "reason": "日序列无方差（数据退化）"}
    z = (m1 - m0) / se
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return {"tested": True, "alpha": alpha, "z": round(z, 3), "p_value": round(p, 4),
            "is_significant": bool(p < alpha),
            "days": {"current": len(current), "previous": len(previous)},
            "daily_mean": {"current": round(m1, 4), "previous": round(m0, 4)},
            "note": "Welch z 检验（日粒度均值差异）；近似检验，仅用于抑制告警噪声"}


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
    prev_spec = previous_spec or _default_previous_spec(current_spec)
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
    cal = _calendar_note((c0, c1), (p0, p1), change, change_pct)
    if cal:
        result["calendar"] = cal
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

    # ---- 守恒自检：分段之和对不上总变化就**该维度**不许说"占波动 X%" ----
    # 比率/均值类指标天然对不上（数学不成立）；可加指标也可能是某个维度没覆盖全部行
    # （NULL 分段、新出现的分段、一对多重复计数）。两种情况下把占比写进结论/报告/告警都是错的，
    # 因此按**维度**降级：坏维度只影响它自己，好维度（如按州）照常给占比。
    checks = {}
    for row in result["dims"]:
        chk = _additivity_check(change, row["delta"])
        row.update({"delta_sum": chk["delta_sum"], "uncovered": chk["uncovered"],
                    "additive": chk["additive"]})
        checks[row["dim"]] = chk
    for c in result["top_contributors"]:
        chk = checks.get(c.get("dim"), {"additive": True})
        if chk["additive"]:
            c["pct_reliable"] = True
            continue
        c["pct_of_change"] = None
        c["pct_reliable"] = False
        c["pct_note"] = "该维度分段之和不等于总变化，占比不适用"
    unreliable = [d for d, chk in checks.items() if not chk["additive"]]
    result["contribution_reliable"] = not unreliable
    result["additivity_checks"] = checks

    # 比率/均值类指标：分段 delta 不成占比，但可以给**量价/结构分解**（先重建校验再给结论）
    dec = _ratio_decomposition(cfg, db_path, m, dims, (c0, c1), (p0, p1),
                               prev_total, cur_total) if unreliable else None
    if dec:
        result["ratio_decomposition"] = dec

    if unreliable:
        dim_names = "、".join(cfg.dimensions[d].name if d in cfg.dimensions else d
                            for d in unreliable)
        has_dec = any(v.get("valid") for v in (dec or {}).values())
        tail = ("；已改用量价/结构分解（见 ratio_decomposition）" if has_dec
                else "；该维度未声明可用的权重指标，故只给分段对照")
        result["contribution_note"] = (
            f"「{m.name}」是比率/均值类指标，按「{dim_names}」的分段变化之和天然不等于总变化，"
            f"故该维度的贡献只作分段对照、不给出「占波动」" + tail if not is_additive(m) else
            f"维度「{dim_names}」未覆盖全部行（或存在一对多重复计数），分段之和不等于总变化，"
            f"该维度的贡献占比降级为参照")
    # 显式暴露"维度拆解不可用"的原因：并行拆解需要按库文件路径独立开只读连接
    # （sqlite3 连接不能跨线程复用）。内存库（:memory:）没有文件路径，拆解会静默为空——
    # 这种静默失败最容易被误读成"该维度没波动"，因此在这里标注出来。
    if dims and not db_path:
        result["dims_skipped_reason"] = (
            "维度拆解需要文件型数据库（并行查询按文件路径开新连接）；"
            "当前连接无文件路径（如 :memory:），故 dims 为空")

    # ---- 统计护栏（放在最后：此处 db_path 与时间窗都已就绪）----
    # 只在"可加指标 + 判定异常"时计算，且只用于**抑制**噪声告警，不用来"证明"波动存在。
    if is_abnormal and is_additive(m) and db_path:
        try:
            sig = significance_test(daily_series(cfg, db_path, m, c0, c1),
                                    daily_series(cfg, db_path, m, p0, p1))
            if sig.get("tested"):
                result["significance"] = sig
        except Exception:  # noqa: BLE001
            pass
    return result


def _deterministic_summary(base: str, top_lines: List[str]) -> str:
    """确定性兜底点评：不依赖 LLM，保证永远有输出、可复核。"""
    if not top_lines:
        return base + "本次波动未达维度拆分要求，暂无显著主因。"
    return base + "主要来自：\n" + "\n".join(top_lines)


# ---------------------------------------------------------------- 可加性

def is_additive(metric) -> bool:
    """该指标在维度上是否**可加**（分段变化之和 == 总变化）。

    为什么必须区分：维度贡献 = 「各分段当期值 − 上期值」，这对 SUM 类成立；
    对比率/均值类**在数学上不成立**。反例（AOV 按州）：
        上期 SP 2 单×50=100、RJ 1 单×50=50 → AOV 50
        当期 SP 3 单×50=110、RJ 1 单×40=40 → AOV 37.5
    分段 delta = +10 / −10（合计 0），而真实变化是 −12.5 —— 报"主因 SP 占波动 −80%"
    是纯粹的错。所以比率/均值类指标不做"占比"表述，只做分段对照。

    判定顺序：配置显式声明 additive → 否则按表达式启发式（含 "/" 或 AVG( 视为不可加）。
    注意这只是**先验**，且刻意不把 COUNT(DISTINCT ...) 一律判为不可加——按"州"这类**划分型维度**
    时它是可加的，是否真的可加由数据侧的守恒自检（_additivity_check）决定。
    """
    explicit = getattr(metric, "additive", None)
    if explicit is not None:
        return bool(explicit)
    expr = (getattr(metric, "metric_expr", "") or "").upper()
    if "/" in expr or "AVG(" in expr:
        return False
    return True


def _additivity_check(change: float, delta: Dict) -> Dict:
    """数据侧守恒自检：分段变化之和是否等于总变化（确定性、零 token）。"""
    s = sum(float(v) for v in delta.values())
    uncovered = change - s
    tol = max(1e-6, abs(change) * 1e-6)
    return {"delta_sum": round(s, 4), "uncovered": round(uncovered, 4),
            "additive": abs(uncovered) <= tol}


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
            for seg in (path or []):
                d, v = seg.get("dim"), seg.get("value")
                if d in cfg.filter_templates:
                    wheres.append(render_filter(cfg, d, v))
            wheres.append(f"o.order_purchase_timestamp >= {start} AND "
                          f"o.order_purchase_timestamp < {end}")
            # 路径过滤条件可能引用需要 JOIN 的表（如按 category 过滤时 WHERE 里有 p.），
            # 因此 JOIN 注入必须同时考虑"当前拆分维度 + 路径里被过滤的维度"，
            # 否则会生成引用了未注入别名（如 p.）的 SQL，被 except 吞掉后错当"没波动"。
            join_dims = list(dict.fromkeys(
                [dim] + [seg.get("dim") for seg in (path or [])
                         if seg.get("dim") in cfg.dimensions]))
            sql = (f"SELECT {frag} AS d, {m.metric_expr} AS v\n"
                   f"{metric_source(cfg, m, join_dims)}\nWHERE " + " AND ".join(wheres) +
                   f"\nGROUP BY {frag}")
            return _exec_map(db, sql)
        return _q

    c0, c1 = _range_spec(current_spec)
    prev_spec = previous_spec or _default_previous_spec(current_spec)
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
    return {"dim": top.get("dim", ""), "value": top.get("key", ""), "delta": top.get("delta", 0.0),
            "hint": f"可继续下钻：{top.get('dim', '')}={top.get('key', '')}"}


# ---------------------------------------------------------------- 决策层（真 Agent）

# 指标 → 可疑的乘法因子构成（用于"单量×客单价"式因子分解）。
# 保持最小显式映射，避免无端为每个指标臆造代数关系。
_FACTOR_MAP = {
    "gmv": [("order_count", "订单量"), ("aov", "客单价")],
}


def is_factor_question(question: str) -> bool:
    """**策略门控**：当前问题是否在问"乘法因子"（量价分解 / 因子贡献）。

    为什么需要门控，而不是把选择权整体交给 LLM：
    实测（26 场景决策评测）把 factorize 的决定权完全交给 LLM 时，它会把
    "本月全国GMV是多少"这类**根本没在问量价**的问题也判成 factorize（过度应用），
    整体命中率反而低于规则基线。根因是当时的 prompt 写了一句
    "若指标是 gmv/成交/销售额，优先考虑 factorize"——那是指令级偏置。

    正确的分工是：**代码判定"这个动作在此语境下是否可用"（策略），
    LLM 在允许范围内做语义判断（判断）**。本函数就是那个策略门。

    判据（关键：提到"单量/客单价"**不等于**在问量价因果）：
      - 显式因子词：因子 / 分解 / 拆解 / 拆开 / 乘积 / 量价
      - 贡献构成词：贡献 / 构成
      - 量价二选一：出现"单量类"与"客单价类"两组词之一，且用"还是/哪个"做比较
        （例如"是订单少了还是客单低了"）
    反例（应判 False，属 drill 而非 factorize）：
      - "那订单量下滑主要出在哪个州？"  只在问**按哪个维度**定位，不是量价分解
      - "最近30天日均订单量多少？"      只是在看一个数
      - "各月客单价趋势如何？"          只是在看趋势
      - "本月全国GMV是多少？"           只是在看一个数
    """
    q = str(question or "")
    vol = any(k in q for k in ("单量", "订单量", "销量", "订单数", "订单少了", "订单多"))
    price = any(k in q for k in ("客单价", "单价", "每单", "客单"))
    # 显式因子词（"拆解/分解/因子"这类，本身已表达因子意图）
    if any(k in q for k in ("因子", "分解", "拆解", "乘积", "量价")):
        return True
    # 贡献 / 构成
    if any(k in q for k in ("贡献", "构成")):
        return True
    # 量价二选一：需"比较"语义（"是订单少了还是客单低了"）
    if vol and price and any(k in q for k in ("还是", "哪个", "哪一个", "孰")):
        return True
    # "拆" + 因子词共现（"拆开看…单量和客单价"）——注意必须同时有因子词，
    # 否则"拆开看哪个品类拖累"会被误判（那是 drill，不是量价分解）
    if "拆" in q and (vol or price):
        return True
    return False


def _contribution_scattered(att: Dict, spread_frac: float = 0.6) -> bool:
    """判断维度贡献是否"分散"：主因占波动比例低于阈值视为分散，需主动换维度再拆。"""
    top = (att.get("top_contributors") or [])
    if not top:
        return False
    return abs(top[0].get("pct_of_change", 0.0)) < spread_frac


def decide_drill(att: Dict, question: str, llm=None) -> Dict:
    """Agent 决策"下一步该往哪拆"（真 Agent 决策，规则兜底）。

    返回 {"action": "drill" | "switch_dim" | "factorize" | "none",
          "reason": str, "decision_source": "llm" | "rule", **action 特有字段}。
      - drill      命中追问且主因清晰 → 沿主因维度值下钻
      - switch_dim 命中追问但贡献分散 → 主动换一个维度再拆（自主决策）
      - factorize  判断指标可疑由乘法因子构成（单量×客单价）→ 因子分解
      - none       无追问 / 无显著贡献

    三层职责（见 is_factor_question 与 evaluation/eval_decisions.py 的说明）：
      1. 策略门控（确定性）：量价问题只可能走 factorize，且由规则直接给出；
      2. 规则地板（确定性）：**没有追问语气就不是一次追问** —— 用户在看一个数/做对比，
         不该被"顺手拆一下"，这一层直接 none，不花 token 也不引入 LLM 的抖动；
      3. LLM 判断（花 token）：真实的追问（"为什么/那…呢/继续/换个角度"）里，
         "往哪儿拆、要不要换讲法"才是判断力该出场的地方。
    """
    top = (att.get("top_contributors") or [])
    q = str(question or "")
    followup_tone = any(k in q for k in ("那", "呢", "为什么", "为何", "继续", "下钻", "再看", "拆", "再"))

    def _rule() -> Dict:
        if not top:
            return {"action": "none", "reason": "无显著维度贡献", "decision_source": "rule"}
        reason = f"贡献主因：{top[0].get('desc')}"
        # 门控放行 + 确实在问量价 → factorize 是规则就能给出的合法首选。
        # 注意：此前因子分解只能靠 LLM 自己"想到"，一旦把偏置从 prompt 拿掉，
        # 规则给出的 none/drill 会直接返回，factorize 分支永远走不到（实测
        # LLM 完全没被调用、结果等于规则基线）。所以这里必须在规则层也认这个动作。
        if is_factor_question(q):
            return {"action": "factorize", "reason": reason + "；用户在问量价/因子，做乘法分解。",
                    "decision_source": "rule"}
        if followup_tone:
            if _contribution_scattered(att):
                return {"action": "switch_dim", "dim": top[0].get("dim", ""),
                        "reason": reason + "；贡献分散，建议换维度再拆。", "decision_source": "rule"}
            nxt = next_drill_suggestion(att)
            if nxt and nxt.get("dim"):
                return {"action": "drill", "path": [{"dim": nxt["dim"], "value": nxt["value"]}],
                        "reason": reason, "decision_source": "rule"}
        return {"action": "none", "reason": "无追问语气，不做下钻", "decision_source": "rule"}

    rule = _rule()
    # 确定性短路 1：门控放行 + 规则已认定在问乘法因子 → 直接 factorize，不消耗 LLM。
    # 这一步是"规则"的地盘（量价判定是确定性语义，不需要 LLM 再判一次）。
    factor_allowed = is_factor_question(q)
    if rule.get("action") == "factorize":
        return rule
    # 确定性短路 2：没有追问语气 = 用户只是在看数/做对比。
    # 这类问题规则已经能给出确定答案（none），交给 LLM 只会引入"顺手多拆一层"的
    # 过拆风险（实测 LLM 会把"本月全国GMV是多少"这类问题主动下钻），
    # 因此不花这次 token。"要不要继续动手"在无追问语气时不是判断题。
    if not followup_tone:
        return rule
    if llm is None:
        return rule

    contrib_lines = "\n".join(
        f"- {c.get('desc')}（占 {abs(c.get('pct_of_change', 0)) * 100:.0f}%）"
        for c in top[:4]) or "无显著维度拆分。"
    suggestion = next_drill_suggestion(att)
    # 策略门控：只有"在问乘法因子"时才允许 factorize（见 is_factor_question 说明）
    factor_rule = ("- factorize: 仅当用户在问订单量/客单价这类**乘法因子**时可选\n"
                   if factor_allowed else
                   "- factorize: 【本问不允许】用户并未在问订单量×客单价这类乘法因子\n")
    prompt = (
        "你是归因分析 Agent。用户上一轮看到某指标波动，现在继续追问。"
        "由你决定'下一步该拆哪儿'。可选动作：\n"
        "- drill: 沿某个维度值下钻一层（给出 dim/value）\n"
        "- switch_dim: 换一个维度重新拆（当前贡献分散时）\n"
        f"{factor_rule}"
        "- none: 用户没有要继续分析的意思（例如只是在看某个数/做对比）→ 选 none\n"
        f"指标波动与维度贡献：\n{contrib_lines}\n"
        f"当前下一步建议：{suggestion or '无'}\n"
        f"用户追问：{q}\n"
        "判据提示：不要因为指标是 GMV 就默认做因子分解——要看**用户问的是不是量价/因子**。"
        "若用户只是要某个数或做对比，应选 none。\n"
        "只输出 JSON：{\"action\":\"drill\"|\"switch_dim\"|\"factorize\"|\"none\","
        "\"dim\":\"维度key\",\"value\":\"维度值\",\"reason\":\"你的判断理由\"}"
    )
    try:
        text = llm.complete(prompt).strip().strip("`").removeprefix("json").removeprefix("JSON")
        obj = json.loads(text)
    except Exception:
        return rule
    action = obj.get("action")
    if action not in ("drill", "switch_dim", "factorize", "none"):
        return rule
    # 门控兜底：LLM 若越界选了 factorize，回退到规则建议的动作
    if action == "factorize" and not factor_allowed:
        fb = dict(rule)
        fb["reason"] = (f"[策略门控] 本问未涉及乘法因子（量价），"
                        f"LLM 的 factorize 判定被拒，回退规则建议。原判据："
                        f"{(obj.get('reason') or '')[:60]}")
        fb["decision_source"] = "rule"
        fb["gated_from"] = "factorize"
        return fb
    out = {"action": action, "reason": obj.get("reason") or rule.get("reason") or "",
           "decision_source": "llm"}
    if action == "drill":
        nxt = next_drill_suggestion(att) or {}
        out["path"] = [{"dim": obj.get("dim") or nxt.get("dim") or top[0].get("dim", ""),
                        "value": obj.get("value") or nxt.get("value") or top[0].get("key", "")}]
    elif action == "switch_dim":
        out["dim"] = obj.get("dim") or top[0].get("dim", "")
    return out


def factorize(cfg: BusinessConfig, db: sqlite3.Connection, metric_key: str,
              current_spec: str, previous_spec: Optional[str] = None) -> Dict:
    """乘法因子分解：把指标变化拆给构成它的因子（如 GMV=订单量×客单价）。

    三部分分摊（对差异做微积分分解）：
        因子A贡献 = B基期 × ΔA；因子B贡献 = A基期 × ΔB；交互项 = ΔA × ΔB。
    回报每个因子的 当期/上期/变化/变化率/占总变动占比，可直接回答"跌来自单量还是客单价"。
    """
    factors = _FACTOR_MAP.get(metric_key)
    if not factors:
        return {"ok": False,
                "reason": f"指标 {metric_key} 未定义乘法因子（当前仅支持 {list(_FACTOR_MAP)}）"}
    m = cfg.metrics.get(metric_key)
    if not m:
        return {"ok": False, "reason": "未知指标"}

    def _total(k: str, start: str, end: str):
        fm = cfg.metrics.get(k)
        if fm is None:
            return None
        sql = (f"SELECT {fm.metric_expr} AS v\n{metric_source(cfg, fm, [])}\n"
               f"{_time_where(fm.where_core, start, end)}")
        return _scalar(db, sql)

    c0, c1 = _range_spec(current_spec)
    prev_spec = previous_spec or _default_previous_spec(current_spec)
    p0, p1 = _range_spec(prev_spec)

    cur_gmv = _scalar(db, f"SELECT {m.metric_expr} AS v\n{metric_source(cfg, m, [])}\n"
                          f"{_time_where(m.where_core, c0, c1)}")
    prev_gmv = _scalar(db, f"SELECT {m.metric_expr} AS v\n{metric_source(cfg, m, [])}\n"
                           f"{_time_where(m.where_core, p0, p1)}")
    if cur_gmv is None or prev_gmv is None:
        return {"ok": False, "reason": "指标当期或上期无结果"}

    values = []
    for k, label in factors:
        c = _total(k, c0, c1)
        p = _total(k, p0, p1)
        if c is None or p is None:
            return {"ok": False, "reason": f"因子指标 {k} 无当期或上期结果，无法分解"}
        values.append((k, label, float(c), float(p)))

    A_c, A_p = values[0][2], values[0][3]      # 因子A（订单量）
    B_c, B_p = values[1][2], values[1][3]      # 因子B（客单价）
    dA, dB = A_c - A_p, B_c - B_p
    dGmv = float(cur_gmv) - float(prev_gmv)
    cA = B_p * dA
    cB = A_p * dB
    cInt = dA * dB

    def _share(x: float) -> float:
        return round(x / dGmv, 4) if dGmv else 0.0

    factors_out = [
        {"factor": values[0][0], "label": values[0][1], "current": A_c, "previous": A_p,
         "change": round(dA, 4), "change_pct": round(dA / A_p, 4) if A_p else 0.0,
         "contribution": round(cA, 4), "share": _share(cA)},
        {"factor": values[1][0], "label": values[1][1], "current": B_c, "previous": B_p,
         "change": round(dB, 4), "change_pct": round(dB / B_p, 4) if B_p else 0.0,
         "contribution": round(cB, 4), "share": _share(cB)},
    ]
    main = max([(values[0][0], cA), (values[1][0], cB)],
               key=lambda t: abs(t[1]))[0] if (dA or dB) else "无"
    return {
        "ok": True, "metric": metric_key, "metric_name": m.name,
        "current_total": cur_gmv, "previous_total": prev_gmv,
        "change": round(dGmv, 4),
        "change_pct": round(dGmv / float(prev_gmv), 4) if float(prev_gmv) else 0.0,
        "formula": f"{values[0][1]} × {values[1][1]}",
        "main_factor": main,
        "factors": factors_out,
        "interaction": {"contribution": round(cInt, 4), "share": _share(cInt)},
    }


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