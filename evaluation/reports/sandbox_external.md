# 沙箱外部对抗语料评测（D）

> 脚本：`evaluation/eval_sandbox_external.py`　语料：`evaluation/sandbox_corpus/sqli_corpus.jsonl`
> 复现：`python evaluation/eval_sandbox_external.py --report-dir evaluation/reports`
> 明细：`evaluation/reports/sandbox_external.json`（本文件的所有数字都可由它复核）

## 1. 为什么需要这份评测

此前"沙箱只读、语句级拦截"的全部证据都来自本项目自己的测试 —— 这是**自证**：
用例由实现者挑，天然偏向"我知道能拦住的那几类"。本评测换成一份**显式标注来源与
意图**的对抗语料，并同时测两个方向：

| 方向 | 含义 | 为什么必须测 |
|---|---|---|
| 危险语料**拦截率** | 写入 / DDL / 多语句 / 危险函数 → 必须被拦 | 漏一个就是越权写库 |
| 只读语料**误杀率** | 合法只读查询（含 UNION、CTE、字符串里出现高危词）→ 不该被拦 | 关键字黑名单天然会误杀，这条以前**完全没测过** |
| **库不变性** | 跑完整个语料后，库文件 sha256 与各表行数必须一字不变 | "只读"不能只是嘴上说 |
| 过滤值注入 | 走 `compiler.render_filter → _quote`，验证注入串被当作**字面量** | 语义层自身也是一个注入面 |

## 2. 语料来源（诚实声明）

`evaluation/sandbox_corpus/sqli_corpus.jsonl` 共 **99 条**（sha256 `4a86f3e98fae…`），
按公开类别构造：OWASP WSTG / CWE-89 / sqlmap tamper 类别 / 各数据库（Oracle/MSSQL/PostgreSQL/MySQL）官方危险功能文档。

**它不是官方语料文件本身。** 要换成 sqlmap / PayloadsAllTheThings 的原始语料，在有网机器上跑：

```bash
python tools/fetch_external_corpus.py --source payloadsallthethings
python evaluation/eval_sandbox_external.py --corpus data/external/sqli_payloadsallthethings.jsonl
```

`tools/fetch_external_corpus.py` 会记录来源 URL、抓取时间与 sha256（写进 `.provenance.json`）；
抓取失败时**拒绝编造**语料。

语料字段：`id / family / intent(destructive|readonly) / expect / payload / source`（跨方言条目另带 `dialect`）。
构成：**76 条破坏性 + 23 条只读**（含 8 条 `readonly_baseline` + 10 条 `readonly_dialect`）。

## 3. 结果

| 指标 | 结果 |
|---|---|
| 危险语料拦截率（策略层） | **76 / 76 = 100.0%**　无绕过 |
| 只读语料误杀率 | **1 / 23 = 4.3%**（唯一误杀 `ro-05`） |
| 库不变性 | 文件 sha256 **一致** / 各表行数 **一致** |
| 过滤值注入 | **5 / 5** 被当作字面量（单引号被转义、无分号逃逸） |
| 语料中"非完整 SQL"条目 | 0（未计入拦截/误杀的干扰项为 0，即分母干净） |

覆盖的族：`tautology / stacked_write / ddl / sqlite_specific /
dangerous_func / union_probe / comment_evasion / encoding / case_evasion / whitespace /
dialect_oracle / dialect_mssql / dialect_pg / dialect_mysql /
encoding_bypass / tamper_whitespace / tamper_comment /
readonly_baseline / readonly_dialect`（含 Oracle / MSSQL / PostgreSQL / MySQL 跨方言类别）。

### 3.1 唯一误杀：`ro-05`

```sql
SELECT 'DROP TABLE x' AS note
```

这是一条**纯只读**查询：高危词只出现在字符串字面量里。策略层按关键字拦下了它 ——
误杀而非漏杀，方向安全，但仍然是误杀，因此**如实计入并固定在测试里**
（`tests/test_sandbox_external_corpus.py` 中 `_KNOWN_FALSE_POSITIVES = {"ro-05"}`），
不允许它悄悄变化：一旦新的解析改动让误杀集合变大，测试立刻红。

