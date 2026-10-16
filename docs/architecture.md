# 架构与实现细节

> 本文件从 [README](../README.md) 拆出：README 保留「定位 / 系统架构图 / 评测汇总表 / 快速开始」，
> **目录结构、业务产品模式与治理面实现细节**集中在此。系统架构总览图与数据流见 README 的「系统架构」一节。

## 目录结构

```
sqlpa/
├── api.py                          # ★ FastAPI REST 后端装配层（/health + include 各 router） + SSE 流式分析端点
├── apiroutes/                      # ★ 路由层（由 api.py 装配，对外路径/签名不变）
│   ├── deps.py                     #   公共依赖：鉴权(登录会话+静态token)/角色/限速/会话记忆（持久化+并发保护）/配置/沙箱
│   ├── auth.py                     #   /api/auth/login /api/auth/logout /api/auth/me（登录会话）
│   ├── query.py                    #   /api/query /api/report /api/metrics
│   ├── analyze.py                  #   /api/analyze /api/analyze/stream /chart-template /report(文件流) /api/analyze/traces /api/analyze/trace/{id}(重放)
│   ├── governance.py               #   /api/governance/conflicts /api/audit /api/metrics/(history|lineage|rollback|upsert|dimensions|aliases 写) /api/datasources /api/llm-usage /api/storage-info
│   ├── hitl.py                     #   /api/hitl*（含 /escalations 与 /sweep） /api/confirmations*
│   ├── subscriptions.py            #   /api/subscriptions*
│   └── workspace.py                #   /api/history /api/favorites /api/feedback（个人工作台）
├── web/                            # ★ Vue3 + ECharts 前端（SSE 流式 Agent 面板 + 归因可视化 + 决策卡 + 治理看板 + 我的工作台 + DataLineage）
├── start_api.bat                   # ★ 一键启动后端（生产/演示，不热重载）
├── start_api_dev.bat               # ★ 开发模式后端（--reload 代码热重载）
├── start_web.bat                   # ★ 一键启动前端
├── run_business.py                 # ★ 业务模式 CLI：自然语言取数+口径说明+审计
├── demo_governance.py              #   治理面确定性演示（无需 Key）
├── config/settings.yaml            # 阈值/护栏/模式开关（改规则不改代码）
├── config/semantic_world.yaml      # ★ 第二个业务域语义层（Spider world_1）：换域只改 YAML，0 行业务代码
├── .env.example                    # LLM Key / 端点模板
├── Dockerfile / docker-compose.yml # 一键容器化（api + web）
├── .github/workflows/ci.yml        # GitHub Actions：push/PR 自动跑离线测试 + 全部离线评测线 + 覆盖率门槛 + 前端 lint/test/build
├── tools/
│   ├── build_olist_db.py           # Olist 真实数据导入 SQLite（数据窗口 2016-09~2018-10）
│   ├── build_olist_sample.py       # 从全量库按天抽样建样本库（2.2MB，供开箱即跑）
│   ├── fetch_external_corpus.py    # ★ 抓取官方注入语料（sqlmap/PayloadsAllTheThings），失败拒绝编造
│   ├── verify_real_llm.py          # 真实 LLM 联调冒烟（在 Olist 上走完整六 Agent 分析链）
│   ├── run_subscriptions.py        # ★ 订阅告警调度入口（cron 调用：检查 + 投递，失败返回非零）
│   └── download_data.py            # 数据下载辅助（按需）
├── src/sqlpa/
│   ├── analysis/orchestrator.py    # ★★★ 分析主角链：LangGraph StateGraph 六 Agent 编排（归因下钻→诊断→决策）+ 编译缓存
│   ├── sandbox/sql_executor.py     # 只读加固沙箱（语句级安全校验，全方言一致）
│   ├── sandbox/dialects.py         # MySQL/PostgreSQL 连接器（会话级只读+schema提取）
│   ├── llm/{base,mock_llm,openai_compat}.py  # 可插拔 LLM（含单次超时+有限重试+总预算）
│   ├── data/schema_extractor.py    #   schema 提取（SQLite introspection）
│   ├── business/                   # 业务语义层
│   │   ├── business_config.yaml    #   指标/维度/过滤模板/别名（配置化口径）
│   │   ├── metric_config.py        #   加载 + 支持度校验 + 可配置关键词/取值域/算子白名单
│   │   ├── metric_matcher.py       #   MetricMatcher：LLM识别+关键词兜底+五道 fail-closed 判据闸
│   │   ├── metric_guard.py         #   公式篡改校验（结果列结构绑定）
│   │   ├── metric_store.py         #   指标中心 CRUD（带校验）
│   │   ├── compiler.py             #   ★ 语义层编译器：规格 → 确定性 SQL（算子白名单/HAVING）
│   │   ├── sqlgen.py               #   维度/过滤 JOIN 注入与派生表别名处理
│   │   ├── intent.py               #   ★ 追问语气词单一来源（has_followup_tone）
│   │   ├── followup.py             #   多轮追问上下文改写
│   │   ├── charting.py             #   结果自动图表推荐
│   │   ├── service.py              #   ★ 取数统一入口（口径内确定性编译 / 口径外明确拒绝）
│   │   ├── datasources.py          #   数据源注册中心（密码加密存储）
│   │   ├── permissions.py          #   表列权限 + PII 掩码
│   │   ├── governance.py           #   口径冲突检测（四类场景）+ 指标口径解释（确定性）+ 冲突 API
│   │   ├── attribution.py          #   异常归因拆解（通用 N 因子分摊 + 维度贡献，并行查询；失败信号 dim_errors）
│   │   ├── confirm.py              # ★ 口径确认门（共用实现：合法口径集合/落库确认/逐字还原 spec）
│   │   ├── audit.py / hitl.py      #   审计留痕 / HITL 工单（SQLite 权威 + 幂等 + 状态历史 + 重跑验证）
│   │   └── storage.py              #   SQLite 持久化（用户/审计/HITL/确认/版本/反馈/收藏）
├── tests/                          # pytest 套件（离线、无需Key、CI可复现）
│   ├── conftest.py                 #   公共 fixture（真实 Olist 切片样本库/迷你库/缺数据自动跳过）
│   ├── test_metric_correctness.py   # ★ 口径真值对照（fan-out、过滤 JOIN、支持度、ratio 同源、编辑不毁指标）
│   ├── test_query_spec_expressiveness.py # ★ QuerySpec 表达力（算子白名单·集合·区间·HAVING·否定反连接）
│   ├── test_external_semantic_layer.py # ★ 外部语料接入机制：可配置词表/取值域/词形别名/判据闸/不得跨长度换口径
│   ├── test_sandbox_external_corpus.py # ★ 外部对抗语料：危险 100% 拦截、只读误杀固定、库不变性、过滤值注入
│   ├── test_attribution_eval_truth.py # ★ 评测尺子独立于被测引擎（变异测试：假引擎自报命中不算命中）+ LLM 基线公平性
│   ├── test_hitl_loop.py            # ★ HITL 闭环：幂等/状态流转/重跑验证/确认记录单次有效/端点权限
│   ├── test_attribution_additivity.py # ★ 归因可加性降级 + 日历口径 + rate/mix 量价分解
│   ├── test_security_hardening.py   # ★ 审计鉴权/append-only/不静默丢、分析链权限与审计、SELECT * fail-closed、外部连接器护栏
│   ├── test_significance_gate.py    # ★ 统计门控（Welch z）+ 决策三态 alert/watch/normal
│   ├── test_observability.py        # ★ LLM 用量/成本/时延计量 + 审计单 sink
│   ├── test_credentials.py          # ★ PBKDF2 随机盐、无预置弱口令、数据源密钥不入源码
│   ├── test_metric_versions.py      # ★ 口径版本历史/回滚/血缘
│   ├── test_row_level_security.py   # ★ 行级权限（注入 + 失效即拒绝）
│   ├── test_subscriptions.py        # ★ 订阅告警：规则/门控/投递/API 权限
│   ├── test_analysis_orchestrator.py # ★ 分析主角链 / SSE / 会话记忆测试
│   ├── test_sandbox.py             #   沙箱安全 + 方言适配
│   ├── test_product.py             #   分级放行/多轮/图表/指标CRUD/数据源
│   ├── test_api.py                 #   REST API 接口
│   ├── test_attribution_drill.py   #   归因下钻 / 因子分解
│   ├── test_attribution_decisions.py # 归因决策(真Agent)评测回归
│   └── test_semantic_compiler.py   #   语义层编译器契约（编译确定性、全部可执行）
├── evaluation/                     # 可复核的离线评测（确定性、无需 Key）
│   ├── run_all.py                  # ★ 批量入口：一次跑完 7 条离线线 + 可选 --llm 对照，汇 overall.json
│   ├── eval_business.py            #   语义层命中/口径外拒绝/权限/掩码/审计
│   ├── eval_decisions.py           #   归因决策(真Agent) 26 场景 + 6 BORDERLINE 判断题
│   ├── eval_product.py             #   产品指标（命中率/降级率/认证率）
│   ├── eval_alert_quality.py       # ★ 告警质量：阈值告警 vs 阈值+显著性门控（假阳性治理）
│   ├── eval_external_bank.py       # ★ 外部公开基准（Spider dev）：覆盖率/覆盖内正确率/拒答归因
│   ├── eval_sandbox_external.py    # ★ 沙箱外部对抗语料：危险拦截率/只读误杀率/库不变性
│   ├── eval_attribution.py         # ★ 归因命中率（注入式真因，Ground Truth by Construction）
│   ├── eval_attribution_llm_baseline.py # ★ 纯 LLM 对照臂（同信息条件，防稻草人）
│   ├── spider_probe/               # ★ 仓库内 Spider 样本语料（题面+gold SQL+库文件，供离线复现）
│   ├── sandbox_corpus/sqli_corpus.jsonl # ★ 99 条显式来源对抗语料（76 破坏性 + 23 只读，含跨方言）
│   └── reports/                    #   评测产物（含 external_bank_world_1.md / sandbox_external.md）
└── data/                           # olist(真实公开数据，gitignored) / reports / audit
```

