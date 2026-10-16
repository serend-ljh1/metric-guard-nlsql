# 诚实说明（局限与边界）

> 本文件从 [README](../README.md) 拆出：把「诚实声明 / 局限与边界」集中在一处，避免 README 头重脚轻。
> 未实现项一律如实列出，不虚构为已实现。

- **当前唯一的 LLM 触点**是"意图识别"（自然语言 → QuerySpec）与"结论/归因讲解"。
  RouterAgent 与 MetricMatcher 是**同一次语义匹配的两次上报**，Executor/Decision 是确定性函数——
  所谓"六 Agent"是**编排**上的六个阶段，不是六个独立智能体，README 不把它当作"多智能体更强"的证据。
- **归因做了守恒自检，并有统计门控（Welch z 检验）抑制假阳性**：单期环比仍是主要形态（已补日历口径提示：日均波动 + 天数可解释比例），
  阈值 5% 与门控 α 均为经验值、未在真实波动分布上标定，因此**假阳性率仍未被精确度量**——这是下一步最该补的评测。
- **比率/均值类指标不做"占波动 X%"**：分段 delta 之和在该类指标上数学不成立（AOV 按州可差到 80%），
  系统会自动降级为分段对照并说明原因。要给出真正的贡献度需要 rate/mix 分解（已实现 rate/mix 量价分解，见测试）。
- **评价类指标的口径已显式化**：Olist 的 `reviews` 表有 802 个重复 `review_id`（100,000 行 / 99,173 个唯一），
  因此"评价数/好评率"采用**去重口径**（`COUNT(DISTINCT review_id)`）并在配置 `desc` 里写明；
  `avg_review` 不支持 `category` 维度（1:多 JOIN 下 AVG 无法去重，需编译器支持子查询聚合）——现已用订单粒度预聚合派生表解决。
- **数据层**：依赖真实 Olist `data/olist/olist.db`；`data/` 下产物（审计 JSONL、HITL、报告、订阅）均为运行期生成、不进版本库。
- **长时记忆 / Checkpoint 未实现**。`config/settings.yaml` 只保留被代码真实读取的键——需要某能力时请先实现再接配置。
- **外部库（MySQL/PostgreSQL）**：已实现语句级安全校验、会话级只读（MySQL 显式关闭 autocommit + 只读事务；
  PG 连接参数 `default_transaction_read_only=on`）与查询级超时（`MAX_EXECUTION_TIME` / `statement_timeout`），
  并有单测锁住语句序列；但**真实库连通性仍需在装有驱动与目标库的环境验证**。
  另外行数上限是客户端 `fetchmany` 截断（服务端仍会扫完），所以超时护栏不可省。
- **审计**：SQLite 为**权威 sink**（append-only 触发器，禁止 DELETE/UPDATE），JSONL 仅作兼容镜像；
  `/api/audit` 只读 SQLite 并支持分页。权威写入失败会记日志与计数（`audit_write_failures()`，`/health` 可见）；
  镜像失败只影响镜像（`audit_mirror_failures()`），不再出现"两个 sink 各说各话"。
- **前端已在 CI 做 lint / 单测 / 构建校验**：`web/`（Vue3 + ECharts，源码已入库）由 CI 的 `web` job 执行
  `npm ci` + `npm run lint` + `npm run test` + `npm run build`；**登录已可从前端使用**（登录条 + 所有请求带 `X-API-Token`，
  含 SSE 流式与 Word 文件流下载）。**查询历史 / 收藏 / 反馈已接入前端「我的工作台」页签**
  （`/api/history`、`/api/favorites`、`/api/feedback`）；反馈尚**不能**由管理员一键导出为评测集（库层有
  `storage.export_badcases`，未加端点）。
- **数据源密码**使用 XOR+Base64 **可逆编码**（密钥取自环境变量或本机生成文件，**仅旧数据解密时回退**到源码内兜底值）存储于 `data/datasources.yaml`（已 gitignore）；仍是编码而非加密，生产部署应改用密钥管理服务（KMS）。
- **用户密码**使用 **PBKDF2-HMAC-SHA256（200k 迭代）+ 每用户随机盐 + 常量时间比较**存储于本地 SQLite（`data/app.db`，已 gitignore）；
  旧格式（全局固定盐 SHA-256）在登录时透明升级。**不再预置 `admin/admin123`**——
  需要账号请显式设置 `SQLPA_BOOTSTRAP_ADMIN_USER/PASSWORD`，否则不创建任何默认账号（`create_user()` 可建）。
- **登录会话**：`POST /api/auth/login` 校验口令后签发 **随机 token**（`sessions` 表，含归属人与到期时间，默认 12h，
  `SQLPA_LOGIN_TTL` 可调），`POST /api/auth/logout` 立即撤销，`GET /api/auth/me` 返回当前身份。
  作用是让**审计与治理动作记到具体的人**，并用 per-username 的登录限速挡撞库。
  仍未实现：口令复杂度策略、失败锁定、找回口令、多设备会话管理、MFA —— 生产应接入企业 SSO/IdP。
- **API 鉴权**：`/api/query`、`/api/analyze*`、`/api/audit`、`/api/datasources` 的角色**只认凭据、不认请求体**。
  凭据有两个来源，优先级从高到低：**登录会话**（`sessions` 表）→ 静态 `SQLPA_API_TOKENS="tokenA:admin,tokenB:analyst"`；
  配了静态 token 后必须带 `X-API-Token`；两者都没配时（本地/演示）按最小权限角色 `SQLPA_DEFAULT_ROLE`（默认 analyst）执行，
  `/api/audit` 对非 admin 脱敏。**生产部署务必配置 token 或接入企业鉴权**。
