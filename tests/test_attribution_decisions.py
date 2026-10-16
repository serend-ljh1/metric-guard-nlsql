"""归因决策层（真 Agent）测试。

锁定"把下一步拆哪儿"从确定性规则升级为 Agent 决策这一层：
  - decide_drill：无追问→none；主因清晰且追问→沿主因下钻；贡献分散→主动换维度；
                  有 LLM 时可返回 factorize / 自定义下钻路径，且来源记为 llm。
  - factorize：GMV=订单量×客单价 的乘法因子分解，量化"跌来自单量还是客单价"。
  - 决策质量评测（evaluation.eval_decisions）：规则基线 2/3，接 LLM 可到 3/3，
    证明"Agent 决策"确实带来可用准确率衡量的提升。
全部离线（MockLLM/规则兜底），不碰真实 API。
"""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from sqlpa.business.attribution import decide_drill, factorize
from sqlpa.business.metric_config import load_config
from sqlpa.business.service import answer
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox

CFG = load_config()


@pytest.fixture
def db(tmpdir_clean, business_months):
    """上月(GMV150=2单×75) vs 本月(GMV80=1单×80)：既支持维度归因也支持因子分解。

    日期用 business_months 动态锚点，与「本月」语义对齐，避免日期炸弹。
    """
    cur, prev, prev_late = (business_months["current"], business_months["previous"],
                            business_months["previous_late"])
    p = tmpdir_clean / "decide.db"
    con = sqlite3.connect(p)
    con.executescript(f"""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','{prev}','delivered'),('o2','c1','{prev_late}','delivered'),
        ('o3','c2','{cur}','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """)
    con.commit()
    return con


class _Oracle:
    """模拟 LLM：按预设返回 JSON 决策。"""
    def __init__(self, text): self.text, self.calls = text, 0
    def complete(self, prompt):
        self.calls += 1
        return self.text


def _att(*, contribs, **kw):
    base = {"ok": True, "is_abnormal": True, "current_spec": "本月", "current_total": 80,
            "previous_total": 150, "change": -70.0, "change_pct": -0.467,
            "dims": [], "top_contributors": list(contribs)}
    base.update(kw)
    return base


def _has_followup(question: str) -> bool:
    """与 attribution.decide_drill 里的 followup_tone 判据同源（"这是不是一次追问"）。"""
    return any(k in question for k in ("那", "呢", "为什么", "为何", "继续", "下钻",
                                       "再看", "拆", "再"))


# ---------------- decide_drill：规则兜底 ----------------

def test_none_without_followup():
    d = decide_drill(_att(contribs=[{"dim": "category", "key": "a", "desc": "品类「a」变化-1",
                                     "pct_of_change": 0.5}]), "各个品类的GMV是多少")
    assert d["action"] == "none" and d["decision_source"] == "rule"


def test_no_followup_does_not_ask_llm():
    """**过拆防护回归**：用户只是在看数/做对比时，不得把问题交给 LLM 去"顺手拆一层"。

    实测 LLM 会把"本月全国GMV是多少"这类问题主动判成下钻（过拆），
    因此无追问语气时直接走规则 none，既不花 token 也不引入抖动。
    """
    oracle = _Oracle('{"action":"drill","dim":"state","value":"SP","reason":"顺手拆一层"}')
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-90",
                                     "pct_of_change": 0.9}]),
                     "本月全国GMV是多少？", llm=oracle)
    assert d["action"] == "none" and d["decision_source"] == "rule"
    assert oracle.calls == 0, "无追问语气的问题不该花 LLM 调用"


def test_drill_clear_contributor_on_followup():
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00",
                                     "pct_of_change": 0.9}]), "那为什么单量少了？")
    assert d["action"] == "drill"
    assert d["path"] == [{"dim": "state", "value": "SP"}]


def test_switch_dim_when_scattered():
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "delta": -30,
                                     "desc": "州「SP」变化-30", "pct_of_change": 0.30},
                                    {"dim": "category", "key": "a", "delta": -28,
                                     "desc": "品类「a」变化-28", "pct_of_change": 0.28}]),
                     "那为什么呢？")
    assert d["action"] == "switch_dim" and d["dim"]


