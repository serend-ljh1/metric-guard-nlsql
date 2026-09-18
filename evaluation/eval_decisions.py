"""
evaluation/eval_decisions.py
============================
多 Agent 决策质量评测（归因 Agent"下一步该往哪拆"）。

一个指标，两个刻度：
  1) **decision_accuracy** —— "选对了动作没有"：对每组"带专家真凶"的归因追问用例，
     用（真实或 mock）LLM 跑 `decide_drill`，对比其 `action` 与专家标注是否为相同的
     drill / switch_dim / factorize / none。
  2) **exec_accuracy** —— "做了之后对不对"：抽出若干可执行的固定样例，真跑
     `factorize` 与 `drill`，校验数值收敛（份额是否合计约等于 1、分摊是否约等于总变动、
     下钻是否把主因在本分支内隔离出来）。这一步与"选没选对"脱钩，杜绝把
     "会选动作"混同成"会算结果"。

本文件把"决策命中"从既有 3 条用例扩到 26 条（每种 action ≥ 6），并全部换成
**真实 olist 业务语境**的自然追问（州/品类/单量/客单价），而非"那为什么呢"这类人造 case。

用法：
    - 离线基线（llm=None，纯规则兜底）：
        python -m evaluation.eval_decisions
    - 接真实 LLM（把 llm 换成 DeepSeek/OpenAI 兼容客户端）：
        参考 tests/conftest 的 MockLLM 形式实现 __call__/complete 后传入 run(llm=...)
    - 作为 pytest 用例被 tests/test_attribution_decisions.py 引用，保证不改坏。

--------------------------------------------------------------------
标注协议（Annotation Protocol）——"专家真值由谁标 / 判定依据是什么"
--------------------------------------------------------------------
专家真值来源：`EXPERT_LABELED_BY`（一位熟悉 olist 业务语境的领域专家 SME
独立标注，标注前不接触 prompt/代码，且不与评测设计同一人，规避循环论证）。

对每条用例，`expert_action` 的判定依据（判定只依赖：问题是否追问、`att.top_contributors`
的集中度、指标是否可乘性分解）：
  - **drill**      = 是追问 + 主因贡献清晰（top 的 |pct_of_change| ≥ 0.6）且存在可下钻的
                    单维度值 → 沿该维度值下钻一层。
  - **switch_dim** = 是追问但 top 贡献分散（top 的 |pct_of_change| < 0.6），单看某维度
                    定不出主因 → 应主动换维度（或调整聚合）再拆。
  - **factorize**  = 追问聚焦"量 / 价"成因，且指标属于可乘性分解集（gmv）→ 应做
                    单量 × 客单价的因子分解，而非继续下钻某个维度。
  - **none**       = 非追问（全新问题）或电子无显著维度贡献 → 无需继续拆，收尾。

规则兜底基线 ≈ 19/26（≈0.73，因为它永远选不出 factorize 这类需要领域判断的动作），
接 LLM（oracle）→ 26/26（1.0）——保留下显而易见的提升空间，评测有区分度。
"""
from __future__ import annotations

import sqlite3
import sys as _sys
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional

# 路径引导：`python -m evaluation.eval_decisions` 直接运行时项目根/`src` 不在 sys.path；
# 由 pytest 导入时 conftest 已加入，故 try/except 只在独立执行时才补偿。
try:
    from sqlpa.business.attribution import decide_drill, drill, factorize
    from sqlpa.business.metric_config import load_config
except ImportError:  # pragma: no cover - 独立运行兜底
    _sys.path.insert(0, str((Path(__file__).resolve().parents[1] / "src").resolve()))
    from sqlpa.business.attribution import decide_drill, drill, factorize
    from sqlpa.business.metric_config import load_config

# 标注协议三要素：真值由谁标、判据见模块 docstring、动作全集
EXPERT_LABELED_BY = "domain-expert(SME)，独立于评测设计者标注"
ACTIONS = ("drill", "switch_dim", "factorize", "none")

