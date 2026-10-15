# 迭代修复记录（带回归测试）

> 本文件从 [README](../README.md) 拆出：README 只保留一句"本轮修复"概述并指向此处。
> 以下各轮修复**均带回归测试**，相关测试见 `tests/test_metric_correctness.py`、
> `tests/test_attribution_additivity.py`、`tests/test_security_hardening.py` 等。

## 第一轮（2026-09，口径与安全加固）

> 1. **口径数字错了**：评价类指标的 `join_clause` 写死了 `LEFT JOIN order_items`，聚合却在 reviews 上 →
>    无条件 fan-out（真实库实测 `review_count` 114,100 → 真值 99,173；`avg_review` 3.9998 → 4.0709）。
>    现已只保留 1:1 JOIN、按需注入维度 JOIN，并加"编译值 vs 独立手写 SQL 真值"回归测试。
> 2. **比值类指标不再输出"占波动 X%"**：分段 delta 之和对比率/均值天然不等于总变化（AOV 按州可差到 80%），
>    归因层新增**守恒自检**，对不上就降级为分段对照并把原因写进依据。
> 3. **过滤条件也注入 JOIN**：此前 `gmv` 加 `state` 过滤会生成 `WHERE c.customer_state=...` 却没连 customers 表。
> 4. **编译期强制支持度**：`support_dims`/`support_filters` 不再只靠 matcher 把关（`aov+category` 曾被编出未认证结果）。
> 5. **ratio 同源校验加上行级口径**：`where_core` 不同直接拒绝（`ratio@gmv/order_count` 曾把 GMV 悄悄改成"仅已送达"）。
> 6. **指标中心不再毁指标**：`upsert_metric` 此前重建字段 dict 时丢掉 `join_clause`，保存一次 gmv 就永久不可用。
> 7. **日历假象可见**：月度环比给出日均口径与"天数可解释比例"，2 月 vs 3 月这类假波动不再无人提醒。
> 8. **安全加固**：`/api/audit`、`/api/datasources` 加鉴权与脱敏；审计表 append-only 且写失败不再静默；
>    分析链补齐 `check_access` + 审计留痕；`SELECT *` 权限/掩码改为 fail-closed；外部连接器补只读事务与查询级超时；
>    SSE 异常不再假超时；会话记忆按调用者隔离 + TTL/上限。
> 9. **工程一致性**：CI 修绿（原 Streamlit 冒烟步骤引用的 `app.py` 已下线）；`settings.yaml` 清掉死键。

## 第二轮（同日，继续把"边界"变成"能力"）

> 10. **审计单一权威 sink**：SQLite 是权威、JSONL 仅作镜像，`/api/audit` 只读 SQLite（消除双写分叉），并支持分页。
> 11. **可观测性**：每次取数/分析都记录 `llm_calls / total_tokens / cost / latency_ms`，
>     落入响应、审计表与 `/health`；同时修掉评测里"调用计数恒为 0"（读错字段 `calls`，实际只有 `usage`）。
> 12. **编译器支持派生表 JOIN**：`JOIN (SELECT ...) r` 可被正确识别/去重/合并；`avg_review` 因此改用
>     **订单粒度预聚合**，把 `category` 维度口径**修对后重新认证**（覆盖率仍 84.9%，但没有错的命中）。
> 13. **比率类指标的 rate/mix 量价分解**：`weight_metric` 声明分母 → 价格效应/结构效应/交互项，
>     并**先用权重指标重建总值做自校验**：重建对不上（如跨品类重复计数）就拒绝给分解。
> 14. **统计门控 + 告警质量度量**：告警需同时过"阈值 + 显著性检验（Welch z，零 token）"；
>     真实 Olist 上实测 **56 个阈值告警中 32 个（57%）属噪声被抑制**，决策分三态
>     `alert / watch / normal`（"证据不足"不再伪装成"正常"）。新评测 `evaluation/eval_alert_quality.py`。
> 15. **凭据加固**：口令改 **PBKDF2 随机盐 + 常量时间比较**（旧格式登录时透明升级）；
>     不再预置 `admin/admin123`（改为环境变量引导）；数据源密钥不再硬编码（env 或本机生成文件，仅旧数据解密时回退）。
> 16. **指标口径版本历史 / 血缘 / 回滚**：每次编辑记 before/after/actor（append-only），
>     `rollback_metric` 可撤销某次变更（创建则撤销为删除）；`metric_lineage` 给出表/列级静态血缘；
>     新增 `/api/metrics/history|lineage|rollback`（回滚需 admin）。
> 17. **行级权限（RLS-lite）**：按角色注入行过滤谓词；谓词引用的表不在该指标取数范围时**编译期拒绝**
>     （行级权限只能收紧、不能失效）；分析链与取数链同一套。
> 18. **订阅告警闭环**：`console / file / webhook` 三种投递通道 + `tools/run_subscriptions.py` 调度入口
>     （交给 cron）+ `/api/subscriptions` 增删查与立即检查；投递失败如实上报（cron 退出码非零）。