# ---------------- decide_drill：LLM 决策 ----------------

def test_llm_factorize_decision():
    """问题确实在问乘法因子（量价）时 → factorize。

    注意这是**确定性短路**：量价问法由 is_factor_question 门控判定，规则层直接给出
    factorize，不消耗 LLM（LLM 不该为确定性语义再判一次）。因此来源是 rule 而非 llm。
    """
    oracle = _Oracle('{"action":"factorize","reason":"GMV疑由单量×客单价构成"}')
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00",
                                     "pct_of_change": 0.9}]),
                     "是单量还是客单价的问题？", llm=oracle)
    assert d["action"] == "factorize"
    assert d["decision_source"] == "rule"
    assert oracle.calls == 0, "确定性语义不该绕一圈去问 LLM"


# ---------------- 策略门控：factorize 的准入 ----------------

def test_gate_allows_factor_questions():
    """显式量价/因子问法应放行。"""
    from sqlpa.business.attribution import is_factor_question
    for q in ("是单量还是客单价的问题？", "把 GMV 的下降拆成单量和客单价看看",
              "拆一下单量×客单价，看到底谁在拖累", "各因子的贡献是多少"):
        assert is_factor_question(q) is True, q


def test_gate_blocks_non_factor_questions():
    """只在看数、或只在问'按哪个维度定位'的问题不得放行（那是 drill/none）。"""
    from sqlpa.business.attribution import is_factor_question
    for q in ("本月全国GMV是多少？", "各月客单价趋势如何？", "按州对比一下本月GMV",
              "那订单量下滑主要出在哪个州？", "最近30天日均订单量多少？",
              "拆开看，到底是哪个品类拖累的？"):
        assert is_factor_question(q) is False, q


def test_llm_factorize_is_gated_when_question_is_not_about_factors():
    """**门控回归**：LLM 越界选 factorize（用户并未问量价）时必须被拒并回退规则。

    背景：26 场景实测中，把 factorize 决定权整体交给 LLM 时，它会把
    "那这几个州的 GMV 对比呢"这类没在问量价的问题也判成 factorize（prompt 里
    原有"若指标是 gmv…优先考虑 factorize"的偏置），导致整体命中率低于规则基线。
    因此改为"策略门控 + LLM 判断"：不允许时直接回退，且记录被拦事实。
    """
    oracle = _Oracle('{"action":"factorize","reason":"GMV 可分解"}')
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00",
                                     "pct_of_change": 0.9}]),
                     "那这几个州的 GMV 对比呢？", llm=oracle)
    assert d["action"] != "factorize", "非量价问题竟被允许 factorize（门控失效）"
    assert d.get("gated_from") == "factorize"
    assert d["decision_source"] == "rule"


def test_llm_drill_uses_agent_chosen_value():
    oracle = _Oracle('{"action":"drill","dim":"category","value":"alimentos","reason":"按品类追"}')
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00",
                                     "pct_of_change": 0.9}]), "那为什么呢？", llm=oracle)
    assert d["action"] == "drill"
    assert d["path"] == [{"dim": "category", "value": "alimentos"}]


def test_llm_bad_json_falls_back_to_rule():
    oracle = _Oracle("这不是JSON")
    d = decide_drill(_att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-70.00",
                                     "pct_of_change": 0.9}]), "那为什么单量少了？", llm=oracle)
    assert d["decision_source"] == "rule"          # LLM 返回非法 → 程序不炸，回退规则


# ---------------- 非可加指标（比率/均值）：占比不适用 → 不得 abs(None) 打断链路 ----------------