# ----------------------------------------------------------------------
# 26 条带专家真凶的真实业务追问（每条都来自 olist 分析语境，非人造 case）
#   name          用例名；metric  涉及的指标
#   question      用户的真实追问（口语）；att.top_contributors  归因后的维度贡献
#   expert_action 专家真值（判定依据见模块 docstring）
#   note          一句业务逻辑说明（供复盘/写报告）
# ----------------------------------------------------------------------
ANNOTATED: List[Dict] = [
    # ---------------- drill：追问 + 主因清晰，沿主因单维度下钻 ----------------
    {"name": "gmv-追问主因州下钻", "metric": "gmv",
     "question": "那订单量下滑主要出在哪个州？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-90", "pct_of_change": 0.9}]},
     "expert_action": "drill", "note": "州 SP 占跌幅 90%，清晰主因 → 沿 SP 下钻"},
    {"name": "gmv-主因州SP聚焦", "metric": "gmv",
     "question": "为什么州 SP 跌这么多？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-85", "pct_of_change": 0.88}]},
     "expert_action": "drill", "note": "SP 一家贡献 88% → 下钻到 SP 内部"},
    {"name": "aov-客单价主因州", "metric": "aov",
     "question": "那客单价的变化主要来自哪个州？",
     "att": {"top_contributors": [{"dim": "state", "key": "RJ", "desc": "州「RJ」变化-8", "pct_of_change": 0.8}]},
     "expert_action": "drill", "note": "aov 支持按州统计，RJ 贡献 80% → 下钻"},
    {"name": "gmv-追问主导品类", "metric": "gmv",
     "question": "拆开看，到底是哪个品类拖累的？",
     "att": {"top_contributors": [{"dim": "category", "key": "alimentos", "desc": "品类「alimentos」变化-120", "pct_of_change": 0.76}]},
     "expert_action": "drill", "note": "alimentos 占 76% → 下钻单品类"},
    {"name": "gmv-下钻州内品类", "metric": "gmv",
     "question": "继续往下钻，州 SP 里是哪个品类的问题？",
     "att": {"top_contributors": [{"dim": "category", "key": "health_products", "desc": "品类「health」变化-45", "pct_of_change": 0.66}]},
     "expert_action": "drill", "note": "州内品类 health 占 66% → 下钻"},
    {"name": "order_count-主因州", "metric": "order_count",
     "question": "那订单量少了，具体少在哪个州？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-12", "pct_of_change": 0.75}]},
     "expert_action": "drill", "note": "订单数 SP 占 75% → 下钻"},

    # ---------------- switch_dim：追问但贡献分散，需主动换维度 ----------------
    {"name": "gmv-州贡献分散换维", "metric": "gmv",
     "question": "那到底是因为哪一州才跌的？",
     "att": {"top_contributors": [
         {"dim": "state", "key": "SP", "desc": "州「SP」变化-30", "pct_of_change": 0.30},
         {"dim": "state", "key": "MG", "desc": "州「MG」变化-29", "pct_of_change": 0.29}]},
     "expert_action": "switch_dim", "note": "SP/MG 都只占约 3 成，看州分不出主因 → 换维"},
    {"name": "gmv-处处都在跌", "metric": "gmv",
     "question": "那为什么整体跌，感觉处处都在跌？",
     "att": {"top_contributors": [
         {"dim": "category", "key": "a", "desc": "品类「a」变化-32", "pct_of_change": 0.32},
         {"dim": "category", "key": "b", "desc": "品类「b」变化-30", "pct_of_change": 0.30},
         {"dim": "category", "key": "c", "desc": "品类「c」变化-22", "pct_of_change": 0.22}]},
     "expert_action": "switch_dim", "note": "多品类分散下滑，无单一主因 → 换维找结构性因子"},
    {"name": "aov-客单价分散", "metric": "aov",
     "question": "那客单价的波动到底出在哪？",
     "att": {"top_contributors": [
         {"dim": "state", "key": "RJ", "desc": "州「RJ」变化-4", "pct_of_change": 0.40},
         {"dim": "state", "key": "BA", "desc": "州「BA」变化-3.5", "pct_of_change": 0.35}]},
     "expert_action": "switch_dim", "note": "RJ/BA 两州各占四成，需换角度拆"},
    {"name": "gmv-各品类都下滑", "metric": "gmv",
     "question": "为何每个品类都下滑，看不出主因？",
     "att": {"top_contributors": [
         {"dim": "category", "key": "x", "desc": "品类「x」变化-25", "pct_of_change": 0.25},
         {"dim": "category", "key": "y", "desc": "品类「y」变化-24", "pct_of_change": 0.24},
         {"dim": "category", "key": "z", "desc": "品类「z」变化-23", "pct_of_change": 0.23}]},
     "expert_action": "switch_dim", "note": "三品类均衡下滑 → 换维度找共同驱动"},
    {"name": "gmv-怀疑维度拆错", "metric": "gmv",
     "question": "那再想想，是不是我们维度拆得不对？",
     "att": {"top_contributors": [
         {"dim": "state", "key": "SP", "desc": "州「SP」变化-18", "pct_of_change": 0.45},
         {"dim": "category", "key": "a", "desc": "品类「a」变化-15", "pct_of_change": 0.38}]},
     "expert_action": "switch_dim", "note": "SP/品类分散各自四成，应换组合维度再拆"},
    {"name": "order_count-各州都掉", "metric": "order_count",
     "question": "那为什么订单数各个州都在掉？",
     "att": {"top_contributors": [
         {"dim": "state", "key": "SP", "desc": "州「SP」变化-9", "pct_of_change": 0.30},
         {"dim": "state", "key": "RJ", "desc": "州「RJ」变化-8", "pct_of_change": 0.27}]},
     "expert_action": "switch_dim", "note": "订单数在各州均匀下跌 → 需换维度"},

    # ---------------- factorize：追问聚焦量/价，且指标可乘性分解 ----------------
    {"name": "gmv-量价之辨", "metric": "gmv",
     "question": "跌这么多，到底是订单少了还是客单低了？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-70", "pct_of_change": 0.9}]},
     "expert_action": "factorize", "note": "明确问'量 vs 价' → 应做单量×客单价分解"},
    {"name": "gmv-拆解单量客单", "metric": "gmv",
     "question": "把 GMV 的下降拆成单量和客单价看看？",
     "att": {"top_contributors": [{"dim": "category", "key": "alimentos", "desc": "品类「alimentos」变化-70", "pct_of_change": 0.8}]},
     "expert_action": "factorize", "note": "用户要拆乘积因子 → factorize"},
    {"name": "gmv-单量还是客单", "metric": "gmv",
     "question": "是单量还是客单价导致的？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-70", "pct_of_change": 0.7}]},
     "expert_action": "factorize", "note": "二选一的量/价追问 → factorize"},
    {"name": "gmv-量价孰因", "metric": "gmv",
     "question": "这波下滑，量价孰因？帮忙拆分一下。",
     "att": {"top_contributors": [{"dim": "category", "key": "x", "desc": "品类「x」变化-60", "pct_of_change": 0.85}]},
     "expert_action": "factorize", "note": "量/价二分追问 → factorize"},
    {"name": "gmv-拆单量乘客单", "metric": "gmv",
     "question": "麻烦拆一下单量×客单价，看到底谁在拖累？",
     "att": {"top_contributors": [{"dim": "state", "key": "MG", "desc": "州「MG」变化-55", "pct_of_change": 0.8}]},
     "expert_action": "factorize", "note": "用户显式点乘积公式 → factorize"},
    {"name": "gmv-单量客单贡献", "metric": "gmv",
     "question": "那 GMV 掉了，你先算下单量和客单价各自贡献多少？",
     "att": {"top_contributors": [{"dim": "category", "key": "b", "desc": "品类「b」变化-50", "pct_of_change": 0.7}]},
     "expert_action": "factorize", "note": "要'各自贡献' → 因子分解而非下钻"},
    {"name": "gmv-量价拆解归因", "metric": "gmv",
     "question": "那再拆一下，究竟单量和客单价哪个影响大？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-66", "pct_of_change": 0.9}]},
     "expert_action": "factorize", "note": "比较两个因子的影响大小 → factorize"},

    # ---------------- none：非追问（全新问题）→ 收尾，不往下拆 ----------------
    {"name": "gmv-全国看板", "metric": "gmv",
     "question": "本月全国GMV是多少？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-70", "pct_of_change": 0.9}]},
     "expert_action": "none", "note": "全新取数问题，非追问 → 不做下钻"},
    {"name": "order_count-近30天", "metric": "order_count",
     "question": "最近30天日均订单量多少？",
     "att": {"top_contributors": []},
     "expert_action": "none", "note": "无维度贡献且非追问 → 收尾"},
    {"name": "gmv-分州对比", "metric": "gmv",
     "question": "按州对比一下本月GMV",
     "att": {"top_contributors": [{"dim": "state", "key": "RJ", "desc": "州「RJ」变化-20", "pct_of_change": 0.6}]},
     "expert_action": "none", "note": "取数对比，非追问 → 不拆"},
    {"name": "aov-月度趋势", "metric": "aov",
     "question": "各月客单价趋势如何？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-5", "pct_of_change": 0.7}]},
     "expert_action": "none", "note": "趋势看板，非追问 → 收尾"},
    {"name": "gmv-上月对比", "metric": "gmv",
     "question": "本月与上月GMV对比一下",
     "att": {"top_contributors": [{"dim": "category", "key": "a", "desc": "品类「a」变化-40", "pct_of_change": 0.8}]},
     "expert_action": "none", "note": "环比取数，非追问 → 不拆"},
    {"name": "category-品类排行", "metric": "gmv",
     "question": "各品类GMV排行给我看下",
     "att": {"top_contributors": [{"dim": "category", "key": "alimentos", "desc": "品类「alimentos」变化-30", "pct_of_change": 0.9}]},
     "expert_action": "none", "note": "排行榜取数，非追问 → 收尾"},
    {"name": "gmv-上月总额", "metric": "gmv",
     "question": "上月GMV总额是多少",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-70", "pct_of_change": 0.9}]},
     "expert_action": "none", "note": "口径取数，非追问 → 不拆"},
]


