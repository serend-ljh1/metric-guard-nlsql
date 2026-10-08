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
    - 离线基线（llm=None，纯规则 + 策略门控）：
        python -m evaluation.eval_decisions
    - 接真实 LLM（.env 配好 LLM_API_KEY / LLM_API_BASE）：
        python -m evaluation.eval_decisions --llm --model qwen3.8-max --out evaluation/reports/decisions_llm.json
      默认**钉死单模型**（不放开模型池），避免额度耗尽时静默换模型、把别人的分数记到它头上。
    - 作为 pytest 用例被 tests/test_attribution_decisions.py 引用，保证不改坏。

--------------------------------------------------------------------
决策的三层结构 —— "哪一层该出钱、哪一层必须确定性"
--------------------------------------------------------------------
  1. **策略门控（确定性，0 token）**：`is_factor_question` 判定用户在不在问
     "单量 × 客单价"这类乘法因子。只有放行才允许 factorize，且放行后直接由规则
     给出 factorize —— 这是语义判定，不需要也不应该再问一次 LLM。
  2. **规则兜底（确定性，0 token）**：主因集中度 → drill / switch_dim；
     无追问语气 → none。它是"LLM 挂了也不会胡说"的地板。
  3. **LLM 判断（花 token）**：门控未短路、且规则给不出高分答案时，由 LLM 决定
     下一步拆哪儿、要不要继续动手。

这个划分的价值是可以被数字验证的：本文件同时输出 `decision_accuracy`（选对动作）、
`exec_accuracy`（做完算对）、`llm_calls`（这一层实际花了多少次调用）
与逐条 `source`（rule / llm），因此"门控到底省了多少、LLM 到底带来多少"
都不靠叙述，靠报告里可核对的计数。

--------------------------------------------------------------------
标注协议（Annotation Protocol）——"专家真值由谁标 / 判定依据是什么"
--------------------------------------------------------------------
专家真值来源（诚实口径）：`EXPERT_LABELED_BY` —— **本项目实现者依据下述判定规则自标**，
不是独立于实现者的第三方真人标注。因此这批 `expert_action` 只承担两个不声称泛化的身份：
(1) **回归测试**（`pytest tests` 里防改坏），(2) **方法论演示**（展示"决策正确性可以这样量化"）。
它**不是**泛化准确率的证据——系统级准确率只由公共基准 Spider-dev 支撑（其 gold 由数据源外部定义）。
若后续引入真正的独立标注第二人，可算出标注一致性并回填到报告，届时再升级本字段。判定规则见下：

对每条用例，`expert_action` 的判定依据（判定只依赖：问题是否追问、`att.top_contributors`
的集中度、指标是否可乘性分解）：
  - **drill**      = 是追问 + 主因贡献清晰（top 的 |pct_of_change| ≥ 0.6）且存在可下钻的
                    单维度值 → 沿该维度值下钻一层。
  - **switch_dim** = 是追问但 top 贡献分散（top 的 |pct_of_change| < 0.6），单看某维度
                    定不出主因 → 应主动换维度（或调整聚合）再拆。
  - **factorize**  = 追问聚焦"量 / 价"成因，且指标属于可乘性分解集（gmv）→ 应做
                    单量 × 客单价的因子分解，而非继续下钻某个维度。
  - **none**       = 非追问（全新问题）或电子无显著维度贡献 → 无需继续拆，收尾。

纯规则地板（无 `is_factor_question` 门控）在 26 条正例上约为 19/26（≈0.73，因为它永远选
不出 factorize 这类需要领域判断的动作）；加入门控后，7 条量价问法由规则确定性命中。离线全量
（26 正例 + 6 判断题）命中 **26/32 ≈ 0.81** —— 那 6 条正是规则判不出、只等 LLM 真判断的
区分标本。门控的价值与"哪些问法根本不该花 token"都由 `--llm` 模式的对照报告直接印出来。
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
EXPERT_LABELED_BY = "实现者依据模块内判定规则自标（非独立第三方；用作回归测试与方法论演示，不作泛化准确率证据）"
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