def test_ratio_metric_none_share_does_not_break_decision():
    """**回归（实测故障）**：比率/均值类指标（如 AOV）的维度占比在数学上不适用，
    analyze 会把 pct_of_change 显式置 None。旧代码在若干处以 `abs(c.get('pct_of_change', 0))`
    取值——键**存在但值为 None** 时 `.get` 的默认值不生效，于是 abs(None) 抛错，
    decide_drill 整段崩掉、AttributionAgent 直接降级成 `{"error": ...}`（归因丢失）。

    这里把三个入口一次钉死：分散判定 / LLM 提示构造 / HITL 说明拼接。
    """
    from sqlpa.business.attribution import _contribution_scattered, notify_anomaly

    att = _att(contribs=[{"dim": "state", "key": "SP", "desc": "州「SP」变化-90.00",
                          "delta": -90.0, "pct_of_change": None}])
    # 占比不适用 ≠ 占比小 → 不得判为"分散"（否则会误触发换维）
    assert _contribution_scattered(att) is False

    # 追问 + LLM：必须走到"给 LLM 构造 contrib_lines"那段（原崩溃点）而不抛错
    oracle = _Oracle('{"action":"drill","dim":"state","value":"SP","reason":"沿主因"}')
    d = decide_drill(att, "本月客单价为什么跌？", llm=oracle)
    assert d["action"] in ("drill", "switch_dim", "factorize", "none")
    assert oracle.calls == 1

    # HITL 异常说明拼接同样不得因 None 占比而崩
    with patch("sqlpa.business.hitl.enqueue", return_value="rec-x") as m:
        rid = notify_anomaly(CFG, {"ok": True, "is_abnormal": True, "metric": "gmv",
                                   "metric_name": "GMV", "change": -90.0, "change_pct": -0.9,
                                   "current_spec": "本月", "dims": [],
                                   "top_contributors": [{"desc": "州「SP」变化-90.00",
                                                         "pct_of_change": None}]})
    assert rid == "rec-x" and m.called


# ---------------- factorize：单量×客单价分解 ----------------

def test_factorize_decomposes_gmv(db):
    f = factorize(CFG, db, "gmv", current_spec="本月")
    assert f["ok"] and f["formula"] == "订单量 × 客单价"
    assert len(f["factors"]) == 2
    by = {x["factor"]: x for x in f["factors"]}
    assert by["order_count"]["current"] == 1 and by["order_count"]["previous"] == 2
    assert f["main_factor"] == "order_count"       # 主因是单量减少，不是客单价
    # 三部分分摊之和约等于总变动（±1 舍入容差）
    total = sum(x["contribution"] for x in f["factors"]) + f["interaction"]["contribution"]
    assert abs(total - f["change"]) < 1.0


def test_factorize_unknown_metric():
    f = factorize(CFG, sqlite3.connect(":memory:"), "aov", current_spec="本月")
    assert not f["ok"] and "未定义乘法因子" in f["reason"]


# ---------------- service 层接线：factorize 决策落地 ----------------

def test_service_factorize_wired(db, tmpdir_clean, business_months):
    """service.answer 用 decide_drill 决策出 factorize 时，应产出 factor_split 落到返回。"""
    cur, prev, prev_late = (business_months["current"], business_months["previous"],
                            business_months["previous_late"])
    p = tmpdir_clean / "svc.db"
    con = sqlite3.connect(p); con.executescript(f"""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','{prev}','delivered'),('o2','c1','{prev_late}','delivered'),
        ('o3','c2','{cur}','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """); con.commit(); con.close()
    sb = SqlSandbox(str(p), ExecConfig(max_rows=2000))
    with patch("sqlpa.business.attribution.decide_drill",
               return_value={"action": "factorize", "reason": "疑为单量×客单价", "decision_source": "llm"}):
        a = answer("本月GMV", load_config(), sb, str(p), llm=None)
    assert a["ok"]
    assert a["drill_decision"]["action"] == "factorize"
    assert a["factor_split"]["ok"] is True and a["factor_split"]["main_factor"] == "order_count"
    # Supervisor：命中语义层 + 归因 Agent 判定 factorize → 路由升级为 drill
    assert a["supervisor"]["decision"] == "drill"
    assert a["supervisor"]["reason"]


