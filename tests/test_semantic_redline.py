"""语义层红线契约测试 —— 把"生成区不许 LLM 进"从 README 的原则变成代码约束。

这是项目**最硬的那块卖点**（口径由配置 + 编译器确定性生成 → 可审计、零口径漂移、
零 token），所以它不该只靠自觉，应该有测试守着。本文件锁三件事：

1. **编译确定性**：同一个 QuerySpec 反复编译，SQL 逐字相同（零口径漂移）。
2. **生成区不碰 LLM**：语义层主路径的 SQL 必须来自 compiler，而不是某个 LLM 输出；
   换一个完全不同的 LLM 返回，只要意图识别出的 spec 相同，SQL 就一模一样。
3. **红线边界是显式的**：只允许"意图识别"这一个环节自由裁量，且它必须留下
   `method`（llm/keyword）痕迹，出问题时能定位是识别漂了还是编译漂了。

一旦有人把 LLM 塞进生成链路（哪怕以"帮忙改写 SQL"的名义），1 或 2 会立刻红。
"""
from __future__ import annotations

from sqlpa.business.compiler import QuerySpec, compile_spec
from sqlpa.business.metric_config import load_config
from sqlpa.business.service import answer
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

CFG = load_config()

# 覆盖：基础指标 / 多维度 / 时间粒度 / 派生指标 / 带过滤
# 注意：这些 spec 必须都是**合法**的（编译期会强制支持度与 ratio 同源），
# 否则这个"确定性"用例会退化成在测"抛不抛异常"。
SPECS = [
    QuerySpec(metric="gmv", dims=["state"]),
    QuerySpec(metric="gmv", dims=["category", "state"]),
    QuerySpec(metric="order_count", dims=["dt"], time_grain="month"),
    QuerySpec(metric="ratio@freight_cost/gmv", dims=["state"]),
    QuerySpec(metric="aov", dims=["state"], filters=[("state", "SP")]),
    QuerySpec(metric="review_count", dims=["state"], filters=[("time_range", "2018-06")]),
]


def test_compile_is_byte_identical_across_repeats():
    """零口径漂移：同一 spec 编译 5 次必须逐字相同（逐字，不是"等价"）。"""
    for s in SPECS:
        sqls = {compile_spec(CFG, s).sql for _ in range(5)}
        assert len(sqls) == 1, f"{s.metric}/{s.dims} 编译不确定：{len(sqls)} 种输出"


def test_no_llm_in_sql_generation(tmpdir_clean, sample_db):
    """生成区不碰 LLM：换任意 LLM 返回，语义层 SQL 仍由编译器决定。

    做法：先拿到语义层实际执行的 SQL；再用一个"说什么都行"的 LLM 问同一个问题，
    断言编译产物与之无关——真正的判据是 `QuerySpec`，不是 LLM 的自由文本。
    """
    sb = SqlSandbox(sample_db, ExecConfig(max_rows=2000))

    class EchoLLM:
        """只用于意图识别；无论它回什么，只要 spec 相同，SQL 就必须相同。"""
        def __init__(self, payload): self.payload = payload
        def complete(self, prompt): return self.payload

    payloads = ['{"metric":"gmv","dims":["state"],"filters":[]}',
                '{"metric":"gmv","dims":["state"],"filters":[],"note":"我随便说的"}']
    sqls, methods = [], []
    for pl in payloads:
        # LLM 推断的口径先停在确认门（人工确认是新增的前置治理环节）
        step1 = answer("各州的GMV", CFG, sb, sample_db, llm=EchoLLM(pl))
        assert step1.get("need_confirm") and step1.get("confirm_id"), step1.get("reject")
        # 用确认记录放行：执行的是**确认卡上的 spec**，与 LLM 的自由文本无关
        r = answer("各州的GMV", CFG, sb, sample_db, llm=EchoLLM(pl),
                   confirm_id=step1["confirm_id"])
        assert r["ok"] and r["path"] == "semantic"
        sqls.append(r["sql"])
        methods.append(r.get("agent_trace", [{}])[0].get("detail", ""))
    assert sqls[0] == sqls[1], "同一 spec 因 LLM 自由文本不同而生成了不同 SQL（生成区失守）"
    # 与编译器直接产出逐字一致 → SQL 来源可指认是 compiler，不是 LLM
    assert sqls[0] == compile_spec(CFG, QuerySpec(metric="gmv", dims=["state"])).sql


def test_redline_boundary_is_observable(tmpdir_clean, sample_db):
    """红线边界必须可观测：产物要能指出"意图识别是 LLM 给的还是关键词给的"。

    当前边界（实测）：允许自由裁量的只有"意图识别"（自然语言 → QuerySpec），
    生成（QuerySpec → SQL）必须零 LLM。边界一旦不可观测，出了问题就无法区分
    "识别漂了"还是"编译漂了"——治理论证正是建立在"能指认"之上的。

    这条用例同时把当前边界钉住：若将来有人把意图识别改成"关键词优先、失败才问 LLM"，
    下面 match_method 的断言会变红，提醒同步更新 README 里的红线表述（那是改进，不是回归）。
    """
    from sqlpa.business.metric_matcher import match
    r_llm = match("各州的GMV", CFG,
                  llm=type("L", (), {"complete": lambda self, p: '{"metric":"gmv","dims":["state"]}'})())
    r_kw = match("各州的GMV", CFG, llm=None)
    assert r_llm.method == "llm" and r_kw.method == "keyword"
    # 两条路给出的 spec 一致 → 关键词层已能覆盖该问法（此问法上 LLM 是冗余的）
    assert (r_llm.metric_key, tuple(r_llm.dims)) == (r_kw.metric_key, tuple(r_kw.dims))

    sb = SqlSandbox(sample_db, ExecConfig(max_rows=2000))
    _llm = type("L", (), {"complete": lambda self, p: '{"metric":"gmv","dims":["state"]}'})()
    # LLM 路线：先拿确认记录（确认卡上的 spec 与 LLM 推断一致），确认后执行
    step1 = answer("各州的GMV", CFG, sb, sample_db, llm=_llm)
    assert step1.get("need_confirm") and step1.get("confirm_id")
    with_llm = answer("各州的GMV", CFG, sb, sample_db, llm=_llm,
                      confirm_id=step1["confirm_id"])
    without = answer("各州的GMV", CFG, sb, sample_db, llm=None)
    # 确定性关键词命中不需要确认（不打扰用户），LLM 推断的确认后记为 confirmed
    assert with_llm["match_method"] == "confirmed" and without["match_method"] == "keyword"
    # 关键：意图识别来源不同，但 SQL 逐字一致 → 生成区与 LLM 无关（红线守住了）
    assert with_llm["sql"] == without["sql"]
    assert with_llm["sql"] == compile_spec(CFG, QuerySpec(metric="gmv", dims=["state"])).sql