## 业务产品模式（把实验变成真实业务产品）

> 为避免"只是一个基准验证实验"，在业务语义层之上叠加了一层**产品层**。当前仓库只有一种运行模式：

- 💼 **业务产品模式**（`api.py` / `run_business.py` / `web/`）：开启业务语义层 + 护栏 + 审计，面向**业务人员自然语言取数**。
- 口径外问题的处理方式是**明确拒绝并给出可操作原因**，不降级生成未认证 SQL
  （自由 SQL 生成会让口径漂移、无法审计、错误伪装成合理数字）。

### ⚠️ 价值锚点：**这不是"教业务写 SQL"**
这个项目**不是**"把中文翻译成 SQL、帮不会写 SQL 的人写 SQL"（那是 NL-to-SQL 最容易被问倒的伪定位）。真正的价值是 **指标语义层 + 治理 + 长尾自助取数**：

- **核心**：把 GMV / 客单价 / 取消率等**业务口径配置化统一**（公式写在配置、由编译器确定性生成，并做**结果列结构绑定校验**），+ **权限白名单 / PII 掩码 / append-only 审计 / 口径治理 / 异常归因闭环**，让业务不用排队找数分也能拿到**口径一致**的数据。**安全底座（权限+掩码+只读沙箱+审计）已核验：表列权限粒度（含 `SELECT *` fail-closed）、PII 覆盖列、沙箱只读与多语句拦截、审计留痕与不可删改均已由离线测试锁定。**
- **服务的是"长尾 / 临时 / 探索式"取数**：看板报表覆盖**固定、高频**的指标；而"**临时想看某个维度异常、某组合**"这类**长尾**问题看板覆盖不到、找数分又排队长——**这才**是它的用武之地（**不是替代看板**，是补充看板覆盖不到的长尾，且因语义层而口径/权限可控）。
- 解决的问题：**"同一个指标，不同人、不同 SQL 算出不同口径"** 的真实痛点。
- **治理面增量**（区别于纯 Text-to-SQL）：**规则 + LLM 协同**，各自解决明确的问题——
  - **规则（确定性算术 / 白名单 / 状态机）**负责"**可复核、零漂移**"：公式校验、权限放行、HITL 闭环状态机、归因的维度拆解与阈值判定——这些一旦交给 LLM 就不可复核，所以用代码锁死；
  - **LLM 负责"**业务听得懂**"的语义增强**：意图识别、**归因总结**（把波动与主因翻成人话）、口径冲突的自然语言解释——这些没有唯一正确答案，交给 LLM 更有价值；
  - **解决了什么**："同一个指标，不同人、不同 SQL 算出不同口径"（规则锁口径）+ "这个波动从哪来、该不该告警、推给谁"（规则定位 + LLM 讲解），两者协同才构成可复盘的治理闭环。
  - **口径治理**：GovernanceAgent 自动检测"语义相近但公式不同"的指标冲突，并给出**可追溯的口径解释**（负责人/版本/公式），避免口径分歧无人发现。
  - **异常归因**：归因 Agent 按**乘法因子（精确、含交互项）+ 维度贡献度**拆解波动——是订单量跌了、客单价跌了，还是某区域/品类跌了；阈值**可配置**。查询按当期/上期**并行**执行降延迟。比率/均值类指标不做"占波动"表述（分段 delta 之和在该类指标上不成立，见守恒自检）。
  - **闭环（ClosureAgent MVP）**：异常自动写入 HITL 队列并**关联指标负责人**，状态机为 **待确认 → 处理中 → 已修复 → 已验证 / 误报**；处理状态可追踪，验证需重跑指标。