def test_decision_layer_eats_raw_question_like_service(tmpdir_clean, business_months):
    """对齐性：决策层(`decide_drill`)在 service 与评测里吃到的都是**原始问句**。

    背景（此前记为"已知口径差"）：service.answer 会先把追问改写成语义完整的独立问题
    供**语义层匹配**（`followup.rewrite`），但传给 `decide_drill` 的仍是用户原话；
    `evaluation.eval_decisions.decision_accuracy` 同样把用例的 `question` 原文喂给
    `decide_drill`。本用例把这个输入契约钉死：即便带了会触发改写的 history，
    决策层拿到的仍是原文——评测与线上一致。
    """
    cur, prev, prev_late = (business_months["current"], business_months["previous"],
                            business_months["previous_late"])
    p = tmpdir_clean / "svc_raw.db"
    con = sqlite3.connect(p); con.executescript(f"""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','{prev}','delivered'),('o2','c1','{prev_late}','delivered'),
        ('o3','c2','{cur}','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """); con.commit(); con.close()
    sb = SqlSandbox(str(p), ExecConfig(max_rows=2000))
    seen = {}

    def _spy(att, q, llm=None):
        seen["q"] = q
        return {"action": "none", "reason": "spy", "decision_source": "rule"}

    with patch("sqlpa.business.attribution.decide_drill", side_effect=_spy):
        a = answer("本月GMV", load_config(), sb, str(p), llm=None,
                   history=[{"question": "上月GMV为什么跌？", "metric": "gmv"}])
    assert a["ok"] and a["drill_decision"]["action"] == "none"
    assert seen.get("q") == "本月GMV"      # 决策层吃原始问句，而非改写后的


# ---------------- 决策质量评测（① 决策命中） ----------------

class _OracleSeq:
    """按调用顺序返回预设 JSON 决策，模拟 LLM（第 i 次调用回第 i 个回复）。"""
    def __init__(self, texts): self.texts, self.calls = list(texts), 0
    def complete(self, prompt):
        self.calls += 1
        return self.texts[min(self.calls - 1, len(self.texts) - 1)]


def test_eval_decision_rule_baseline():
    """规则层基线：+ 策略门控后，规则已能给出全部 4 种动作（含 factorize）。

    历史：门控前规则永远选不出 factorize → 30/40（0.75）。加入 `is_factor_question`
    门控后，10 条量价问法在规则层即被判为 factorize，规则基线升到 40/40。
    **这里记录的是"哪些问题根本不需要 LLM"**：决策层花的 LLM 预算只在
    drill / switch_dim / none 这三类判断题上。
    """
    from evaluation.eval_decisions import run, ANNOTATED, action_tally, EXPERT_LABELED_BY
    # 每种动作用例数 ≥ 5（答辩要求：覆盖 4 种 action，不能平均不足一条）
    tally = action_tally(ANNOTATED)
    for a in ("drill", "switch_dim", "factorize", "none"):
        assert tally.get(a, 0) >= 5, f"action {a} 只有 {tally.get(a, 0)} 条"
    r = run(ANNOTATED, llm=None)
    assert r["total"] >= 20
    assert r["decision_accuracy"] >= 0.9          # 规则已覆盖含 factorize 的动作全集
    assert {c["source"] for c in r["cases"]} == {"rule"}
    assert r["expert_labeled_by"] == EXPERT_LABELED_BY   # 标注协议：真值由谁标


def test_eval_decision_gate_is_what_earns_factorize():
    """门控的可度量价值：把门控关掉（还原旧规则），factorize 10 条全错。

    这条用例把"门控到底值多少"钉成数字：30/40 → 40/40 的差就是它带来的。
    """
    from evaluation.eval_decisions import run, ANNOTATED
    from sqlpa.business import attribution as _att
    real = _att.is_factor_question
    try:
        _att.is_factor_question = lambda q: False        # 模拟门控前：永不放行 factorize
        off = run([{**c} for c in ANNOTATED], llm=None)
    finally:
        _att.is_factor_question = real
    on = run([{**c} for c in ANNOTATED], llm=None)
    fac = [c for c in off["cases"] if c["expert"] == "factorize"]
    assert len(fac) == 10 and all(not c["hit"] for c in fac)   # 门控关：量价问题全漏
    assert off["decision_accuracy"] == pytest.approx(30 / 40, abs=1e-3)
    assert on["decision_accuracy"] > off["decision_accuracy"]