# ----------------------------------------------------------------------
# 6 条"真·判断题"（BORDERLINE）—— 命中率的分水岭
# ----------------------------------------------------------------------
# 上面 26 条 ANNOTATED，**规则 + 策略门控能确定性全对**（26/26，0 token）。
# 但一个评测如果"永远判得对"，就会退化成规格测试、量不出任何判断力。
# 这 6 条正是"规则判不出、LLM 判得出"的边界题，用来承载 `llm_value`：
#   - 规则只有"主因清晰就下钻 / 分散就换维 / 无追问就收尾"这条机械判据，
#     遇到"用户否定主因 / 叫停 / 踩在 0.6 阈值边界"就必然错；
#   - 这类"要不要继续动手、要不要换讲法"的判断力，才是 LLM 该挣的钱。
# 它们全部带 `needs_judgment: True`，是评测的**区分标本**；专家真值标注协议同上。
# 注意：这几条的量价门控均为假 → 不会误进 factorize，只有 drill/switch/none 择一。
BORDERLINE: List[Dict] = [
    {"name": "判断-用户否定主因", "metric": "gmv",
     "question": "那先别看州了，换个别的角度再拆一遍？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-90", "pct_of_change": 0.9}]},
     "expert_action": "switch_dim", "note": "主因清晰但用户否定该维度 → 应换维", "needs_judgment": True},
    {"name": "判断-用户叫停", "metric": "gmv",
     "question": "那先别拆了，这次的波动算正常吗？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-90", "pct_of_change": 0.9}]},
     "expert_action": "none", "note": "用户明确叫停 → 不再动手", "needs_judgment": True},
    {"name": "判断-阈值边界仍应下钻", "metric": "gmv",
     "question": "那真的只有这一个州在跌吗，再确认下呢？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-90", "pct_of_change": 0.58}]},
     "expert_action": "drill", "note": "pct 略低于 0.6 阈值，但语义上它就是唯一主因 → 仍应下钻", "needs_judgment": True},
    {"name": "判断-用户转移话题", "metric": "gmv",
     "question": "那先别管 GMV 了，帮我看看别的指标？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-90", "pct_of_change": 0.9}]},
     "expert_action": "none", "note": "放弃当前指标的追问 → 收尾而非下钻", "needs_judgment": True},
    {"name": "判断-阈值边界仍应换维", "metric": "gmv",
     "question": "那是不是其实整块在下滑，不该只盯这一个州？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-90", "pct_of_change": 0.62}]},
     "expert_action": "switch_dim", "note": "pct 略高于 0.6，但语义是整体下滑 → 应换维而非下钻", "needs_judgment": True},
    {"name": "判断-要结论", "metric": "gmv",
     "question": "那到底什么结论，一句话总结下呢？",
     "att": {"top_contributors": [{"dim": "state", "key": "SP", "desc": "州「SP」变化-90", "pct_of_change": 0.9}]},
     "expert_action": "none", "note": "用户要收尾总结 → 不再拆", "needs_judgment": True},
]

# 全量评测集 = 确定性集(ANNOTATED) + 判断题(BORDERLINE)
FIELD: List[Dict] = ANNOTATED + BORDERLINE


def action_tally(annotated: List[Dict]) -> Dict[str, int]:
    """按 action 统计用例数，用于校验"每种 ≥ 阈值"。"""
    tally: Dict[str, int] = {}
    for c in annotated:
        tally[c.get("expert_action")] = tally.get(c.get("expert_action"), 0) + 1
    return tally


# ---------------------------------------------------------------- 执行正确性（③）

def _month_anchors() -> Dict[str, str]:
    """与「本月 / 上月」语义对齐的日期锚点（相对运行当天动态计算）。

    回归背景（日期炸弹）：早期这里把订单日期硬编码为 2026-08 / 2026-09，
    再用 current_spec="本月" 取数；而归因的时间语义由 `attribution._now()`
    用 `date.today()` 解析。运行日期一旦不在 2026-09，当期范围就落在样例库之外，
    factorize / drill 全部 ok=False，执行正确性评测随之失败。
    """
    import datetime
    first = datetime.date.today().replace(day=1)
    prev_first = (first - datetime.timedelta(days=1)).replace(day=1)
    return {"current": first.isoformat(), "previous": prev_first.isoformat(),
            "previous_late": prev_first.replace(day=20).isoformat()}