- **对标品类**：这是一类真实存在的产品 —— **dbt Semantic Layer / Cube / MetricFlow / Looker / AtScale**（"语义层 + 自助 + 治理"），以及近两年的 **AI-BI / text-to-dashboard 智能体**。**NL 生成 SQL 只是外壳，语义层 + 治理才是核心。**

### 两条分支：口径内确定性编译 / 口径外明确拒绝

- **口径内**（命中配置指标且组合受支持）→ 由 `compiler.compile_spec` 从配置**确定性编译** SQL
  → 结果列与配置表达式做**结构绑定校验** → 表列/行级权限 → 只读沙箱执行 → PII 掩码 → 审计。
  结果标记 **🛡️ 口径已认证**（`path=semantic`）。
- **口径外**（未命中指标 / 组合不受支持 / 句式超出表达力）→ **明确拒绝**，给出可操作原因
  （"不支持按品类看取消率""该句式超出语义层表达力"），并同样进审计以便统计"哪些问法还没被口径覆盖"。
  **不降级到自由 SQL 生成**——本项目不存在该链路，理由见 README「架构红线」。

### 安全底座状态机（PermissionAgent）

权限不是"开关"而是一条确定性的**状态机**，不依赖 LLM，可复核：

```
请求 → 校验(角色表列白名单 + 敏感列判定) → 放行 / 拦截 → 审计留痕
        └─ 拦截（含照常执行、仅掩码两态）→ 越权/敏感 → 入 HITL 转人工核验
```

