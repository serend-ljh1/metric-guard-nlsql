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