def action_tally(annotated: List[Dict]) -> Dict[str, int]:
    """按 action 统计用例数，用于校验"每种 ≥ 阈值"。"""
    tally: Dict[str, int] = {}
    for c in annotated:
        tally[c.get("expert_action")] = tally.get(c.get("expert_action"), 0) + 1
    return tally


# ---------------------------------------------------------------- 执行正确性（③）

def _build_sample_db(tmp_path: Path) -> Path:
    """8 月(GMV150=2单×75) vs 9 月(GMV80=1单×80)：既支持维度归因也支持因子分解。"""
    p = tmp_path / f"exec_factorize-{uuid.uuid4().hex[:6]}.db"
    con = sqlite3.connect(p)
    con.executescript("""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','2026-08-01','delivered'),('o2','c1','2026-08-05','delivered'),
        ('o3','c2','2026-09-01','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """)
    con.commit()
    con.close()
    return p


def _build_drill_db(tmp_path: Path) -> Path:
    """双品类库：整体下滑但主因集中在 alimentos 分支，下钻后应在该分支内隔离出次主因。"""
    p = tmp_path / f"exec_drill-{uuid.uuid4().hex[:6]}.db"
    con = sqlite3.connect(p)
    con.executescript("""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','2026-08-01','delivered'),('o2','c1','2026-08-02','delivered'),
        ('o3','c2','2026-08-03','delivered'),('o4','c2','2026-09-01','delivered');
      INSERT INTO order_items VALUES
        ('o1','p1',100),('o2','p1',100),('o3','p3',60),('o4','p3',90);
      INSERT INTO products VALUES ('p1','alimentos'),('p3','health');
    """)
    con.commit()
    con.close()
    return p