- **部署边界（重要）**：`start_api*.bat` 与 `docker-compose.yml` 均以**单 worker** 启动。
  SQLite 连接已开 **WAL 日志模式 + busy_timeout**（`storage._conn()`，可用 `SQLPA_SQLITE_JOURNAL_MODE` /
  `SQLPA_SQLITE_BUSY_TIMEOUT_MS` 覆盖），会话记忆（`session_memory`）与限速计数（`rate_limit`）也已落 SQLite。
  因此把 `--workers` 调大、各 worker 指向**同一个 `data/app.db`** 时，并发读写可用、状态一致、单进程重启可续。
  但仍**不建议**靠"多机共享同一 DB 文件"横向扩容（文件锁语义在跨机场景不可靠），跨机应改用 Redis 等共享存储。
- **HITL SLA / 超时升级**：工单登记即带 `due_at`（`SQLPA_HITL_SLA_HOURS`，默认 24h）；`hitl.sweep()` 巡检
  「超期未闭环 → 置 escalated 并写流转历史」「临期（不足 1h 或 SLA 的 1/4）→ 催办一次」，
  幂等（同一动作不重复触发）。入口：`tools/run_hitl_sla.py`（cron/Task Scheduler）+ `POST /api/hitl/sweep`（admin）
  + `GET /api/hitl/escalations`（看板）。投递：升级/催办结果除写本地 outbox 外，会 **POST 到 `SQLPA_HITL_WEBHOOK`**
  （未配置则显式返回"未投递"，不静默成功；投递失败被捕获并记入巡检 summary，不影响巡检本身）。
  **仍无邮件/SMS 通道**（需部署侧 SMTP/短信凭据与配额），也没有优先级重排与自动改派。
- **CORS 与限速可环境变量化**：`SQLPA_CORS_ORIGINS` 配置允许来源（默认本地前端），
  `SQLPA_RATE_LIMIT_PER_MIN` 控制端点限速（<=0 关闭）；限速计数已落 SQLite（`rate_limit` 表），重启不清零。
  默认值是本地/演示友好配置，生产请显式覆盖。
- **行级权限是应用层实现（RLS-lite）**：谓词注入编译后的 WHERE，可见、可测、与口径一起进审计；
  但它**不是数据库原生 RLS** —— 绕过应用直连数据库即失效。生产应叠加数据库视图/RLS 策略或独立只读账号。
- **指标血缘是表/列级静态解析**（来自 `from_clause`/`join_clause`/表达式），不是数据库字段级血缘，
  也不含上游加工链路与字段级影响分析。
- **统计门控是近似检验**：Welch z 检验假设日间独立、近似正态，对强趋势/强周期数据偏保守；
  它只用来**抑制**告警（减少假阳性），不用来"证明"波动存在。假阳性率本身仍未被精确度量
  （`evaluation/eval_alert_quality.py` 报的是告警量下降与成因结构，真 FP/FN 需独立标注集）。
- **README 评测表格与产物的一致性已机器校验**：`evaluation/check_readme_consistency.py`
  （由 `tests/test_readme_consistency.py` 纳入 CI）逐项比对 README 表格数字与 `evaluation/reports/*.json`，
  防止"重跑后数字漂移、文档停在旧值"。**告警假阳性率的独立标注集仍未提供**——精确 FP/FN 需人工标注真值，
  当前如实标注为**未做**，不虚构数字。
- **订阅告警无邮件/SMS 通道**：需要 SMTP/短信服务凭据与配额，属部署侧配置；仓库内提供的是
  `console / file / webhook` 三种通道 + cron 调度入口（`tools/run_subscriptions.py`），**内置调度器刻意不做**。
- **归因决策层有 32 条专家自标用例**（`evaluation/eval_decisions.py`）：26 条正例 + 6 条 BORDERLINE 判断题。
  **真值由实现者自标、非独立第三方**；离线（`llm=None`）命中 26/32=81.2%，`llm_value=+0.0%`（判断题 0/6 → 5/6），
  门控判据存在过拟合风险，**不要把它当普适准确率**。
- **外部基准只有一个库、且用的是 dev 集**：Spider `world_1`（3 张表）属 dev split，**建语义层时看过题面**，
  因此 25.0% 是开发集覆盖率、**不是泛化能力**；有效样本量 65 个不同 gold SQL，规模小。
  真正的 held-out 数字需要 Spider 未公开的 test gold（本机离线拿不到）。
- **语义层表达力的剩余边界**：`QuerySpec` 现有 `{指标 + 维度 + 过滤(等值/比较/区间/集合) + 组内筛选}`，
  仍表达不了 **EXISTS/INTERSECT（"两者都会"）、子查询比较（"大于任何亚洲国家"）、多度量、
  任意列投影 / argmax（取最大者返回实体名）**。遇到这些句式系统**显式拒答**（90/120 条拒答属此类），
  而不是"猜一个"。其中"投影/argmax"是**有意不做**：那会把系统变成通用查询构建器并稀释口径治理这条主线。
  目标形态覆盖率 58.3% 的剩余缺口主要来自这些形态。
- **外部基准只跑了确定性路径**（`llm=None`）：线上有 LLM 时的意图识别质量未在该评测中度量
  （LLM 路径另有口径确认门兜底）。
- **外部对抗语料是"按类别构造"而非官方语料文件**：61 条覆盖了已知类别，不等于穷举；
  只读误杀率 7.7%（1/13）说明策略层是文本粗筛，跨方言沙箱的等价性也未在真实 MySQL/PG 上验证。