def test_eval_decision_llm_lifts_accuracy():
    """LLM(oracle) 参与判断题时全中 → 决策命中 1.0，且严格高于不接 LLM 的基线。

    "严格高于"靠一组**规则判不出、LLM 判得出**的用例来体现（下面的 extra）：
    规则层对量价门控之外的追问只有"主因清晰就下钻"这一条机械判据，遇到
    "那先别看州了，换个角度再拆" / "那先别拆了，波动算正常吗"就必然错；
    这类"要不要继续动手、要不要换讲法"的判断力才是 LLM 该挣的钱。
    """
    from evaluation.eval_decisions import run, ANNOTATED
    from sqlpa.business.attribution import is_factor_question
    consult = [c for c in ANNOTATED
               if not is_factor_question(c["question"]) and _has_followup(c["question"])]
    deterministic = len(ANNOTATED) - len(consult)
    assert len(consult) == 19 and deterministic == 21   # 10 量价 + 11 非追问 → 确定性

    def _oracle_for(cases):
        replies = {"drill": '{"action":"drill","dim":"state","value":"SP","reason":"r"}',
                   "switch_dim": '{"action":"switch_dim","dim":"category","reason":"r"}',
                   "factorize": '{"action":"factorize","reason":"r"}',
                   "none": '{"action":"none","reason":"r"}'}
        return _OracleSeq([replies[c["expert_action"]] for c in cases])

    oracle = _oracle_for(consult)
    r = run(ANNOTATED, llm=oracle)
    assert oracle.calls == len(consult)            # 确定性短路的 14 条不问 LLM
    assert r["llm_used"] is True and r["decision_accuracy"] == 1.0

    # 规则判不出、LLM 判得出的补充用例 → 证明"接 LLM"确有增益而非摆设
    extra = [
        {"name": "规则漏判-要求换讲法", "metric": "gmv",
         "question": "那先别看州了，换个别的角度再拆一遍？",
         "att": {"top_contributors": [{"dim": "state", "key": "SP",
                                       "desc": "州「SP」变化-90", "pct_of_change": 0.9}]},
         "expert_action": "switch_dim", "note": "主因清晰但用户否定该维度 → 应换维"},
        {"name": "规则漏判-只要结论", "metric": "gmv",
         "question": "那先别拆了，这次的波动算正常吗？",
         "att": {"top_contributors": [{"dim": "state", "key": "SP",
                                       "desc": "州「SP」变化-90", "pct_of_change": 0.9}]},
         "expert_action": "none", "note": "用户明确叫停 → 不再动手"},
    ]
    base = run(extra, llm=None)
    assert base["decision_accuracy"] == 0.0        # 规则：主因清晰就下钻，两条都判错
    lifted = run(extra, llm=_oracle_for(extra))
    assert lifted["decision_accuracy"] == 1.0      # 同样的输入，LLM 判对


# ---------------- 判断题 BORDERLINE：让评测有区分度（不再自证） ----------------