## 第三轮（外部证据轮）——把"自证"换成"别人的题、别人的答案"

> 19. **接入外部公开基准（Spider dev `world_1`，N=120）**：问题与 gold SQL 均由第三方数据集定义，
>     对错由 gold SQL 的执行结果裁定。新增 `config/semantic_world.yaml`（**换域只改 YAML**）+ 沙箱治理面。
>     详见 [`evaluation/reports/external_bank_world_1.md`](../evaluation/reports/external_bank_world_1.md)。
> 20. **这次评测真的改动了系统**：首轮 20/23 = 87% 正确、**3 条错答**，每条都指向一个机制缺口 ——
>     ① 拼写差异（`Carribean`≠`Caribbean`）导致**过滤静默丢失**、范围被放大成全世界的面积（数值对、范围错）→
>     新增**未知专有名词闸**；② 最值词形漏网（`shortest`）→ 答成另一个问题的答案 → 词形集合**写成域配置项**；
>     ③ "与均值比较"（`above the average`）条件被忽略 → 比较类词形入闸。
> 21. **沙箱外部对抗语料**：61 条显式来源语料（48 破坏性 + 13 只读），**危险拦截 48/48 = 100%、库 sha256 与行数一字未变**；
>     同时首次度量**只读误杀率 1/13 = 7.7%**（`SELECT 'DROP TABLE x' AS note` 这类字面量）并固定在测试里。
>     过程中修掉 3 个真实漏洞 —— 其中 `_BLOCKED_FUNCTIONS` **因大小写不匹配从未生效过**。
>     报告：`evaluation/reports/sandbox_external.md`。
> 22. **语义层消歧不得换口径**：为支持"事实表消歧"一度过度生效，把 Olist 的 `每单件数按订单状态`
>     从"正确拒答"变成静默改答成"商品件数按订单状态"（用户问 A 得 B，而"指标识别准确率"仍是 100%）。
>     现限制**仅命中词同长的真并列候选**之间可消歧（`tests/test_external_semantic_layer.py` 守住）。

## 第四轮（表达力轮）——把"拒答"变成"能答"，而不是放宽判据