- **放行两态**对同一请求分别判定：**表列越权**（限定列 / **非限定列** / **`AS` 别名改写** 均拦截，已核验 5/5）与 **PII 敏感列**（命中则掩码而非拦截，含别名改写，已核验 2/2）→ 前者入 HITL 让分析师修正，后者静默脱敏照常返回。
- 与 **同义词/别名**协同：MetricMatcher 靠同义词把"销售额/成交额/GMV"归一，才能正确触发权限白名单；没有别名归一，同一意思会被算成不同指标、绕开口径控制。
- 与 **闭环状态机**衔接：任何被权限 / 公式 / 引擎失败拦下或归因异常判定的边界情况，都汇入 HITL 队列（**待确认 → 处理中 → 已修复 → 已验证 / 误报**）并关联负责人，人工处理后才关闭。

### 多轮追问 + 自动图表

- **多轮追问**：有 LLM 时做指代消解/省略补全（question rewriting），无 LLM 时确定性启发式兜底；改写结果透传下游并记入审计。
  「追问语气词表」收敛为**单一来源** `src/sqlpa/business/intent.py`（`has_followup_tone`），路由与分析链共用，避免多处词表漂移。
- **自动图表**：按「列名 + 数据类型 + 行数」确定性推荐——1维度+1数值→柱状图（日期维度→折线）、2维度→分组柱状、单行单数值→指标卡。

### 指标中心（语义层可运营）

指标的公式、来源、支持维度全部配置化。**运营入口是库层 + API**：
`metric_store` 提供指标/维度/别名的增删改与校验（保存即时生效于业务取数），并经
`POST/DELETE /api/metrics/*`（写操作仅 admin）暴露；`GET /api/metrics` 可读。
**可视化增删改页面尚未实现**（前端现有分析演示页 / 治理复核看板 / 我的工作台三个页签）。

指标定义字段（最小闭环）：**指标名、同义词/别名、公式、口径说明、负责人、版本号、状态**。其中**同义词/别名**是 MetricMatcher 语义匹配与 GovernanceAgent 冲突检测的输入——没有别名，用户的"销售额 / 成交额 / GMV"会被当成不同指标或无法命中；版本号与负责人用于口径解释可追溯。

**防篡改校验是"结果列结构绑定"**（`metric_guard.verify_formula`）：要求"别名 = 指标 key 的那一列"
与配置表达式**逐字等价**，因此改系数、加诱饵列、把表达式藏进注释/字符串都会被拦（离线核验 5/5）。
仍非完备：它校验的是**词法等价**，不解析 SQL 语义。

### 数据源接入（MySQL / PostgreSQL）

- 沙箱的**语句级安全校验**（SELECT-only / 高危关键字拦截）对所有方言一致生效；
- 外部连接器另有**会话级只读双保险**（MySQL：关 autocommit + `SET SESSION TRANSACTION READ ONLY` + 显式只读事务；
  PostgreSQL：连接参数 `default_transaction_read_only=on` + `set_session(readonly=True)`）；