需要说明的是：**策略层拦下 ≠ 完全不可用**。策略层是"先按文本粗筛"，粗筛误杀合法查询的
代价是"少答一条"，而漏筛的代价是"越权写库"，两者不对称，所以这里选择保守。
若要消除这条误杀，正确做法是加一层轻量 SQL 解析（把字面量与标识符分开再做判定），
属明确的后续工作。

## 4. 本轮评测**发现并修掉**的真实缺陷

评测不是走过场：它暴露了 4 个此前未被任何测试覆盖的漏洞，全部已修并补了回归测试。

| # | 缺陷 | 为什么危险 | 修法 |
|---|---|---|---|
| 1 | `_BLOCKED_FUNCTIONS` 用小写函数名去匹配**已大写**的 SQL → **黑名单从未生效** | `load_extension` 之前只是"恰好"被 SQLite 的扩展加载开关挡住，不是被我们的策略挡住；换方言即失守 | 修正大小写（`fn.upper()`），并补齐跨方言危险函数：`sleep / benchmark / load_file / pg_sleep / pg_read_file / pg_ls_dir / dblink / xp_cmdshell` |
| 2 | `INTO OUTFILE` / `INTO DUMPFILE` 未被拦 | MySQL 场景下这是**写文件**，属破坏性操作却被放行 | 加入 `_BLOCKED_KEYWORDS` |
| 3 | `PRAGMA` 检测用的是小写正则去匹配大写文本 → `PRAGMA_*` 变体可绕过 | SQLite 专有的库级操作可绕过拦截 | 改为 `\bPRAGMA_\w+` 并保持大小写无关 |
| 4 | 负面函数清单用**词边界**匹配，`pg_read_file` 匹配不到同族 `pg_read_binary_file` / `pg_stat_file` / `lo_import` / `lo_export` | 探针实测 `SELECT pg_read_binary_file('/etc/passwd')` 被**放行**（ALLOW）；真实 PG 连接下即服务端任意文件读 | 补入这 4 个同族函数名（本次扩充跨方言语料时发现） |

## 5. 这份证据**不能**证明什么

- **不能**证明"对所有 SQL 注入都免疫"。语料是按公开类别构造的 99 条，不是穷举；
  真正的结论是"在这 99 条已知类别上 100% 拦截、库未被改动"。
- **不能**替代渗透测试。策略层基于文本规则，已知其局限（见 3.1 的误杀；
  规则型黑名单在极端构造下存在绕过的理论空间）。
- **不能**证明跨方言沙箱的**等价性**（只能证明"策略层不失效"）：语料已扩充 Oracle / MSSQL / PostgreSQL / MySQL
  专属危险 payload 及编码/篡改绕过类别，执行层当前是只读 SQLite，方言 payload 常"函数不存在"而执行失败；
  但**策略层 `_sanitize` 是方言无关的**，同一套负面清单同样作用于 MySQL/PostgreSQL 连接器——
  本语料对每个方言 payload 断言的都是**策略层拦截**。连接器的**执行层护栏**（时长/行数）由各连接器实现，
  未在真实 MySQL/PG 实例上验证（见 README 的已知限制）。

真正的安全边界仍然多层叠加：**连接为 `mode=ro` + `PRAGMA query_only=ON`**
（即使策略层被绕过也写不进库）+ 单语句限制 + 函数/关键字黑名单 + 查询超时。
本次评测的"库不变性"一项正是对这一边界的独立验证：76 条破坏性 payload 全部打进去之后，
库文件的 sha256 与各表行数一字未变。

## 6. 与上一轮相比的变化

| | 上一轮 | 本轮 |
|---|---|---|
| 语料 | 无（只有自编单测） | 99 条显式来源对抗语料 + 官方语料抓取脚本（含跨方言，本机无外网未拉官方语料） |
| 危险拦截率 | 未度量 | 76/76 = 100%（无绕过） |
| 只读误杀率 | **未度量过** | 1/23 = 4.3%（`ro-05`，已固定） |
| 库不变性 | 未度量 | sha256 + 行数一致 |
| 过滤值注入 | 未度量 | 5/5 字面量化 |
| 发现缺陷 | — | 4 个真实漏洞（含"黑名单从未生效"、`pg_read_binary_file` 同族函数漏网） |
