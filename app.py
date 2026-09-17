"""
app.py
======
Streamlit 前端：多智能体 Text-to-SQL 智能取数产品。

六个页面（左侧切换）：
  - 🔐 登录认证：用户名+密码登录，角色绑定（admin/analyst）。
  - 💼 业务取数：多轮对话式取数。口径内→配置公式硬约束(认证徽章)；
    口径外→引擎多Agent自由生成(降级标注)；自动图表；支持 👍/👎 反馈与 ⭐ 收藏。
  - 📜 查询历史：按用户查看历史查询记录。
  - ⭐ 收藏夹：收藏常用查询。
  - ⚙️ 指标中心：指标/维度/别名的可视化管理。
  - 🗄️ 数据源管理：SQLite / MySQL / PostgreSQL 连接注册与连通性测试。
  - 🧪 引擎演示：Spider 真实基准单题演示 + 实测消融结果 + 多Agent编排链路可视化。

运行：pip install -r requirements.txt && streamlit run app.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "tools"))

# 从 app 目录加载 .env，避免受启动 cwd 影响
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass


@st.cache_resource
def _business_cfg():
    from sqlpa.business.metric_config import load_config
    return load_config()


@st.cache_resource
def _sample_db() -> str:
    real = ROOT / "data" / "olist" / "olist.db"
    if real.exists():
        return str(real)
    from build_olist_sample import build
    build()
    return str(ROOT / "data" / "olist_sample" / "sample.db")


def _has_key() -> bool:
    return bool(os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("OPENAI_API_KEY"))


@st.cache_resource
def _real_llm():
    from sqlpa.llm.openai_compat import OpenAICompatLLM
    return OpenAICompatLLM()


@st.cache_resource(show_spinner=False)
def _sandbox_for(ds_id: str, sqlite_path: str):
    """按数据源创建沙箱（缓存）。外部源走方言连接器，内置源走 SQLite。"""
    from sqlpa.business.datasources import get_datasource, to_sandbox_spec
    from sqlpa.sandbox.sql_executor import ExecConfig, make_sandbox
    ds = get_datasource(ds_id) or {"id": "builtin_sqlite", "kind": "sqlite"}
    spec = to_sandbox_spec(ds, sqlite_path)
    return make_sandbox(spec, ExecConfig.from_settings(max_rows=2000))


# ============================================================================
# 💼 业务取数（多轮对话 · 分级放行 · 自动图表）
# ============================================================================

def _render_answer(a: dict, cfg) -> None:
    """渲染一次回答：认证徽章 / 口径说明 / SQL / 图表 / 表格。"""
    if a.get("used_context"):
        st.caption(f"🔗 已结合上下文理解：{a['rewritten_question']}")

    if not a["ok"]:
        st.error(("⛔ 拦截：" if a.get("matched") else "✗ ") + a["reject"])
        if a.get("sql"):
            with st.expander("查看被拦截的 SQL"):
                st.code(a["sql"], language="sql")
        return

    if a["mode"] == "metric":
        m = cfg.metrics[a["metric"]]
        st.success(f"🛡️ 口径已认证 · {m.name} = {m.desc}")
        st.info(f"公式来自配置(硬约束,已校验): `{a['metric_expr']}` | "
                f"来源 {a['source']} | 维度 {a['dims']}")
    else:
        st.warning("🔓 自由查询 · 未经口径认证：该问题不在指标中心配置范围内，"
                   "结果由多Agent引擎生成，口径请自行核对。")

    with st.expander("生成 SQL", expanded=False):
        st.code(a["sql"], language="sql")

    cols, rows = a.get("columns", []), a.get("rows", [])
    if not cols:
        st.write("(无结果)")
        return

    # ---- 自动图表推荐 ----
    from sqlpa.business.charting import build_altair, recommend
    spec = recommend(cols, rows)
    if spec.get("kind") == "metric":
        v = rows[0][cols.index(spec["y"])]
        st.metric(spec.get("label") or spec["y"],
                  f"{v:,.2f}" if isinstance(v, float) else v)
    else:
        chart = build_altair(spec, cols, rows)
        if chart is not None:
            st.altair_chart(chart, use_container_width=True)

    st.dataframe([dict(zip(cols, r)) for r in rows[:200]], hide_index=True)
    if len(rows) > 200:
        st.caption(f"仅显示前 200 行（共 {len(rows)} 行）")

    if a.get("agent_trace"):
        with st.expander("🧩 多Agent编排链路（谁 · 干了什么 · 耗时）"):
            st.dataframe([{"#": i + 1, "Agent": t["agent"], "角色": t["role"],
                           "做了什么": t["detail"], "耗时": f"{t['ms']}ms"}
                          for i, t in enumerate(a["agent_trace"])], hide_index=True)


def _business_mode() -> None:
    st.subheader("💼 业务取数（多轮对话 · 口径认证 · 分级放行）")
    cfg = _business_cfg()
    user = st.session_state.user
    role = user["role"]  # 角色来自登录态，不再由侧边栏切换

    from sqlpa.business.datasources import display, list_datasources
    ds_list = list_datasources()
    with st.sidebar:
        st.caption(f"当前角色：**{role}**")
        ds_id = st.selectbox("数据源", [d["id"] for d in ds_list],
                             format_func=lambda i: next(d["name"] for d in ds_list if d["id"] == i))
        if st.button("🗑️ 新对话（清空上下文）"):
            st.session_state.pop("biz_history", None)
            st.rerun()

    ds = next(d for d in ds_list if d["id"] == ds_id)
    sqlite_path = _sample_db()
    st.caption(f"数据源：{display(ds) if ds.get('kind') != 'sqlite' else 'SQLite · ' + sqlite_path}")

    try:
        sb = _sandbox_for(ds_id, sqlite_path)
    except Exception as e:  # noqa: BLE001
        st.error(f"数据源不可用：{e}")
        return

    # ---- 历史对话渲染 ----
    history = st.session_state.setdefault("biz_history", [])
    for idx, h in enumerate(history):
        with st.container(border=True):
            st.markdown(f"**🙋 {h['question']}**")
            _render_answer(h["answer"], cfg)
            # 反馈 + 收藏按钮
            c1, c2, c3 = st.columns([1, 1, 4])
            ans = h["answer"]
            qid = ans.get("query_id", f"q_{idx}")
            with c1:
                if st.button("👍 有用", key=f"fb_up_{idx}"):
                    from sqlpa.business import storage
                    storage.insert_feedback(qid, user["username"], h["question"],
                                            ans.get("sql", ""), 1)
                    st.success("已记录，感谢反馈")
            with c2:
                if st.button("👎 有误", key=f"fb_dn_{idx}"):
                    from sqlpa.business import storage
                    storage.insert_feedback(qid, user["username"], h["question"],
                                            ans.get("sql", ""), -1)
                    st.warning("已记录，该坏例将进入评测集补充")
            with c3:
                if st.button("⭐ 收藏", key=f"fav_{idx}"):
                    from sqlpa.business import storage
                    storage.add_favorite(user["username"], h["question"][:30],
                                         h["question"], ans.get("sql", ""))
                    st.success("已收藏")

    # ---- 新问题输入 ----
    question = st.text_input("业务问题（支持追问，如：再按月份细分 / 只看SP州）",
                             value="", placeholder="例如：各个品类的GMV")
    run = st.button("🔍 取数", type="primary")

    if run and question.strip():
        from sqlpa.business.service import answer
        llm = _real_llm() if _has_key() else None
        if llm is None and ds.get("kind") != "sqlite":
            st.warning("离线模式（无 API Key）仅支持内置 SQLite 数据源。")
            return
        with st.spinner("多Agent 处理中…"):
            a = answer(question, cfg, sb, sqlite_path, llm, role=role,
                       history=history, username=user["username"])
        # 历史条目带上命中指标/SQL，供下一轮追问改写做指代消解
        history.append({"question": question, "answer": a,
                        "metric": a.get("metric", ""), "sql": a.get("sql", "")})
        st.rerun()

    if not history:
        st.caption("💡 试试：各个品类的GMV → 追问「再按月份细分」；"
                   "或问一个口径外的问题（如 每个卖家的销售额）体验自由查询分级放行。")

    # ---- 审计面板（SQLite，按当前用户过滤）----
    with st.expander("🗂 审计留痕（我的查询）"):
        from sqlpa.business import storage
        recs = storage.list_audit(username=user["username"], limit=50)
        if recs:
            st.dataframe([{k: str(v) for k, v in r.items()} for r in recs],
                         hide_index=True)
        else:
            st.write("暂无审计记录")

    # ---- HITL: 人工复核 ----
    with st.expander("👤 人工复核(HITL)"):
        from sqlpa.business.hitl import queue as hitl_queue, resolve
        q = hitl_queue()
        if not q:
            st.write("暂无待人工复核项")
        for ind, rec in enumerate(q):
            st.markdown(f"**{ind+1}. {rec['user_input']}**  `{rec['matched_metric'] or '—'}`")
            st.caption(f"原因: {rec['reject_reason']}")
            if rec.get("generated_sql"):
                st.code(rec["generated_sql"], language="sql")
            if st.button("✅ 采纳", key=f"acc_{rec['record_id']}"):
                resolve(rec["record_id"], "approved", "人工采纳")
                st.success("已采纳")
                st.rerun()
            if st.button("✖ 驳回", key=f"rej_{rec['record_id']}"):
                resolve(rec["record_id"], "dismissed", "人工驳回")
                st.warning("已驳回")
                st.rerun()


# ============================================================================
# ⚙️ 指标中心（指标/维度/别名 可视化管理）
# ============================================================================

def _reload_cfg() -> None:
    """配置被修改后清缓存，让取数页用上新口径。"""
    _business_cfg.clear()


def _metric_center() -> None:
    st.subheader("⚙️ 指标中心（语义层管理）")
    st.caption("指标的公式、来源、支持维度全部配置化——LLM 只能选指标，改不了公式。"
               "此处的修改即时生效于业务取数。")
    from sqlpa.business import metric_store as store

    cfg = _business_cfg()
    tab_m, tab_d, tab_a = st.tabs(["📊 指标", "🧭 维度", "🔤 业务别名"])

    # ---------------- 指标 ----------------
    with tab_m:
        rows = [{"key": m.key, "名称": m.name, "公式": m.metric_expr,
                 "支持维度": ",".join(m.support_dims),
                 "支持过滤": ",".join(m.support_filters)} for m in cfg.metrics.values()]
        st.dataframe(rows, hide_index=True, use_container_width=True)

        opts = ["➕ 新建指标"] + list(cfg.metrics.keys())
        sel = st.selectbox("选择要编辑的指标", opts, key="metric_edit_sel")
        editing = None if sel == "➕ 新建指标" else cfg.metrics[sel]

        with st.form("metric_form"):
            c1, c2 = st.columns(2)
            key = c1.text_input("key（小写字母/数字/下划线）",
                                value=editing.key if editing else "",
                                disabled=editing is not None)
            name = c2.text_input("名称", value=editing.name if editing else "")
            desc = st.text_input("口径说明", value=editing.desc if editing else "")
            metric_expr = st.text_input("计算公式（口径核心，LLM 不可篡改）",
                                        value=editing.metric_expr if editing else "")
            from_clause = st.text_area("数据来源 from_clause（FROM ... JOIN ...）",
                                       value=editing.from_clause if editing else "", height=90)
            where_core = st.text_input("基础过滤 where_core",
                                       value=editing.where_core if editing else "1=1")
            all_dims = list(cfg.dimensions.keys())
            all_fts = list(cfg.filter_templates.keys())
            c3, c4 = st.columns(2)
            sd = c3.multiselect("支持维度", all_dims,
                                default=editing.support_dims if editing else [])
            sf = c4.multiselect("支持过滤", all_fts,
                                default=editing.support_filters if editing else [])
            sub = st.form_submit_button("💾 保存指标", type="primary")
        if sub:
            ok, msg = store.upsert_metric(
                {"key": key, "name": name, "desc": desc, "metric_expr": metric_expr,
                 "from_clause": from_clause, "where_core": where_core,
                 "support_dims": sd, "support_filters": sf},
                editing_key=editing.key if editing else "")
            (st.success if ok else st.error)(msg)
            if ok:
                _reload_cfg()
                st.rerun()
        if editing is not None:
            if st.button(f"🗑️ 删除指标 {editing.key}", key="del_metric"):
                ok, msg = store.delete_metric(editing.key)
                (st.success if ok else st.error)(msg)
                if ok:
                    _reload_cfg()
                    st.rerun()

    # ---------------- 维度 ----------------
    with tab_d:
        st.dataframe([{"key": d.key, "名称": d.name, "SQL片段": d.sql_fragment}
                      for d in cfg.dimensions.values()],
                     hide_index=True, use_container_width=True)
        with st.form("dim_form"):
            c1, c2 = st.columns(2)
            dkey = c1.text_input("维度 key", key="dim_key")
            dname = c2.text_input("维度名称", key="dim_name")
            dfrag = st.text_input("SQL 片段（如 c.customer_state）", key="dim_frag")
            dsub = st.form_submit_button("💾 新增维度")
        if dsub:
            ok, msg = store.upsert_dimension({"key": dkey, "name": dname,
                                              "sql_fragment": dfrag})
            (st.success if ok else st.error)(msg)
            if ok:
                _reload_cfg()
                st.rerun()
        del_d = st.selectbox("删除维度", list(cfg.dimensions.keys()), key="dim_del_sel")
        if st.button("🗑️ 删除所选维度", key="del_dim"):
            ok, msg = store.delete_dimension(del_d)
            (st.success if ok else st.error)(msg)
            if ok:
                _reload_cfg()
                st.rerun()

    # ---------------- 别名 ----------------
    with tab_a:
        st.dataframe([{"别名": k, "表": v.get("table", ""), "列": v.get("col", "")}
                      for k, v in cfg.alias.items()],
                     hide_index=True, use_container_width=True)
        with st.form("alias_form"):
            c1, c2, c3 = st.columns(3)
            aname = c1.text_input("中文别名", key="alias_name")
            atable = c2.text_input("对应表", key="alias_table")
            acol = c3.text_input("对应列（可空）", key="alias_col")
            asub = st.form_submit_button("💾 保存别名")
        if asub:
            ok, msg = store.upsert_alias(aname, atable, acol)
            (st.success if ok else st.error)(msg)
            if ok:
                _reload_cfg()
                st.rerun()
        del_a = st.selectbox("删除别名", list(cfg.alias.keys()), key="alias_del_sel")
        if st.button("🗑️ 删除所选别名", key="del_alias"):
            ok, msg = store.delete_alias(del_a)
            (st.success if ok else st.error)(msg)
            if ok:
                _reload_cfg()
                st.rerun()


# ============================================================================
# 🗄️ 数据源管理
# ============================================================================

def _datasource_page() -> None:
    st.subheader("🗄️ 数据源管理")
    st.caption("业务库连接注册：内置 SQLite（Olist）开箱即用；可注册 MySQL / PostgreSQL，"
               "沙箱的语句级安全校验与只读会话对所有数据源一致生效。")
    from sqlpa.business import datasources as dss

    for ds in dss.list_datasources():
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"**{ds['name']}**  \n`{dss.display(ds)}`")
            if ds.get("builtin"):
                c2.caption("内置")
            else:
                if c2.button("🗑️ 删除", key=f"del_ds_{ds['id']}"):
                    dss.delete_datasource(ds["id"])
                    st.rerun()
            if ds["kind"] != "sqlite":
                if st.button("🔌 测试连接", key=f"ping_ds_{ds['id']}"):
                    from sqlpa.sandbox.dialects import make_connector
                    spec = {k: v for k, v in ds.items() if k not in ("id", "name", "builtin")}
                    try:
                        ok, msg = make_connector(spec).ping()
                        (st.success if ok else st.error)(msg)
                    except Exception as e:  # noqa: BLE001
                        st.error(f"连接失败：{e}")

    st.divider()
    st.markdown("**➕ 注册新数据源**")
    with st.form("ds_form"):
        kind = st.selectbox("类型", ["mysql", "postgres"])
        c1, c2 = st.columns(2)
        name = c1.text_input("名称", value=f"我的{kind.upper()}业务库")
        host = c2.text_input("主机", value="127.0.0.1")
        c3, c4, c5 = st.columns(3)
        port = c3.number_input("端口", min_value=1, max_value=65535,
                               value=3306 if kind == "mysql" else 5432)
        user = c4.text_input("用户名")
        password = c5.text_input("密码", type="password")
        database = st.text_input("数据库名")
        schema = st.text_input("Schema（仅 PostgreSQL）", value="public",
                               disabled=(kind != "postgres"))
        sub = st.form_submit_button("💾 注册", type="primary")
    if sub:
        if not (host and user and database):
            st.error("主机/用户名/数据库名不能为空")
        else:
            dss.add_datasource({"kind": kind, "name": name, "host": host,
                                "port": int(port), "user": user,
                                "password": password, "database": database,
                                "schema": schema})
            st.success("已注册。请先「测试连接」确认可用，再在业务取数页切换数据源。")
            st.rerun()


# ============================================================================
# 🧪 引擎演示
# ============================================================================

# Spider 数据根目录：默认仓库内 data/spider（已 gitignore）。
# 可用环境变量 SQLPA_SPIDER_ROOT 指向本地下载的 Spider 数据，避免写死机器路径。
SPIDER_DB_ROOT = Path(os.environ.get("SQLPA_SPIDER_ROOT") or (ROOT / "data" / "spider"))


@st.cache_resource
def _spider_data():
    """加载 Spider dev 真实基准,按库分组 {db_id: {db_path, questions[]}}。"""
    import json
    dev = SPIDER_DB_ROOT / "dev.json"
    if not dev.exists():
        return None
    data = json.load(open(dev, encoding="utf-8"))
    out = {}
    for rec in data:
        db = rec["db_id"]
        db_path = SPIDER_DB_ROOT / "database" / db / f"{db}.sqlite"
        if not db_path.exists():
            continue
        out.setdefault(db, {"db_path": str(db_path), "questions": []})["questions"].append(
            {"question": rec["question"], "gold_sql": rec["query"], "db_id": db})
    return out or None


@st.cache_resource
def _spider_sandbox(db_path: str):
    from sqlpa.sandbox.sql_executor import SqlSandbox, ExecConfig
    return SqlSandbox(db_path, ExecConfig.from_settings(max_rows=2000))


def _agent_roles() -> None:
    """多Agent角色速览卡片：谁 · 干什么 · 是否LLM。"""
    agents = [
        ("hub", "SupervisorAgent", "调度中枢", "按路由派发任务、设护栏上限、触发终止", "确定性"),
        ("route", "RouterAgent", "难度路由", "判定问题走简单/复杂(跨表/聚合)", "启发式+LLM"),
        ("code", "SQLWriterAgent", "写手·生成者", "从问题+schema生成SQL(业务模式受公式约束)", "LLM"),
        ("fact_check", "ReviewAgent", "评审者·Critic", "独立审SQL(语法/语义/口径)，给意见让写手修订", "LLM"),
        ("health_and_safety", "DiagnoseAgent", "诊断者", "SQL报错时定位根因、给修复方向", "LLM"),
        ("verified", "ValidatorAgent", "校验者", "执行结果vs金标准/语义一致性", "LLM(部分)"),
        ("terminal", "ExecutorAgent", "执行工具", "只读沙箱安全执行SQL(SQLite)", "工具,非LLM"),
    ]
    st.markdown("**🤖 多Agent角色（谁 · 干什么）**")
    half = 4
    for start in (0, half):
        cols = st.columns(4 if start == 0 else 3)
        for col, (icon, name, role, desc, llm) in zip(cols, agents[start:start + half]):
            with col:
                with st.container(border=True):
                    st.markdown(f":material/{icon}: **{name}**\n\n**{role}**  ·  {llm}\n\n{desc}")


def _benchmark_results() -> None:
    """展示基准消融数值（读自 data/benchmark_results.json）。

    注意：该文件是**手动维护的展示常量**，没有脚本生成、也没有逐题运行产物，
    因此不得作为"实测准确率"呈现。
    """
    import json
    p = ROOT / "data" / "benchmark_results.json"
    if not p.exists():
        return
    d = json.load(open(p, encoding="utf-8"))
    st.markdown("### 📊 消融数值（展示用，非可复现实测）")
    st.warning("以下数值来自 `data/benchmark_results.json` —— 这是一个**手动维护的常量文件**，"
               "无生成脚本、无逐题运行产物，且对同一配置存在互相矛盾的两组数。"
               "**请勿作为准确率引用**；跑真实数字请用 `run_eval.py`。详见 README「评测框架」。")
    st.caption(d.get("source", ""))
    for key, label in [("self_heal", "自愈深度消融（修复几轮最好）"),
                       ("critic", "多Agent协作消融（Writer ↔ Critic 评审者）")]:
        blk = d.get(key)
        if not blk:
            continue
        st.markdown(f"**{label}**")
        st.caption(blk["note"])
        data = [{"配置": r["label"],
                 "总 EX": f"{r['ex']:.2f}",
                 "simple": f"{r['simple']:.2f}(n={r['simple_n']})",
                 "complex": f"{r['complex']:.2f}(n={r['complex_n']})"}
                for r in blk["rows"]]
        st.dataframe(data, hide_index=True)
        st.caption("💡 " + blk["conclusion"])


def _cost_panel() -> None:
    """成本/token/延迟看板：读 LLM 实例的累计用量与成本；上次查询延迟取自 session_state。"""
    st.markdown("**📈 成本 / 延迟**")
    if _has_key():
        try:
            s = _real_llm().stats()
            c1, c2, c3 = st.columns(3)
            c1.metric("Token(累计)", f"{s['usage']['total_tokens']:,}")
            c2.metric("估算成本", f"${s['cost']:.3f}")
            last = st.session_state.get("last_latency_ms", "-")
            c3.metric("上次查询延迟", f"{last}" if last == "-" else f"{last} ms")
        except Exception as e:  # noqa: BLE001
            st.caption(f"统计不可用：{e}")
    else:
        st.caption("Mock 模式：未统计成本/token（无真实 LLM 调用）")


# ============================================================================
# 📜 查询历史 & ⭐ 收藏夹
# ============================================================================

def _history_page() -> None:
    st.subheader("📜 查询历史")
    user = st.session_state.user
    from sqlpa.business import storage
    recs = storage.list_audit(username=user["username"], limit=200)
    if not recs:
        st.info("暂无查询记录，去「业务取数」问个问题吧。")
        return
    st.caption(f"共 {len(recs)} 条记录（最近 200 条）")
    for r in recs:
        with st.container(border=True):
            tag = "🛡️ 认证" if r.get("certified") else "🔓 自由"
            st.markdown(f"**{r['user_input']}**  `{tag}`  ·  {r['created_at']}")
            if r.get("generated_sql"):
                with st.expander("SQL", expanded=False):
                    st.code(r["generated_sql"], language="sql")
            st.caption(f"指标: {r.get('matched_metric') or '—'}  |  "
                       f"结果: {'✅ ' + str(r.get('result_rows', 0)) + ' 行' if r.get('is_success') else '❌ ' + (r.get('reject_reason') or '失败')}")


def _favorites_page() -> None:
    st.subheader("⭐ 收藏夹")
    user = st.session_state.user
    from sqlpa.business import storage
    favs = storage.list_favorites(user["username"])
    if not favs:
        st.info("暂无收藏，在「业务取数」回答下方点 ⭐ 即可收藏。")
        return
    for f in favs:
        with st.container(border=True):
            col1, col2 = st.columns([8, 1])
            with col1:
                st.markdown(f"**{f['title']}**")
                st.caption(f"问题：{f['user_input']}  ·  {f['created_at']}")
                if f.get("generated_sql"):
                    with st.expander("SQL", expanded=False):
                        st.code(f["generated_sql"], language="sql")
            with col2:
                if st.button("🗑️", key=f"delfav_{f['id']}"):
                    storage.delete_favorite(f["id"], user["username"])
                    st.rerun()


def _engine_mode() -> None:
    st.subheader("🧪 引擎评测（多Agent · 真实 LLM 生成 vs 金标准 · EX/EM 对比）")
    _benchmark_results()
    _cost_panel()
    st.divider()
    _agent_roles()
    st.divider()
    from sqlpa.eval.metrics import execution_match, gold_match, em_match
    from sqlpa.graph.pipeline import run_question
    from sqlpa.data.schema_extractor import extract_from_sqlite

    spider = _spider_data()
    if not spider:
        st.error("未找到 Spider 数据(D:\\ds harness\\spider)，请先下载/确认路径。")
        return
    with st.sidebar:
        max_repair = st.slider("最大自愈轮次", 1, 5, 3)

    db_id = st.selectbox("Spider 库", list(spider.keys()))
    qs = spider[db_id]["questions"]
    db_path = spider[db_id]["db_path"]

    q_text = st.selectbox("题目（每条带金标准）", [q["question"] for q in qs])
    gold = next(q["gold_sql"] for q in qs if q["question"] == q_text)
    st.markdown("**金标准 SQL（默认答案）**")
    st.code(gold, language="sql")

    if st.button("🤖 用真实 LLM 生成并对比", type="primary"):
        if not _has_key():
            st.warning("未配置 API Key，无法用 LLM 生成。请在 .env 填 LLM_API_KEY 后重试。")
            st.stop()
        sb = _spider_sandbox(db_path)
        schema = extract_from_sqlite(db_path, qs[0]["db_id"]).to_dict()
        with st.spinner("LLM 生成中…"):
            res = run_question(q_text, qs[0]["db_id"], schema, sb, _real_llm(),
                               gold_sql=gold, max_repair_round=max_repair)
        st.session_state["last_latency_ms"] = int(sum(a["ms"] for a in res.agent_trace))
        gold_exec = sb.execute(gold)
        llm_rows = res.exec_result.get("rows") or []
        # 金标准执行失败时不可用"空 vs 空"判为一致（与 runner 同一口径）
        matched, _reason = gold_match(gold_exec.rows, bool(gold_exec.ok), llm_rows)
        ex = bool(res.final_valid) and matched
        em = em_match(gold, res.final_sql)
        if not gold_exec.ok:
            st.warning("⚠️ 本题金标准 SQL 在沙箱中执行失败，EX 不可判定（已计为不符）。"
                       f"原因：{gold_exec.error or gold_exec.reason}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("EX (执行一致)", f"{ex}")
        c2.metric("EM (逐字一致)", f"{em}")
        c3.metric("路由", res.route)
        c4.metric("修复次数", f"{res.repairs} (尝试 {res.attempts})")

        st.markdown("#### 🧩 多Agent编排（谁 · 干了什么 · 耗时）")
        if res.agent_trace:
            st.dataframe(
                [{"#": i + 1, "Agent": a["agent"], "角色": a["role"],
                  "做了什么": a["detail"], "耗时": f"{a['ms']}ms"}
                 for i, a in enumerate(res.agent_trace)],
                hide_index=True)
        else:
            st.write("(无 agent_trace)")

        st.markdown("**🤖 LLM 生成 SQL**")
        st.code(res.final_sql or "(空)", language="sql")
        st.markdown("**执行结果**")
        if res.exec_result.get("ok"):
            cols = res.exec_result.get("columns") or []
            rows = res.exec_result.get("rows") or []
            st.dataframe([dict(zip(cols, r)) for r in rows] if cols else rows, hide_index=True)
        else:
            st.error(f"执行失败: {res.exec_result.get('error')}")
        with st.expander("🗂 链路 Trace"):
            for t in res.trace:
                st.write(f"- {t}")


# ============================================================================

st.set_page_config(page_title="多智能体 Text-to-SQL 智能取数产品",
                   page_icon="🧭", layout="wide",
                   initial_sidebar_state="expanded")

# ---- 真实登录（角色绑定）----
from sqlpa.business import storage as _storage
_storage.ensure_default_users()

if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    st.title("🔐 登录")
    st.caption("演示账号：admin / admin123（管理员）  ｜  analyst / analyst123（分析师）")
    with st.form("login_form"):
        u = st.text_input("用户名")
        p = st.text_input("密码", type="password")
        sub = st.form_submit_button("登录", type="primary")
    if sub:
        role = _storage.verify_user(u, p)
        if role:
            st.session_state.user = {"username": u, "role": role}
            st.success(f"欢迎，{u}（{role}）")
            st.rerun()
        else:
            st.error("用户名或密码错误")
    st.stop()

# 已登录：侧边栏显示用户信息 + 页面导航
with st.sidebar:
    st.markdown(f"👤 **{st.session_state.user['username']}** "
                f"(`{st.session_state.user['role']}`)")
    if st.button("🚪 退出登录"):
        st.session_state.user = None
        st.rerun()

page = st.sidebar.radio("页面",
    ["💼 业务取数", "📜 查询历史", "⭐ 收藏夹", "⚙️ 指标中心", "🗄️ 数据源管理", "🧪 引擎演示"])

if page.startswith("💼"):
    _business_mode()
elif page.startswith("📜"):
    _history_page()
elif page.startswith("⭐"):
    _favorites_page()
elif page.startswith("⚙️"):
    _metric_center()
elif page.startswith("🗄️"):
    _datasource_page()
else:
    _engine_mode()