> 23. **`QuerySpec` 扩展**：过滤取值支持**集合（IN）/ 显式算子（lt/gt/gte/lte/ne/between/nin）**，
>     并新增 `having`（组内筛选）。规格里只出现**算子名**，SQL 记号必须来自配置白名单
>     （`filter_ops` / `metric_ops`）—— 算子与取值一样是注入面，实测把 op 写成 `"'; DROP TABLE singer; --"` 被编译期拒绝。
> 24. **否定形态（反连接）**：`negated_filters` 把"不使用英语的国家总人口"映射到配置写的
>     自包含 `NOT IN (SELECT ...)` 过滤类型；子查询模板里的别名不再被误判为"需要注入 JOIN"。
>     若某否定问法没有登记对应形态 → **照旧拒答**（不猜、绝不按正面版本执行）。
> 25. **守卫与表达力联动**：原来"否定/比较/年份区间一律拒答"的核心守卫改为
>     **条件触发——线索没有被成功表达成过滤条件时才拒答**；表达不了时仍 fail-closed。
> 26. **多取值语义显式化**：`continent IN (Asia, Europe)` 是"或"（同一行只有一个取值）→ 可表达；
>     `language` 的多取值是"两者都会"（与语义）→ 配置声明 `multi: and` → 拒答，避免答成"会其中一种"。
>     跨指标的组内筛选（"每个政体的总人口，其中平均寿命 > 72"）同样明确拒答。
> 27. **外部基准覆盖率 17.5% → 25.0%**（目标形态 **43.8% → 58.3%**），**错答仍为 0**；
>     过程中又修掉 3 个"扩展引入的新缺陷"（`multi` 声明被解析器吃掉、`at least` 被当最值词误拦、
>     `at least 3` 映射成 `> 3` 的 off-by-one）。

## 第五轮（工程收敛轮）

> 28. **归因失败信号**：取数失败不再被当作"无波动"，归因结果挂 `dim_errors` 并在结论中显式提示未参与归因的维度。
> 29. **通用 N 因子分摊**：`factorize` 由双因子扩展为任意 N 因子的主效应 + 交互项，公式串动态生成。
> 30. **LLM 调用加固**：单次调用超时 + 有限重试 + 总预算（`LLM_TIMEOUT` / `LLM_TOTAL_TIMEOUT`），失败降级不阻塞主链路。
> 31. **路由层拆分与并发保护**：`api.py` 拆为 `apiroutes/` 各 router + 独立会话记忆模块（对外路径/签名不变），
>     同一 `(caller, session_id)` 的分析请求串行化，防止共享工作记忆被并发写撕裂。
> 32. **StateGraph 编译缓存**：编排图编译一次、跨请求复用（per-request 参数经 `state["_ctx"]` 注入）。
> 33. **前端治理看板 + 工程化**：新增治理/HITL 复核看板；前端接入 eslint 与 node:test 单测；
>     报告导出改文件流（`FileResponse`）；CI 统一 Python 3.12、改跑 `run_all.py` 并加覆盖率门槛；
>     `start_api.bat` 拆分开发（`--reload`）/生产两脚本。评测方法学与局限说明拆入 `docs/`。

## 第六轮（可运营轮）——把"演示能跑"推到"运营不塌"

> 34. **真实登录 + 身份贯通审计**：新增 `POST /api/auth/login`（口令校验→签发随机会话 token，`sessions` 表含归属人与到期时间）、
>     `POST /api/auth/logout`（即时撤销）、`GET /api/auth/me`。鉴权优先级改为
>     **登录会话 > 静态 `SQLPA_API_TOKENS` > 最小权限默认角色**，取数/分析/治理动作的审计与流转 `actor`
>     从此记到**具体的人**（此前只记到"某个 token"或空串）；登录按用户名限速挡撞库。
>     前端新增登录条（账号口令 + 静态凭据两种方式），**所有请求都带 `X-API-Token`**——
>     包括 SSE 流式与 Word 文件流下载这两条最容易漏加的链路。
>     同时把 `storage.ensure_default_users()` 接进启动流程：此前它**只有测试会调**，
>     "设了引导口令就自动建管理员账号"这件事实际上从没发生过，登录端点也就没有账号可用。
> 35. **会话记忆与限速持久化**：两类状态从**纯进程内 dict** 迁到 SQLite（`session_memory` / `rate_limit` 表）——
>     单进程重启不再丢多轮下钻上下文、不再把限速计数清零；多 worker 共享同一 DB 文件时行为一致。
>     并在 README / [`docs/limitations.md`](limitations.md) 明确**部署边界**（默认单 worker；调大 `--workers` 需同库，跨机需 Redis）。
> 36. **HITL SLA / 超时升级 / 催办**：工单登记即带 `due_at`（`SQLPA_HITL_SLA_HOURS`，默认 24h）；
>     `hitl.sweep()` **幂等**地执行「超期 → 置 escalated 并写流转历史 + 审计」「临期（不足 1h 或 SLA 的 1/4）→ 催办一次」；
>     新增 `GET /api/hitl/escalations`（超期/临期，看板数据源）、`POST /api/hitl/sweep`（仅 admin）与
>     `tools/run_hitl_sla.py`（cron / Task Scheduler 调度入口，与 `run_subscriptions.py` 同款）；
>     治理看板新增「SLA 超期升级」格与 admin「SLA 巡检」按钮。回归见 `tests/test_auth_session_sla.py`（13 项）。