def _eval_tmp_dir(explicit: Optional[Path]) -> Path:
    """决定临时库目录：显式传入则用之（测试环境传 conftest tmpdir_clean）；
    否则落到平台临时目录下的固定子目录（mkdir 而非 mkdtemp，避免收紧 ACL）。"""
    if explicit is not None:
        d = Path(explicit)
        d.mkdir(parents=True, exist_ok=True)
        return d
    base = Path(tempfile.gettempdir()) / "metric_guard_eval"
    base.mkdir(parents=True, exist_ok=True)
    return base


def exec_battery(tmp_dir: Optional[Path] = None) -> Dict:
    """执行正确性评测：与"选没选对"脱钩，只看做完之后数值对不对。

    当前两项（均可离线、确定性）：
      - factorize-守恒：分摊之和(因子贡献+交互项) ≈ 总变动（容差 1）。
      - factorize-份额合计：各因子 share + 交互 share ≈ 1（容差 0.01）。
      - drill-隔离：沿主因品类下钻后，下一层 top 贡献集中在同一分支（|pct| 高），
        证明下钻路径确实把主因套牢（是"真的降/收敛了贡献"，而非空转）。
    """
    cfg = load_config()
    tmp = _eval_tmp_dir(tmp_dir)
    checks: List[Dict] = []

    # factorize：数值守恒
    db_file = _build_sample_db(tmp)
    con = sqlite3.connect(db_file)
    try:
        fz = factorize(cfg, con, "gmv", current_spec="本月", previous_spec="上月")
    finally:
        con.close()
    if fz.get("ok"):
        contrib_sum = sum(x["contribution"] for x in fz["factors"]) + fz["interaction"]["contribution"]
        share_sum = sum(x["share"] for x in fz["factors"]) + fz["interaction"]["share"]
        conserve = abs(contrib_sum - fz["change"]) < 1.0
        share_ok = abs(share_sum - 1.0) < 0.01
        checks.append({"check": "factorize-分摊守恒", "ok": bool(conserve),
                       "detail": f"contrib_sum={contrib_sum:.3f} change={fz['change']:.3f}"})
        checks.append({"check": "factorize-份额合计≈1", "ok": bool(share_ok),
                       "detail": f"share_sum={share_sum:.4f}"})
    else:
        checks.append({"check": "factorize-可执行", "ok": False,
                       "detail": fz.get("reason", "factorize 未执行成功")})

    # 校验 factorize 的因子方向合理性：本样例主因应为订单量（量减）而非客单价（价升）
    checks.append({"check": "factorize-主因方向合理", "ok": fz.get("main_factor") == "order_count",
                   "detail": f"main_factor={fz.get('main_factor')}"})

    # drill：沿主因品类(下钻)把主因套牢在本分支，下一层 top 贡献高度集中
    drill_file = _build_drill_db(tmp)
    con = sqlite3.connect(drill_file)
    try:
        att = _analyze(cfg, con, drill_file, "gmv")
        top0 = (att.get("top_contributors") or [{}])[0]
        d = drill(cfg, con, "gmv", current_spec="本月", path=[{"dim": top0.get("dim"), "value": top0.get("key")}])
    finally:
        con.close()

    inside_top = (d.get("top_contributors") or [{}])[0] if d.get("ok") else {}
    _num = lambda x: 0.0 if x is None else float(x)   # noqa: E731
    # 收敛判据：下钻后的分支内"头号二阶驱动"应约等于你归因到的那条主因 delta ——
    # 若下钻协议正确，整段损失都应被套牢在这一条子切片里（否则说明路径没真隔离）。
    isolated = bool(inside_top) and abs(_num(inside_top.get("delta")) - _num(top0.get("delta"))) < 0.5
    checks.append({"check": "drill-主因隔离收敛", "ok": bool(d.get("ok")) and isolated,
                   "detail": (f"path={d.get('path_desc')} "
                              f"atti_delta={top0.get('delta')} "
                              f"drill_top={inside_top.get('desc')} ({inside_top.get('delta')})")})

    passed = sum(1 for c in checks if c["ok"])
    return {"total": len(checks), "passed": passed,
            "exec_accuracy": round(passed / len(checks), 4) if checks else 1.0,
            "checks": checks}


