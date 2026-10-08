"""外部语料接入机制测试：可配置词表 / 值词表 / 判据闸 / 表达能力边界。

这批断言全部来自**外部基准（Spider dev）实测暴露的错答**，每条都对应一次真实失败：
  - 并列多指标 → 曾"只答一半"；
  - 否定/比较/取最大者 → 曾返回语义被翻转或条件被忽略的结果；
  - 拼写/未登记取值 → 曾静默丢掉过滤器、把范围放大（数值对、范围错）；
  - 同一段文字同时是两个词表的取值（world 库里 "Caribbean" 既是地区名又是语言名）
    → 曾因按字母序选词表而把地区过滤漂成语言过滤，被 support 拒掉。

测试不依赖 Spider 数据库文件（属仓库外部的可选资源）：词表用内联配置构造，
保证 CI 里也能跑。
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from sqlpa.business.metric_config import BusinessConfig, Dimension, Metric, load_config
from sqlpa.business import metric_matcher as mm

ROOT = Path(__file__).resolve().parents[1]
WORLD_CFG_PATH = ROOT / "config" / "semantic_world.yaml"
SPIDER_DB = ROOT.parent / "spider" / "database" / "world_1" / "world_1.sqlite"


def _cfg(**mk) -> BusinessConfig:
    """最小可编译配置 + 自定义关键词机制（不依赖任何真实库）。"""
    cfg = BusinessConfig(
        metrics={
            "country_count": Metric(
                key="country_count", name="国家数", desc="", metric_expr="COUNT(*)",
                from_clause="FROM country co", where_core="1=1",
                support_dims=["continent"], support_filters=["continent", "language"]),
            "city_population": Metric(
                key="city_population", name="城市人口", desc="", metric_expr="SUM(ci.Population)",
                from_clause="FROM city ci", where_core="1=1",
                support_dims=["district"], support_filters=["district"]),
            "aov": Metric(key="aov", name="客单价", desc="", metric_expr="AVG(x)",
                          from_clause="FROM orders o", where_core="1=1",
                          support_dims=["state"], support_filters=["state"]),
        },
        dimensions={
            "continent": Dimension("continent", "大洲", "co.Continent"),
            "district": Dimension("district", "辖区", "ci.District"),
            "state": Dimension("state", "州", "c.customer_state"),
        },
        filter_templates={
            "continent": "co.Continent = {value}",
            "district": "ci.District = {value}",
            "language": "cl.Language = {value}",
            "state": "c.customer_state = {value}",
        },
        matcher_keywords={
            "metrics": {
                "country_count": ["how many countries", "total population"],
                "city_population": ["total population", "how many people live in"],
                "aov": ["客单价"],
            },
            "dimensions": {"district": ["by district"]},
            "filter_values": {
                "continent": ["Asia", "Europe"],
                "district": ["Gelderland"],
                "language": ["Caribbean", "English"],
            },
            "filter_value_aliases": {"continent": {"asian": "Asia"}},
            "unsupported_patterns": [
                {"pattern": r"\bboth\b", "reason": "交集语义"},
                # 域特有词形（核心集只管否定/比较/年份区间，最值词形按域声明）
                {"pattern": r"\b(?:most|largest|greatest|highest|shortest|lowest)\b",
                 "reason": "取最大/最小者（argmax）语义"},
                {"pattern": r"\b(?:above|below|exceed)\b", "reason": "与阈值比较的筛选语义"},
            ],
        },
    )
    return cfg


# ---------------------------------------------------------------- 值词表与取值

def test_filter_values_span_conflict_prefers_declared_order():
    """同一段文字命中两个词表（region vs language）时，取**配置里先声明**的那个。

    回归：world 库里 `Caribbean` 既是地区名又是语言名，旧实现让 ftype 参与排序 →
    按字母序选中 language → 过滤条件漂成语言 → 被 support_filters 拒答。
    """
    cfg = _cfg()
    q = "What is the total surface area of the countries in the Caribbean region?"
    kept, conflicts = mm._filter_values_from_text(q, cfg)
    assert ("language", "Caribbean") in kept or ("continent", "Caribbean") in kept
    # 本内联配置里 `continent` 先于 `language` 声明，且只有它能命中"Caribbean"
    assert conflicts == {}
    assert len(kept) == 1, f"同段文字只应产生一个取值，实际 {kept}"


def test_filter_value_alias_maps_surface_form_to_canonical():
    """词形别名：`Asian` → `Asia`（库里存名词，问句用形容词）。"""
    cfg = _cfg()
    kept, _ = mm._filter_values_from_text("how many countries in Asian region", cfg)
    assert ("continent", "Asia") in kept


def test_same_filter_type_two_values_is_refused_as_or_semantics():
    """`Asia and Europe` 是「或」关系，规格里的过滤是与关系 → 必须拒答。"""
    cfg = _cfg()
    r = mm.match("total surface area of the countries in Asia and Europe", cfg)
    assert r.matched is False
    assert "多个取值" in "".join(r.reject_reasons)


# ---------------------------------------------------------------- 判据闸

def test_multi_metric_question_is_refused_not_half_answered():
    """并列两个指标 → 拒答（旧行为：挑命中词最长的那个回答，只给一半）。"""
    cfg = _cfg()
    r = mm.match("total population of Gelderland district and 客单价是多少", cfg)
    assert r.matched is False and r.method == "multi_metric"


def test_unsupported_patterns_refuse_negation_and_argmax():
    """否定/取最大者/比较 三类句式 → 拒答（QuerySpec 表达不了）。"""
    cfg = _cfg()
    cases = [
        ("How many countries do not speak English?", "否定"),
        ("What is the total number of languages used in Asia and both English?", "交集"),
        ("Which language is the most popular in Asia?", "最"),
        ("Which language is spoken by the largest number of countries?", "最"),
    ]
    for q, why in cases:
        r = mm.match(q, cfg)
        assert r.matched is False, f"{why}: {q} 不该被回答"
        assert r.method == "unsupported", (q, r.method, r.reject_reasons)


def test_unknown_entity_refuses_instead_of_dropping_filter():
    """未登记的专有名词 → 拒答。

    回归：Spider 题面把 Caribbean 写成 Carribean（拼写错误），过滤器匹配不上 →
    旧实现照原样执行，返回**全球**总面积（数值对、范围错），是实测到的最危险错答。
    """
    cfg = _cfg()
    q = "How much surface area do the countires in the Carribean cover together?"
    r = mm.match(q, cfg)
    assert r.matched is False and r.method == "unknown_entity"
    assert "Carribean" in "".join(r.reject_reasons)
    # 已知取值（含别名）不该被误判
    r2 = mm.match("how many countries in Asian region", cfg)
    assert r2.matched is True and r2.metric_key == "country_count"


def test_unknown_entity_skips_sentence_initial_word():
    """句首大写词（What/How/Give）不算专有名词，否则英文问题会被全量误杀。"""
    cfg = _cfg()
    r = mm.match("How many countries are in Asia?", cfg)
    assert r.matched is True


# ---------------------------------------------------------------- 事实表消歧

def test_metric_disambiguated_by_filter_fact_table():
    """同措辞、不同过滤落在不同事实表 → 由 support_filters 决定用哪个指标。"""
    cfg = _cfg()
    r1 = mm.match("How many people live in Gelderland district?", cfg)
    assert r1.matched and r1.metric_key == "city_population" and ("district", "Gelderland") in r1.filters
    r2 = mm.match("What is the total population of the countries in Asia?", cfg)
    assert r2.matched and r2.metric_key == "country_count"


def test_no_cross_length_metric_substitution_for_unsupported_dim():
    """**回归（真实踩过）**：不得为了让维度"能跑"而换成一个更宽泛的指标。

    外部基准引入的"事实表消歧"一度过度生效：`每单件数按订单状态`（每单件数不支持
    status）被静默改答成 `商品件数按订单状态` —— 用户问 A 得到 B，指标识别准确率
    表面仍是 100%，但口径已经漂了。消歧只允许在**命中词同长**的真并列候选之间发生。
    """
    cfg = load_config()                     # Olist 默认域
    r = mm.match("每单件数按订单状态", cfg)
    assert r.metric_key == "items_per_order", f"不得替换成更宽泛指标，实际 {r.metric_key}"
    assert r.matched is False, "该维度组合不受支持 → 应明确拒绝而不是换指标作答"
    assert "不支持" in "".join(r.reject_reasons)


# ---------------------------------------------------------------- 向后兼容

def test_olist_default_config_keeps_builtin_chinese_keywords():
    """未声明 `matcher_keywords` 的域行为不变（内置中文词表 + 无取值闸）。"""
    cfg = load_config()
    assert not (cfg.matcher_keywords or {}).get("filter_values")
    r = mm.match("每个州的GMV", cfg)
    assert r.matched and r.metric_key == "gmv" and r.dims == ["state"]
    # 没有取值词表时，"未知专有名词"闸不生效（无从判断认不认识）
    assert mm._unknown_entity("每个州的GMV", cfg) == ""


# ---------------------------------------------------------------- 配置完整性

def _world_cfg_with_fake_vocab() -> BusinessConfig:
    """用**假的取值域**加载真实配置：只验证结构自洽，不依赖仓库外的 Spider 库。"""
    def provider(table: str, column: str):
        return {
            ("country", "Continent"): ["Asia", "Europe", "Africa", "North America",
                                       "South America", "Oceania", "Antarctica"],
            ("country", "Region"): ["Caribbean", "Central Africa"],
            ("country", "GovernmentForm"): ["Republic", "Monarchy"],
            ("country", "Name"): ["Aruba", "Afghanistan"],
            ("countrylanguage", "Language"): ["English", "Chinese", "Caribbean"],
            ("city", "District"): ["Gelderland"],
        }.get((table, column), [])
    return load_config(WORLD_CFG_PATH, value_provider=provider)


def test_world_config_is_internally_consistent():
    """语义层自检：维度/过滤/别名/指标别名引用必须自洽（配置错了应立刻暴露）。"""
    cfg = _world_cfg_with_fake_vocab()
    assert len(cfg.metrics) >= 15
    for m in cfg.metrics.values():
        for d in m.support_dims:
            assert d in cfg.dimensions, f"{m.key} 声明了未定义维度 {d}"
        for f in m.support_filters:
            assert f in cfg.filter_templates, f"{m.key} 声明了未定义过滤 {f}"
    fv = cfg.matcher_keywords["filter_values"]
    for ftype in fv:
        assert ftype in cfg.filter_templates, f"取值词表 {ftype} 没有对应过滤模板"
    for ftype, al in (cfg.matcher_keywords.get("filter_value_aliases") or {}).items():
        for surface, canon in al.items():
            assert canon in fv.get(ftype, []), f"别名 {surface}→{canon} 的目标不在取值域内"


def test_world_config_keywords_match_real_metrics():
    """词表里的指标 key 必须真实存在（否则是永远命中不了的死配置）。"""
    cfg = _world_cfg_with_fake_vocab()
    for key in cfg.matcher_keywords["metrics"]:
        assert key in cfg.metrics, f"关键词表引用了不存在的指标 {key}"
    for key in cfg.matcher_keywords["dimensions"]:
        assert key in cfg.dimensions, f"关键词表引用了不存在的维度 {key}"


@pytest.mark.skipif(not SPIDER_DB.exists(), reason="Spider 数据集不在本机（仓库外资源）")
def test_external_corpus_case_end_to_end_on_real_db():
    """真实库上的端到端一例：编译→执行→与 gold 结果一致（机制可用性的最小证据）。"""
    import sqlite3

    from sqlpa.business.compiler import QuerySpec, compile_spec
    from sqlpa.sandbox.sql_executor import SqlSandbox

    def provider(table, column):
        conn = sqlite3.connect(f"file:{SPIDER_DB.as_posix()}?mode=ro", uri=True)
        try:
            return [r[0] for r in conn.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL')]
        finally:
            conn.close()

    cfg = load_config(WORLD_CFG_PATH, value_provider=provider)
    q = "What is the average life expectancy in African countries that are republics?"
    r = mm.match(q, cfg)
    assert r.matched, r.reject_reasons
    assert r.metric_key == "country_life_expectancy_avg"
    assert ("continent", "Africa") in r.filters, "形容词 African 必须经别名映射成 Africa"
    assert ("governmentform", "Republic") in r.filters
    cq = compile_spec(cfg, QuerySpec(metric=r.metric_key, dims=r.dims, filters=r.filters))
    rows = SqlSandbox(SPIDER_DB).execute(cq.sql).rows
    conn = sqlite3.connect(f"file:{SPIDER_DB.as_posix()}?mode=ro", uri=True)
    try:
        gold = conn.execute("SELECT avg(LifeExpectancy) FROM country "
                            "WHERE Continent = 'Africa' AND GovernmentForm = 'Republic'").fetchall()
    finally:
        conn.close()
    assert round(float(rows[0][0]), 6) == round(float(gold[0][0]), 6)


def test_world_config_copy_is_not_mutated_by_loading():
    """load_config 不得改写传入的 YAML 内容（解析只发生在内存里）。"""
    before = copy.deepcopy(WORLD_CFG_PATH.read_text(encoding="utf-8"))
    _world_cfg_with_fake_vocab()
    assert WORLD_CFG_PATH.read_text(encoding="utf-8") == before