## 第七轮（补齐"有表无入口 / 有数无展示 / 有标记无投递"）

> 37. **个人工作台（查询历史 / 收藏 / 反馈）**：`storage` 早就有 `audit_logs / favorites / feedback` 三张表与读写函数，
>     却**没有任何端点**——"看历史 / 存收藏 / 提反馈"在系统里做不到。现新增 `GET /api/history`（按调用者隔离、分页）、
>     `GET/POST /api/favorites`、`DELETE /api/favorites/{id}`（按 username 过滤，删不到别人的）、
>     `GET/POST /api/feedback`（非 admin 只看自己的），并新增前端**「我的工作台」页签**（收藏支持**一键复跑**回填分析台）。
>     回归见 `tests/test_workspace_api.py`。此前 README/`limitations.md` 均如实写"尚未暴露端点与页面"，本轮兑现。
> 38. **统计门控从"只在后端"变为"前端可见"**：显著性检验（Welch z 的 z / p / α 与结论）此前只存在于
>     `decision` 载荷里、界面看不见，用户无法复核"为什么没告警"。现于 `DecisionCard` 增加「统计门控」块，
>     并为 `watch`（观察项·证据不足）/`inconclusive`（数据不足）补上各自的视觉语义——
>     "没敢报"不再被显示成"没问题"。
> 39. **指标中心写接口（CRUD，仅 admin）**：`metric_store` 的指标/维度/别名增删改与校验**无端点调用**，语义层只能手改 YAML。
>     现新增 `POST /api/metrics/upsert`、`DELETE /api/metrics/{key}`、`POST/DELETE /api/metrics/dimensions*`、
>     `POST/DELETE /api/metrics/aliases*`（写操作一律 admin；经校验后落回权威 `business_config.yaml`，
>     并可经 `SQLPA_METRIC_CONFIG_PATH` 隔离写入）。回归见 `tests/test_metrics_admin_api.py`（含"编辑指标不得丢 join_clause"的历史事故回归）。
> 40. **HITL 升级/催办的真实投递**：此前只写本地 outbox 文件，"催办"仅停留落盘。
>     现 `hitl.deliver_webhook()` 把升级摘要 POST 到 **`SQLPA_HITL_WEBHOOK`**，投递结果挂回巡检 summary；
>     未配置地址则**显式返回"未投递"**（不静默成功），投递失败被捕获且不影响巡检。回归见 `tests/test_hitl_webhook.py`。
> 41. **SQLite 多 worker 就绪**：`storage._conn()` 开启 **WAL 日志模式 + busy_timeout**（可用 `SQLPA_SQLITE_JOURNAL_MODE`
>     / `SQLPA_SQLITE_BUSY_TIMEOUT_MS` 覆盖），并发写不再直接抛 `database is locked`；同步更新 README / `limitations.md` 部署边界。
>     回归见 `tests/test_sqlite_concurrency.py`（含 8 线程并发写）。
> 42. **README 评测表格 ↔ 报告产物一致性校验**：新增 `evaluation/check_readme_consistency.py`，逐项比对 README 表格数字
>     与 `evaluation/reports/*.json`（含 row1「100% × 5」压缩写法、row3「判定题 N」等结构性断言），
>     并由 `tests/test_readme_consistency.py` 纳入 CI，防止"重跑后数字漂移、文档停在旧值"。
>     **告警假阳性率仍无独立标注集，如实标注为未做**（不虚构 FP/FN 数字）。