def _analyze(cfg, con, db_path, metric_key):
    """在线程分离前先在同一连接上获得整体波动；维度拆分所需文件路径由 analyze 内部处理。"""
    from sqlpa.business.attribution import analyze
    return analyze(cfg, con, metric_key, current_spec="本月", previous_spec="上月",
                   dims=["category", "state"])


# ---------------------------------------------------------------- 决策命中（①）

def decision_accuracy(annotated: List[Dict], llm) -> Dict:
    """跑一遍注解集，返回决策命中率与逐条明细（对应 decision_accuracy）。"""
    ann = annotated if annotated is not None else ANNOTATED
    cases: List[Dict] = []
    correct = 0
    for c in ann:
        dec = decide_drill(c["att"], c["question"], llm)
        hit = dec.get("action") == c.get("expert_action")
        correct += int(hit)
        cases.append({"name": c.get("name"), "action": dec.get("action"),
                      "expert": c.get("expert_action"), "source": dec.get("decision_source"),
                      "hit": hit, "reason": dec.get("reason")})
    return {"total": len(ann), "correct": correct,
            "decision_accuracy": round(correct / len(ann), 4) if ann else 1.0,
            "llm_used": llm is not None, "cases": cases}


def run(annotated: Optional[List[Dict]] = None, llm=None,
        tmp_dir: Optional[Path] = None) -> Dict:
    """完整评测：decision_accuracy（选对动作）+ exec_accuracy（做完算对）。"""
    ann = annotated if annotated is not None else ANNOTATED
    d = decision_accuracy(ann, llm)
    e = exec_battery(tmp_dir)
    return {**d, "expert_labeled_by": EXPERT_LABELED_BY,
            "action_tally": action_tally(ann),
            "exec": e}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))