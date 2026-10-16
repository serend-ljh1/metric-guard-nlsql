"""L1 受限规划循环 —— 算子登记表与有界规划器测试。

锁定三件事：
  1. **登记表 + schema 校验**：非法算子/非法参数（未知键、越界维度、坏 path）一律拒绝；
  2. **规则规划器与旧线性链等价**：首步 analyze；非异常且非续钻 → 不追加动作；
     `_step_from_decision` 把 decide_drill 的 action 正确映射到算子；
  3. **有界性**：`run_plan` 无 LLM 时立即剪枝；接会"自旋"的 LLM 时也**严格不超** `PLAN_MAX_STEPS`。

全部离线（Mock LLM），不碰真实 API、不碰真实库。
"""
from __future__ import annotations

from unittest.mock import patch

from sqlpa.analysis import operators
from sqlpa.analysis.operators import OpContext, PlanStep
from sqlpa.business.metric_config import load_config

CFG = load_config()
ATTR = {"is_abnormal": True,
        "top_contributors": [{"desc": "州「SP」变化-900", "dim": "state", "key": "SP",
                              "delta": -900, "pct_of_change": 0.5}]}


def _ctx(dims=None) -> OpContext:
    return OpContext(cfg=CFG, db=None, metric="gmv", time_spec="2018-06",
                     question="那 SP 呢，为什么跌", dims=list(dims or []))


# ---------------------------------------------------------------- 登记表 + 校验

def test_registry_contains_expected_operators():
    for name in ("get_metric", "list_dims", "analyze", "drill", "factorize",
                 "check_significance"):
        assert name in operators.OPERATORS, f"登记表缺少算子 {name}"
    # 手册可渲染（给 LLM 规划器用），且列出每个算子名
    man = operators.describe_operators()
    assert all(n in man for n in operators.OPERATORS)


def test_validate_args_rejects_unknown_operator_and_param():
    ctx = _ctx()
    ok, _, err = operators.validate_args("不存在的算子", {}, ctx)
    assert not ok and "未登记" in err
    ok, _, err = operators.validate_args("factorize", {"多余": 1}, ctx)
    assert not ok and "不接受参数" in err


def test_validate_args_analyze_dim_out_of_scope():
    ctx = _ctx()
    ok, args, _ = operators.validate_args("analyze", {"dims": ["state"]}, ctx)
    assert ok and args == {"dims": ["state"]}
    ok, _, err = operators.validate_args("analyze", {"dims": ["不存在的维度"]}, ctx)
    assert not ok and "可拆范围" in err
    ok, _, err = operators.validate_args("analyze", {"dims": "state"}, ctx)
    assert not ok and "字符串列表" in err


def test_validate_args_drill_bad_path():
    ctx = _ctx()
    ok, args, _ = operators.validate_args(
        "drill", {"path": [{"dim": "state", "value": "SP"}]}, ctx)
    assert ok and args["path"] == [{"dim": "state", "value": "SP"}]
    ok, _, err = operators.validate_args("drill", {"path": []}, ctx)
    assert not ok and "非空" in err
    ok, _, err = operators.validate_args("drill", {"path": [{"dim": "no_such"}]}, ctx)
    assert not ok and "形如" in err
    ok, _, err = operators.validate_args(
        "drill", {"path": [{"dim": "不支持的维度", "value": "x"}]}, ctx)
    assert not ok


def test_validate_args_no_param_operators():
    ctx = _ctx()
    for name in ("get_metric", "list_dims", "factorize", "check_significance"):
        ok, args, _ = operators.validate_args(name, None, ctx)
        assert ok and args == {}, name


# ---------------------------------------------------------------- 规则规划器等价

def test_rule_plan_first_step_is_analyze():
    step = operators.rule_plan_next(_ctx(dims=["state"]), [])
    assert step.op == "analyze" and step.args == {"dims": ["state"]} and step.source == "rule"


def test_rule_plan_stops_when_not_abnormal_and_not_resumed():
    history = [{"op": "analyze", "result": {"is_abnormal": False, "top_contributors": [
        {"desc": "x", "dim": "state", "key": "SP"}]}}]
    assert operators.rule_plan_next(_ctx(), history) is None


def test_step_from_decision_maps_actions():
    att = ATTR
    assert operators._step_from_decision(
        {"action": "factorize", "reason": "量价", "decision_source": "rule"}, att).op == "factorize"
    step = operators._step_from_decision(
        {"action": "drill", "path": [{"dim": "state", "value": "SP"}],
         "reason": "沿主因", "decision_source": "llm"}, att)
    assert step.op == "drill" and step.args["path"][0]["value"] == "SP"
    # switch_dim 无 path → 用主因 key 兜底
    step = operators._step_from_decision(
        {"action": "switch_dim", "dim": "state", "reason": "换角度"}, att)
    assert step.op == "drill"
    assert operators._step_from_decision({"action": "none", "reason": "不拆"}, att) is None


# ---------------------------------------------------------------- 有界性

class _SpinningLLM:
    """每调用一次就提一个新下钻值 → 若无硬上限，循环会一直跑。"""
    def __init__(self):
        self.n = 0

    def complete(self, prompt):
        self.n += 1
        return ('{"op":"drill","args":{"path":[{"dim":"state","value":"v%d"}]},"reason":"t"}'
                % self.n)


def test_run_plan_no_llm_stops_immediately_after_first_action():
    ctx = _ctx()
    history = [{"op": "drill", "result": {"ok": True, "top_contributors": []}}]
    with patch.object(operators, "run_op", return_value={"ok": True}) as mock_run:
        executed = operators.run_plan(ctx, history, question="那 SP 呢", llm=None)
    assert executed == [] and mock_run.call_count == 0   # 无 LLM → 不再追加任何动作


def test_run_plan_is_bounded_by_max_steps():
    ctx = _ctx()
    history = [{"op": "analyze", "result": ATTR}]
    with patch.object(operators, "run_op", return_value={"ok": True}) as mock_run:
        executed = operators.run_plan(ctx, history, question="那 SP 呢",
                                      llm=_SpinningLLM())
    assert len(executed) == operators.PLAN_MAX_STEPS     # 严格不超硬上限
    assert mock_run.call_count == operators.PLAN_MAX_STEPS


def test_llm_plan_next_rejects_unregistered_and_analyze():
    ctx = _ctx()
    history = [{"op": "drill", "result": {"ok": True}}]

    class _Bad:
        def __init__(self, text): self.text = text
        def complete(self, prompt): return self.text

    assert operators.llm_plan_next(ctx, history, llm=_Bad('{"op":"drop_table"}')) is None
    assert operators.llm_plan_next(ctx, history, llm=_Bad('{"op":"analyze"}')) is None
    assert operators.llm_plan_next(ctx, history, llm=_Bad('{"op":"stop"}')) is None
    assert operators.llm_plan_next(ctx, history, llm=None) is None


def test_plan_step_reject_carries_reason():
    s = PlanStep("drill", {"path": []}, "规划校验拒绝：bad", "reject")
    assert s.source == "reject" and "拒绝" in s.reason