def _build_sample_db(tmp_path: Path) -> Path:
    """上月(GMV150=2单×75) vs 本月(GMV80=1单×80)：既支持维度归因也支持因子分解。"""
    m = _month_anchors()
    p = tmp_path / f"exec_factorize-{uuid.uuid4().hex[:6]}.db"
    con = sqlite3.connect(p)
    con.executescript(f"""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','{m["previous"]}','delivered'),('o2','c1','{m["previous_late"]}','delivered'),
        ('o3','c2','{m["current"]}','delivered');
      INSERT INTO order_items VALUES ('o1','p1',100),('o2','p1',50),('o3','p2',80);
      INSERT INTO products VALUES ('p1','alimentos'),('p2','alimentos');
    """)
    con.commit()
    con.close()
    return p


def _build_drill_db(tmp_path: Path) -> Path:
    """双品类库：整体下滑但主因集中在 alimentos 分支，下钻后应在该分支内隔离出次主因。"""
    m = _month_anchors()
    p = tmp_path / f"exec_drill-{uuid.uuid4().hex[:6]}.db"
    con = sqlite3.connect(p)
    con.executescript(f"""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, customer_id TEXT,
        order_purchase_timestamp TEXT, order_status TEXT);
      CREATE TABLE order_items(order_id TEXT, product_id TEXT, price REAL);
      CREATE TABLE products(product_id TEXT, product_category_name TEXT);
      CREATE TABLE customers(customer_id TEXT, customer_state TEXT);
      INSERT INTO customers VALUES ('c1','SP'),('c2','RJ');
      INSERT INTO orders VALUES
        ('o1','c1','{m["previous"]}','delivered'),('o2','c1','{m["previous"]}','delivered'),
        ('o3','c2','{m["previous"]}','delivered'),('o4','c2','{m["current"]}','delivered');
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

def decision_accuracy(annotated: List[Dict], llm, freeze_rule_shortcircuit: bool = False) -> Dict:
    """跑一遍注解集，返回决策命中率与逐条明细（对应 decision_accuracy）。

    逐条记录 `source`（rule=规则/门控确定性给出，llm=LLM 判断给出），
    这样"哪些问题花了 LLM 的钱、哪些根本不用"是可以被审计的，而不是嘴上说。

    `freeze_rule_shortcircuit=True`：把**策略门控 + 无追问短路**这两条确定性规则
    钉死（冻结），只让 LLM 在原本就会咨询它的那部分题上判断。用于在**同一条链路**
    上量 LLM 的边际贡献——否则"不接 LLM 的基线"走的是另一套路由，两个数不可比。
    """
    ann = annotated if annotated is not None else ANNOTATED
    cases: List[Dict] = []
    correct = 0
    llm_calls = 0
    for c in ann:
        use_llm = llm
        if freeze_rule_shortcircuit:
            # 冻结确定性短路：量价题（门控放行）与无追问语气的题**不问 LLM**，
            # 分别由门控/规则给出 factorize / none；其余题照常咨询 LLM。
            from sqlpa.business.attribution import is_factor_question
            if is_factor_question(c["question"]) or not _followup(c["question"]):
                use_llm = None
        before = _llm_call_count(llm)
        dec = decide_drill(c["att"], c["question"], use_llm)
        after = _llm_call_count(llm)
        llm_calls += max(0, after - before)
        hit = dec.get("action") == c.get("expert_action")
        correct += int(hit)
        cases.append({"name": c.get("name"), "action": dec.get("action"),
                      "expert": c.get("expert_action"), "source": dec.get("decision_source"),
                      "hit": hit, "reason": dec.get("reason"),
                      "gated_from": dec.get("gated_from"),
                      "needs_judgment": bool(c.get("needs_judgment"))})
    return {"total": len(ann), "correct": correct,
            "decision_accuracy": round(correct / len(ann), 4) if ann else 1.0,
            "llm_used": llm is not None, "llm_calls": llm_calls, "cases": cases}


def _followup(question: str) -> bool:
    """与 attribution.decide_drill 的 followup_tone 同源：这一问算不算"追问"。"""
    return any(k in str(question or "")
               for k in ("那", "呢", "为什么", "为何", "继续", "下钻", "再看", "拆", "再"))


def llm_value(annotated: List[Dict] = ANNOTATED, llm=None) -> Dict:
    """给一组用例算出"接 LLM 到底值多少"。

    `decision_accuracy`（接 LLM 的整条链路命中率）对标 `rule_accuracy`
    （**同一条链路**上把 LLM 关掉的确定性命中率），差即 `llm_value`
    （负 = LLM 反而帮倒忙）。同时单独报判断题集（needs_judgment=True）的
    命中率 `judgment_accuracy` —— 这才是判断力真正受力的地方。

    `rule_accuracy` 的取法很关键：**不是**另跑一遍 `llm=None`（那会连
    "有追问语气就问 LLM"这条路由也一起改掉，分母不同、两个数不可比），
    而是冻结两条确定性短路（门控 + 无追问），只把 LLM 关掉——这样两个数
    只差"LLM 的贡献"，差值才有意义。代价是基线里 18 条真追问要用规则重算一次，
    不花 token。
    """
    ann = annotated if annotated is not None else ANNOTATED
    full = decision_accuracy(ann, llm)                                     # 接 LLM
    base = decision_accuracy(ann, None, freeze_rule_shortcircuit=True)     # 同链路、关 LLM
    jcases = [c for c in full["cases"] if c.get("needs_judgment")]
    if jcases:
        judgment_accuracy = round(sum(1 for c in jcases if c["hit"]) / len(jcases), 4)
        judgment_llm = sum(1 for c in jcases if c.get("source") == "llm")
    else:
        judgment_accuracy, judgment_llm = None, 0
    return {**full, "rule_accuracy": base["decision_accuracy"],
            "rule_correct": base["correct"],
            "llm_value": round(full["decision_accuracy"] - base["decision_accuracy"], 4),
            "judgment_cases": len(jcases),
            "judgment_accuracy": judgment_accuracy,
            "judgment_llm_calls": judgment_llm,
            "judgment_rule_calls": len(jcases) - judgment_llm}


def _llm_call_count(llm) -> int:
    """统计 LLM 客户端的调用次数。

    历史缺陷：只找 `calls`/`n_calls`/`call_count`，而 `OpenAICompatLLM` 暴露的是
    `usage`/`stats()` → 计数恒为 0（README 里"花了 N 次 LLM 调用"因此是错的）。
    现在优先读 `usage["calls"]`。
    """
    if llm is None:
        return 0
    usage = getattr(llm, "usage", None)
    if isinstance(usage, dict) and isinstance(usage.get("calls"), int):
        return int(usage["calls"])
    stats = getattr(llm, "stats", None)
    if callable(stats):
        try:
            u = (stats() or {}).get("usage") or {}
            if isinstance(u.get("calls"), int):
                return int(u["calls"])
        except Exception:  # noqa: BLE001
            pass
    for attr in ("calls", "n_calls", "call_count"):
        v = getattr(llm, attr, None)
        if isinstance(v, int):
            return v
    return 0


def run(annotated: Optional[List[Dict]] = None, llm=None,
        tmp_dir: Optional[Path] = None) -> Dict:
    """完整评测：decision_accuracy（选对动作）+ exec_accuracy（做完算对）。

    返回里额外带区分度指标（不接 LLM 也能拿到）：
      rule_accuracy / llm_value / judgment_accuracy / judgment_cases ——
      用一套"规则判不出、LLM 判得出"的判断题把 LLM 的价值量化，而不是考自证。
    """
    ann = annotated if annotated is not None else ANNOTATED
    e = exec_battery(tmp_dir)
    if llm is None:
        d = decision_accuracy(ann, None)              # 离线：全确定性，无 LLM
        return {**d, "rule_accuracy": d["decision_accuracy"], "llm_value": 0.0,
                "judgment_cases": sum(1 for c in ann if c.get("needs_judgment")),
                "judgment_accuracy": None, "judgment_llm_calls": 0,
                "expert_labeled_by": EXPERT_LABELED_BY,
                "action_tally": action_tally(ann), "exec": e}
    d = llm_value(ann, llm)                           # 接 LLM：同时给规则基线与增值
    return {**d, "expert_labeled_by": EXPERT_LABELED_BY,
            "action_tally": action_tally(ann), "exec": e}


def _build_llm(model: str, pool: bool):
    """构造真实 LLM。

    默认 `pool=False`：把模型池收成"就这一个模型"。评测必须可复现，
    若放开模型池，某个模型 403/额度耗尽时会静默换到别的模型，测出来的
    数字就不再是"这个模型"的成绩（历史踩过：50 例跑分被误当成单模型结果）。
    """
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:  # pragma: no cover
        pass
    try:
        from sqlpa.config import ensure_utf8_console
        ensure_utf8_console()
    except ImportError:  # pragma: no cover
        pass
    from sqlpa.llm.openai_compat import OpenAICompatLLM
    return OpenAICompatLLM(model=model, temperature=0.0,
                           model_pool=[model] if not pool else None, max_retries=0)


def _report(r: Dict, tag: str) -> None:
    """把一次评测打成人能看的结论：命中率、决策来源、被门控拦下的、判错的。"""
    src: Dict[str, int] = {}
    for c in r["cases"]:
        src[c["source"]] = src.get(c["source"], 0) + 1
    print(f"\n=== {tag} ===")
    print(f"决策命中 decision_accuracy = {r['correct']}/{r['total']} "
          f"= {r['decision_accuracy']:.1%}")
    print(f"确定性基线 rule_accuracy   = {r['decision_accuracy'] - r.get('llm_value', 0):.1%}  "
          f"(纯规则+门控，0 token)   LLM 增值 llm_value = {r.get('llm_value', 0):+.1%}")
    if r.get("judgment_cases"):
        print(f"其中判断题 {r['judgment_cases']} 条（规则判不出）命中 "
              f"{r.get('judgment_accuracy') or 0:.1%}"
              f"  → 这是判断力真正受力的地方（花了 {r.get('judgment_llm_calls', 0)} 次 LLM）")
    print(f"执行正确 exec_accuracy      = {r['exec']['passed']}/{r['exec']['total']} "
          f"= {r['exec']['exec_accuracy']:.1%}")
    print(f"决策来源 {src}"
          f"    LLM 实际调用 {r.get('llm_calls', 0)} 次（确定性短路省下的调用不花钱）")
    gated = [c for c in r["cases"] if c.get("gated_from")]
    if gated:
        print(f"被策略门控拦下（LLM 越界选 factorize）{len(gated)} 条："
              + ", ".join(str(c["name"]) for c in gated))
    bad = [c for c in r["cases"] if not c["hit"]]
    if bad:
        print(f"判错 {len(bad)} 条：")
        for c in bad:
            print(f"  x {str(c['name'])[:32]:34s} 判={str(c['action']):11s} "
                  f"期望={str(c['expert']):11s} 源={c['source']}")
    else:
        print("判错 0 条")


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口：

        python -m evaluation.eval_decisions                    # 离线规则基线（不花钱）
        python -m evaluation.eval_decisions --llm              # 真实 LLM 参与判断
        python -m evaluation.eval_decisions --llm --model qwen3.8-max --out evaluation/reports/decisions_llm.json
    """
    import argparse
    import json

    try:  # Windows 控制台默认 GBK，中文/符号会炸 → 统一抬到 UTF-8
        from sqlpa.config import ensure_utf8_console
        ensure_utf8_console()
    except ImportError:  # pragma: no cover
        pass

    ap = argparse.ArgumentParser(description="归因 Agent 决策质量评测（专家自标真值：26 确定性 + 6 判断题；用作回归与演示，非泛化准确率证据）")
    ap.add_argument("--llm", action="store_true", help="接真实 LLM 参与决策（需要 .env 里的 key）")
    ap.add_argument("--model", default=None, help="指定模型（默认取 .env 的 LLM_MODEL）")
    ap.add_argument("--pool", action="store_true",
                    help="允许模型池自动切换（默认关闭：评测必须钉死单模型才可复现）")
    ap.add_argument("--out", default=None, help="把完整结果写成 JSON 到该路径")
    args = ap.parse_args(argv)

    llm = None
    if args.llm:
        import os
        model = args.model or os.environ.get("LLM_MODEL") or "qwen3.8-max"
        llm = _build_llm(model, args.pool)
        print(f"评测模型：{model}（模型池 {'开' if args.pool else '关'}）")

    # 默认评测全量集 FIELD（确定性 + 判断题），这样才能量出 llm_value 的区分度。
    r = run(FIELD, llm=llm)
    _report(r, "规则 + 策略门控" + ("  + LLM 判断" if llm else "（离线基线：判断题判不出）"))

    if llm is not None:
        base = run(FIELD, llm=None)
        _report(base, "对照：不接 LLM（规则 + 门控，全部确定性）")
        print(f"\nLLM 净增益：{r['decision_accuracy']:.1%} vs 规则 {base['decision_accuracy']:.1%}"
              f"（判定题 {r.get('judgment_cases', 0)} 条是区分度来源）")

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
