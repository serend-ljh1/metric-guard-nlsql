"""L1 受限规划循环 —— 归因算子登记表（operator registry）与有界规划器。

背景：归因 Agent 原本是一条**写死的线性链**（analyze → decide_drill → 一个动作）。
这里把它重构成一个**有界规划循环（bounded ReAct）**：Agent 每一轮从登记表里挑一个算子、
传经 schema 校验的参数、执行、观察，再决定下一步；直到 `stop` 或触达硬上限 `PLAN_MAX_STEPS`。

红线（与项目铁律一致）：
  - 登记表中的算子**全部包装既有确定性实现**（`attribution.analyze/drill/factorize/
    significance_test/daily_series`）；算子内部仍走确定性编译 + 只读沙箱；
  - 规划器（LLM 或规则）**只能"挑算子 + 填参数"**，参数先过 `validate_args` 的 schema 校验，
    非法即拒绝该步 —— 规划器**永不生成 SQL、永不直连库、永不见原始行**；
  - **无 LLM 时**由规则规划器驱动，算子序列与旧线性链一致（回退保证，由测试钉死）。

用法（orchestrator 里）：
    ctx = OpContext(cfg=cfg, db=db, metric=..., time_spec=..., dims=dims)
    history = [{"op": "analyze", "result": att}]
    run_plan(ctx, history, question=q, llm=llm, on_step=emit_step)
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from sqlpa.business import attribution
from sqlpa.business.metric_config import BusinessConfig

# 硬上限：一次规划最多执行多少步（防止规划器自旋）。
PLAN_MAX_STEPS = 4


# ---------------------------------------------------------------- 上下文 / 数据结构

@dataclass
class OpContext:
    """算子执行上下文：只暴露"确定性计算需要的东西"，不带 LLM、不带原始行。"""
    cfg: BusinessConfig
    db: sqlite3.Connection
    metric: str
    time_spec: str
    threshold_pct: float = 0.05
    question: str = ""
    dims: List[str] = field(default_factory=list)

    @property
    def allowed_dims(self) -> List[str]:
        """该指标**允许拆解**的维度（支持维度 ∩ 配置维度）。"""
        m = self.cfg.metrics.get(self.metric)
        if m is None:
            return []
        return [d for d in m.support_dims if d in self.cfg.dimensions]


@dataclass
class Operator:
    name: str
    desc: str
    schema: Dict[str, str]          # 参数名 -> 参数说明（只允许登记表里出现的键）
    fn: Callable[[OpContext, Dict], Dict]


@dataclass
class PlanStep:
    """规划器给出的一步：挑哪个算子、传什么参数、为什么。"""
    op: str
    args: Dict = field(default_factory=dict)
    reason: str = ""
    source: str = "rule"            # rule | llm | memory | reject
    decision: Optional[Dict] = None  # 若来自 decide_drill，携带完整决策（供 L4 轨迹展示）


# ---------------------------------------------------------------- 算子实现（全部包装既有确定性函数）

def _op_get_metric(ctx: OpContext, args: Dict) -> Dict:
    m = ctx.cfg.metrics.get(ctx.metric)
    if m is None:
        return {"ok": False, "reason": f"未知指标 {ctx.metric}"}
    return {"ok": True, "metric": ctx.metric, "name": m.name,
            "support_dims": list(m.support_dims), "additive": attribution.is_additive(m),
            "factorizable": ctx.metric in attribution._FACTOR_MAP}


def _op_list_dims(ctx: OpContext, args: Dict) -> Dict:
    return {"ok": True, "dims": ctx.allowed_dims}


def _op_analyze(ctx: OpContext, args: Dict) -> Dict:
    return attribution.analyze(ctx.cfg, ctx.db, ctx.metric, current_spec=ctx.time_spec,
                               dims=args.get("dims"), threshold_pct=ctx.threshold_pct)


def _op_drill(ctx: OpContext, args: Dict) -> Dict:
    return attribution.drill(ctx.cfg, ctx.db, ctx.metric, current_spec=ctx.time_spec,
                             path=args["path"], threshold_pct=ctx.threshold_pct)


def _op_factorize(ctx: OpContext, args: Dict) -> Dict:
    return attribution.factorize(ctx.cfg, ctx.db, ctx.metric, current_spec=ctx.time_spec)


def _op_check_significance(ctx: OpContext, args: Dict) -> Dict:
    """统计门控算子：复用既有 `daily_series` + `significance_test`（确定性、零 token）。

    与 analyze 内部的门控同一套实现 —— 这里把它单独抽成一个算子，供规划器显式调用。
    """
    m = ctx.cfg.metrics.get(ctx.metric)
    if m is None:
        return {"ok": False, "reason": f"未知指标 {ctx.metric}"}
    try:
        db_path = ctx.db.execute("PRAGMA database_list").fetchone()[2]
    except Exception:  # noqa: BLE001
        db_path = None
    if not db_path:
        return {"ok": False, "reason": "需要文件型数据库才能取日序列"}
    asof = attribution._now()
    c0, c1 = attribution._quote(attribution._resolve_window(ctx.time_spec, asof))
    p0, p1 = attribution._quote(attribution._previous_window(ctx.time_spec, asof))
    sig = attribution.significance_test(
        attribution.daily_series(ctx.cfg, db_path, m, c0, c1),
        attribution.daily_series(ctx.cfg, db_path, m, p0, p1))
    return {"ok": True, **sig}


OPERATORS: Dict[str, Operator] = {}


def _register(op: Operator) -> None:
    OPERATORS[op.name] = op


_register(Operator("get_metric", "读取当前指标的元信息（支持维度/是否可加/可否因子分解）", {}, _op_get_metric))
_register(Operator("list_dims", "列出当前指标允许拆解的维度", {}, _op_list_dims))
_register(Operator("analyze", "按维度拆解指标波动，返回主因贡献者（当期/上期对比）",
                   {"dims": "可选，字符串列表；缺省用指标支持维度"}, _op_analyze))
_register(Operator("drill", "沿某个维度值继续下钻一层",
                   {"path": "必填，形如 [{\"dim\":\"state\",\"value\":\"SP\"}]"}, _op_drill))
_register(Operator("factorize", "把可加指标的变化拆给乘法因子（如 GMV=订单量×客单价）", {}, _op_factorize))
_register(Operator("check_significance", "对当前窗口做日粒度 Welch z 检验（统计门控，确定性）",
                   {}, _op_check_significance))


# ---------------------------------------------------------------- 参数 schema 校验

def validate_args(name: str, args: Optional[Dict], ctx: OpContext) -> tuple:
    """校验"算子名 + 参数"是否合法。返回 `(ok, normalized_args, error)`。

    这是规划循环的**唯一入口闸**：LLM 给的任何参数都必须先过这里，非法即拒绝该步。
    """
    op = OPERATORS.get(name)
    if op is None:
        return False, None, f"未登记的算子「{name}」（可选：{', '.join(OPERATORS)}）"
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return False, None, "参数必须是对象（JSON object）"
    unknown = [k for k in args if k not in op.schema]
    if unknown:
        return False, None, (f"算子「{name}」不接受参数 {unknown}"
                             f"（可传：{list(op.schema) or '无'}）")

    if name == "analyze":
        dims = args.get("dims")
        if dims is not None:
            if not isinstance(dims, list) or not all(isinstance(d, str) for d in dims):
                return False, None, "dims 必须是字符串列表"
            bad = [d for d in dims if d not in ctx.allowed_dims]
            if bad:
                return False, None, (f"维度 {bad} 不在指标「{ctx.metric}」的可拆范围 "
                                     f"{ctx.allowed_dims}")
        return True, {"dims": dims}, ""

    if name == "drill":
        path = args.get("path")
        if not isinstance(path, list) or not path:
            return False, None, "drill 需要非空的 path=[{dim,value}]"
        norm: List[Dict] = []
        for seg in path:
            if not isinstance(seg, dict) or "dim" not in seg or "value" not in seg:
                return False, None, "path 每段必须形如 {dim, value}"
            d, v = seg["dim"], seg["value"]
            if d not in ctx.cfg.filter_templates:
                return False, None, f"维度「{d}」不支持路径过滤（无 filter_template）"
            if not isinstance(v, str) or not v.strip():
                return False, None, "path.value 必须是非空字符串"
            norm.append({"dim": d, "value": v})
        return True, {"path": norm}, ""

    # 无参算子（get_metric / list_dims / factorize / check_significance）
    return True, {}, ""


def run_op(name: str, ctx: OpContext, args: Dict) -> Dict:
    """执行算子（参数须已过 `validate_args`）。"""
    return OPERATORS[name].fn(ctx, args)


def describe_operators() -> str:
    """登记表的人话手册（给 LLM 规划器或文档用）。"""
    lines = []
    for op in OPERATORS.values():
        params = "、".join(f"{k}（{v}）" for k, v in op.schema.items()) or "无参数"
        lines.append(f"- {op.name}: {op.desc} ｜ 参数：{params}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 归因链条观测摘要

def summarize_observation(history: List[Dict]) -> str:
    """把"到目前为止的观测"压成**摘要**（不是原始行）交给规划器。"""
    if not history:
        return "（尚未执行任何算子）"
    lines = []
    for h in history:
        op = h.get("op")
        r = h.get("result") or {}
        if op == "analyze" or op == "drill":
            tops = (r.get("top_contributors") or [])[:3]
            desc = "；".join(c.get("desc", "") for c in tops) or "无显著维度贡献"
            chg = r.get("change_pct")
            lines.append(f"- {op}：波动 {chg * 100:+.1f}%（若异常）"
                         if isinstance(chg, (int, float)) else f"- {op}：{desc}")
            lines.append(f"    主因：{desc}｜路径：{r.get('path_desc', '')}")
        elif op == "factorize":
            facs = (r.get("factors") or [])
            lines.append(f"- factorize：主因子 {r.get('main_factor')}；"
                         f"{'；'.join(f.get('label', '') + f'贡献{float(f.get("share") or 0) * 100:+.0f}%' for f in facs)}")
        elif op == "check_significance":
            lines.append(f"- check_significance：tested={r.get('tested')} "
                         f"p={r.get('p_value')} 显著={r.get('is_significant')}")
        else:
            lines.append(f"- {op}：{str(r)[:80]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 规划器

def _step_from_decision(decision: Dict, att: Dict) -> Optional[PlanStep]:
    """把 `decide_drill` 的动作映射到登记表里的算子（与旧线性链逐字一致）。"""
    action = decision.get("action")
    src = decision.get("decision_source", "rule")
    reason = decision.get("reason", "")
    if action == "factorize":
        return PlanStep("factorize", {}, reason, src, decision=decision)
    if action in ("drill", "switch_dim") and (decision.get("path") or decision.get("dim")):
        path = decision.get("path")
        if not path and decision.get("dim"):
            path = [{"dim": decision["dim"],
                     "value": (att.get("top_contributors") or [{}])[0].get("key", "")}]
        if path:
            return PlanStep("drill", {"path": path}, reason, src, decision=decision)
    return None


def rule_plan_next(ctx: OpContext, history: List[Dict], *, question: str = "",
                   resumed: bool = False, resume_path: Optional[List[Dict]] = None,
                   llm=None) -> Optional[PlanStep]:
    """确定性规则规划器：**只用登记表里的算子**，产出与旧线性链一致的序列。

    - 首步（history 只有 analyze）：异常或续钻才落一个动作（factorize / drill / 停）；
    - 其后：规则规划器**不再追加**（旧链每轮只做一个动作），多步链只由 LLM 规划器开。
    """
    if not history:
        return PlanStep("analyze", {"dims": ctx.dims}, "首步固定做维度拆解", "rule")
    last = history[-1]
    if last.get("op") != "analyze":
        return None
    att = last.get("result") or {}
    if not (att.get("is_abnormal") or resumed):
        return None
    if resumed and resume_path:
        return PlanStep("drill", {"path": resume_path}, "会话记忆续钻上轮主因", "memory")
    decision = attribution.decide_drill(att, question or ctx.question, llm)
    return _step_from_decision(decision, att)


def llm_plan_next(ctx: OpContext, history: List[Dict], *, question: str = "",
                  llm=None) -> Optional[PlanStep]:
    """LLM 规划器：让模型在**登记表**里挑下一个算子（或 stop）。

    给模型的是：算子手册（名字 + 参数 + 说明）+ 到目前为止的**观测摘要**（不是原始行）。
    LLM 只输出 JSON `{"op":..., "args":{...}, "reason":...}`；参数非法 → 本步作废（停）。
    """
    if llm is None:
        return None
    prompt = (
        "你是归因分析 Agent 的**规划器**。你已经看过前面几步的观测摘要，"
        "现在要决定'下一步调哪个算子'，或选择 stop 结束。\n"
        "可选算子（只能从下列算子中挑，且参数须符合其说明）：\n"
        f"{describe_operators()}\n"
        "- stop: 信息已足够，结束规划\n"
        f"到目前为止的观测摘要：\n{summarize_observation(history)}\n"
        f"用户问题：{question or ctx.question}\n"
        "约束：不得重复执行已经做过的算子+参数；每次最多再走 1-2 步就够了。\n"
        "只输出 JSON：{\"op\":\"算子名或 stop\",\"args\":{...},\"reason\":\"你的理由\"}"
    )
    try:
        text = llm.complete(prompt).strip().strip("`").removeprefix("json").removeprefix("JSON")
        obj = json.loads(text)
    except Exception:  # noqa: BLE001 —— 规划失败即停，不影响确定性主链
        return None
    op = obj.get("op") or obj.get("action")
    if op in (None, "stop", "none", "结束"):
        return None
    if op == "analyze":
        # 首步已做过 analyze，续步再拆一次无意义 → 视为停。
        return None
    if op not in OPERATORS:
        return None
    args = obj.get("args") or {}
    ok, _nargs, _err = validate_args(op, args, ctx)
    if not ok:
        return None
    return PlanStep(op, args, obj.get("reason", ""), "llm")


def plan_next(ctx: OpContext, history: List[Dict], *, question: str = "",
              llm=None, resumed: bool = False, resume_path: Optional[List[Dict]] = None
              ) -> Optional[PlanStep]:
    """规划下一步：首步沿用既有 `decide_drill`（含策略门控/规则地板/LLM 判断）；
    其后（仅接 LLM 时）交给 LLM 规划器做**有界**的多步链。
    """
    last = history[-1] if history else None
    if last is None or last.get("op") == "analyze":
        if last is None:
            return PlanStep("analyze", {"dims": ctx.dims}, "首步固定做维度拆解", "rule")
        att = last.get("result") or {}
        if not (att.get("is_abnormal") or resumed):
            return None
        if resumed and resume_path:
            return PlanStep("drill", {"path": resume_path}, "会话记忆续钻上轮主因", "memory")
        decision = attribution.decide_drill(att, question or ctx.question, llm)
        return _step_from_decision(decision, att)
    # 后续步骤：无 LLM 的规则链一个动作即止；接 LLM 才可能继续多步
    if llm is None:
        return None
    return llm_plan_next(ctx, history, question=question, llm=llm)


# ---------------------------------------------------------------- 有界循环执行

def run_plan(ctx: OpContext, history: List[Dict], *, question: str = "", llm=None,
             resumed: bool = False, resume_path: Optional[List[Dict]] = None,
             on_step: Optional[Callable[[PlanStep, Optional[Dict]], None]] = None
             ) -> List[Dict]:
    """执行**有界规划循环**：每轮 `plan_next → validate_args → run_op`，最多 `PLAN_MAX_STEPS` 步。

    on_step(step, result)：每步回调（result=None 表示该步被规划校验拒绝或规划停）。
    返回已执行步骤列表 `[{"plan": PlanStep, "result": Dict}]`。
    """
    executed: List[Dict] = []
    seen: set = set()
    while len(executed) < PLAN_MAX_STEPS:
        step = plan_next(ctx, history, question=question, llm=llm,
                         resumed=resumed, resume_path=resume_path)
        if step is None:
            break
        ok, nargs, err = validate_args(step.op, step.args, ctx)
        if not ok:
            rejected = PlanStep(step.op, step.args, f"规划校验拒绝：{err}", "reject",
                                decision=step.decision)
            if on_step:
                on_step(rejected, None)
            break
        key = (step.op, json.dumps(nargs, ensure_ascii=False, sort_keys=True))
        if key in seen:
            break               # 防自旋：同一步骤不重复执行
        seen.add(key)
        result = run_op(step.op, ctx, nargs)
        history.append({"op": step.op, "result": result})
        executed.append({"plan": step, "result": result})
        if on_step:
            on_step(step, result)
    return executed