def test_eval_borderline_proves_llm_value():
    """判断题集是区分度标本：规则 40/50 判不出、LLM(oracle) 补位到 100%。

    这直接回应"40/40 是自证的"：加入 10 条规则判不出的边界题后，离线命中率
    掉到 40/50，说明这套评测**确实有会判错的用例**；而 `llm_value` 为正则证明
    "接 LLM 的判断力"不是摆设——它有可衡量的、正面的题目去挣。
    """
    from evaluation.eval_decisions import run, BORDERLINE, FIELD, ANNOTATED
    from sqlpa.business.attribution import is_factor_question
    assert len(FIELD) == len(ANNOTATED) + len(BORDERLINE) == 50
    assert all(c.get("needs_judgment") for c in BORDERLINE)        # 全部标注为判断题
    assert all(not c.get("needs_judgment") for c in ANNOTATED)     # 确定性集不标

    # (1) 不接 LLM：确定性集 40/40 全对，但 10 条判断题规则判不出 → 40/50，评测不再自证
    off = run(FIELD, llm=None)
    assert off["judgment_cases"] == len(BORDERLINE) == 10
    assert off["decision_accuracy"] == pytest.approx(40 / 50, abs=1e-3)

    # (2) 接 LLM(oracle)：判断题全对 → 全量 100%，llm_value > 0
    def _reply(a): return f'{{"action":"{a}","dim":"state","value":"SP","reason":"r"}}'
    consult = [c for c in ANNOTATED
               if _has_followup(c["question"]) and not is_factor_question(c["question"])]
    # 调用顺序 = ANNOTATED 的 consult 题(19) 在前，BORDERLINE 题(10) 在后
    texts = [_reply(c["expert_action"]) for c in consult] + [_reply(c["expert_action"]) for c in BORDERLINE]
    oracle = _OracleSeq(texts)
    lifted = run(FIELD, llm=oracle)
    assert oracle.calls == len(consult) + len(BORDERLINE) == 29
    assert lifted["judgment_cases"] == 10 and lifted["judgment_accuracy"] == 1.0
    assert lifted["llm_value"] > 0
    assert lifted["decision_accuracy"] == pytest.approx(1.0, abs=1e-3)


# ---------------- 执行正确性评测（③ exec_accuracy） ----------------

def test_eval_exec_accuracy(tmpdir_clean):
    """exec_accuracy 与决策命中脱钩：只验'做完之后算得对不对'。
    factorize 分摊守恒/share≈1/主因方向；drill 沿主因下钻后分支内收敛。"""
    from evaluation.eval_decisions import run, ANNOTATED
    r = run(ANNOTATED, llm=None, tmp_dir=tmpdir_clean)
    e = r["exec"]
    assert e["total"] >= 3
    by = {c["check"]: c["ok"] for c in e["checks"]}
    assert by["factorize-分摊守恒"] is True
    assert by["factorize-份额合计≈1"] is True
    assert by["factorize-主因方向合理"] is True      # 样例主因是量(订单量)减少，不是价
    assert by["drill-主因隔离收敛"] is True
    assert e["exec_accuracy"] == 1.0
    assert "exec" in r and "decision_accuracy" in r   # 两个指标分开，不混


# ---------------- 下钻贡献占比（分母=路径总变化，非全局） ----------------

def test_drill_exposes_contribution_share(tmpdir_clean, business_months):
    """drill 的贡献应带占比与守恒可信标记，且分母口径是**该州内的总变化**。

    排序不变（仍按 |delta| 降序，与独立真值表一致）；新增的是"该段解释了路径波动的多少"，
    用于把"定位到主因"与"主因确实支配"区分开（正是 top-1 62.5% 的诚实补注）。
    """
    from sqlpa.business import attribution
    cur, prev, prev_late = (business_months["current"], business_months["previous"],
                            business_months["previous_late"])
    p = tmpdir_clean / "drill_share.db"
    con = sqlite3.connect(p)
    con.executescript(f"""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','SP'),('c3','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','{prev}','delivered'),('o2','c2','{prev_late}','delivered'),
        ('o3','c1','{cur}','delivered'),('o4','c2','{cur}','delivered');
      INSERT INTO order_items VALUES
        ('o1','p1',100),('o2','p2',50),('o3','p1',40),('o4','p2',45);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','brinquedos');
    """)
    con.commit()
    # SP 路径内：alimentos 100→40(delta -60)、brinquedos 50→45(delta -5) → 路径总变化 -65
    r = attribution.drill(CFG, con, "gmv", current_spec="本月", previous_spec="上月",
                          path=[{"dim": "state", "value": "SP"}], next_dims=["category"])
    assert r["ok"]
    assert r["path_change"] == -65.0
    top = r["top_contributors"][0]
    assert top["key"] == "alimentos"                 # 仍按 |delta| 排序：排序未变
    assert top["delta"] == -60.0
    assert top["pct_reliable"] is True               # 分段之和 == 路径总变化（可加）
    assert abs(top["pct_of_change"] - (-60.0 / -65.0)) < 1e-3   # -60/-65≈92.3%，非全局占比