- schema 通过 information_schema 自动提取（含外键），注入 LLM 上下文时附带方言提示。
- 驱动惰性导入：仅用 SQLite 可不装 pymysql/psycopg2。

**业务数据底座：自带样本库（开箱即跑）+ 可选全量 Olist（~10万订单）**
```bash
# 默认就用仓库自带样本库：data/sample/olist_sample.db（2.2MB / 634 天）
# 需要全量口径时，下载 Olist CSV 放到 data/olist/ 后：
python tools/build_olist_db.py --src data/olist --out data/olist/olist.db

# 想重建样本库（从全量库按天抽样）：
python tools/build_olist_sample.py            # 默认每天抽 5 单
```
> **数据解析顺序**：`SQLPA_DB_PATH` > `data/olist/olist.db`（全量） > `data/sample/olist_sample.db`（样本）。
> 跑在样本库上时，接口/评测会打印明确提示并在报告里记 `db_kind=sample`。
>
> **评测对数据的要求**：`eval_business` / `eval_product` 在样本库上即可跑；
> `eval_attribution`（注入真因依赖分段规模）与 `eval_alert_quality` 的**README 数字来自全量库**——
> 前者在样本库上会明确拒绝运行，后者会打印"数值不代表全量口径结果"。
> 业务层用**真实公开数据 Olist**（2016-09~2018-10 窗口）。演示/评测提问请落在该窗口内，
> 如"2018-06 GMV 为什么比上月跌"；不要问"本月/最近30天"——相对今天的日期在窗口外必然无数据。

### 验收指标（衡量产品价值，而非 SQL 准确率）

| 指标 | 含义 |
|---|---|
| 口径争议下降率 | 引入指标中心后，同一指标口径不一致的反馈环比 |
| 取数等待时间 | 从提问到拿到认证结果的时间（口径内应秒级） |
| 异常发现→归因时间 | 从波动出现到归因定位根因维度的时间 |
| 闭环率 | 写入 HITL 的异常中被处理并验证关闭的比例 |
| 脱敏覆盖率 | 敏感列在所有访问路径（含别名改写）下都被掩码的比例 |

### 演示场景（用 Olist 演练治理面，而非演示 SQL 翻译）

1. **口径冲突**：故意配置两个语义相近但公式不同的指标（如"成交额"含运费 vs"GMV"不含），运行 `demo_governance.py` 看 GovernanceAgent 自动告警与负责人/版本追溯。
2. **异常下跌**：选 Olist 中订单量有波动的时段，演示归因 Agent 并行拆解"是订单量/客单价还是某品类/区域下跌"，并提示已推送负责人。
3. **敏感访问**：用 analyst 角色查询含 `customer_zip_code_prefix` 等敏感列，展示 PII 掩码与权限拦截（含别名改写场景）。

> **真实实测结果（`demo_governance.py`，Olist 全量库，可复现）**：
> - **口径冲突检测**：当前指标中心健康无冲突；临时注入近义指标「净成交额」(`SUM(oi.price)-COALESCE(SUM(r.refund),0)`) 后立即被 GovernanceAgent 告警，指认同 GMV 公式不一致，并带出负责人/版本；指标解释 GMV 返回公式 `SUM(oi.price)`、负责人「财务线」、版本 `v3`。
> - **异常归因（2018-03 vs 2018-02）**：GMV 当期 98.1 万 vs 上期 83.8 万，环比 **+17.1%**（超默认阈值）；并行拆解出主要贡献维度 —— **SP 州 +7.4 万（占波动 52%）**、**品类「手表/礼品 relogios_presentes」+3.5 万（占 24%）**；订单数 +6.8%，同样归因到 SP 州 +339 单（占 76%）。
> - **闭环**：异常自动写入 HITL 队列并关联负责人「财务线」，记录含各维度贡献占比，供后续"待确认→处理中→已修复→已验证/误报"状态流转。
>
> 复现：`PYTHONPATH=src python demo_governance.py`（无 LLM 依赖，纯确定性计算）。

### 不做清单（诚实边界，避免过度承诺）

- ❌ 不做通用 BI 替代（不抢 Tableau/看板的固定看报表位）
- ❌ 不做无治理的自由 SQL：**口径外一律明确拒绝**，不生成未认证结果让用户"自行判断"
- ❌ 不做全自动治理（口径冲突检测是确定性规则，归因是确定性算术，不自动改公式/自动修数据）
- ❌ **不把权限/PII 掩码后置**：安全底座是上线底线，外部未脱敏数据不给业务用
- ❌ 不做字段级血缘（当前只做"维度拆解"轻量归因，不追踪表/字段级血缘）
