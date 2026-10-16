# evaluation/spider_probe —— 仓库内可复现的第三方基准语料

## 这是什么

`eval_external_bank.py` 的**离线可复现**语料：把 Spider 数据集 dev 集中 `db_id=world_1`
的全部 **120** 条问题与对应 gold SQL，连同该库的 sqlite 文件一起内联进仓库。

- `world_1_dev_sample.json` —— 与 `dev.json` 中 `db_id == "world_1"` 的条目**逐字相同**
  （结构也是同一套：`db_id / question / query / sql / query_toks / …`）。
  生成方式：从官方 dev.json 过滤 `db_id == "world_1"`，不抽样、不改写。
- `world_1.sqlite` —— Spider 的 `world_1` 库文件。
- 语料 sha256 由脚本计算并写进报告 `provenance`（`dev_json_sha256` / `db_sha256`），
  因此"这个覆盖率是在哪份语料上算的"永远可复核。

## 为什么要在仓库里放一份

完整 Spider 数据集属**仓库外**资源（3.6 MB 的 dev.json + 各库文件），
换台机器 clone 后就没有 → 评测静默跳过、README 里的数字无法复核。

`eval_external_bank.py` 的取值顺序是：
1. `--spider-root` / `SQLPA_SPIDER_ROOT` 指向的**完整** Spider 数据集；
2. 缺失时**退回本目录**的样本语料，并在报告里标注 `corpus_source=in_repo_probe`。

两者都在时才用全量；只有本目录可用时照样跑，且同一批问题（N=120）结果一致
（world_1 的全量子集本来就是 120 条，不是抽样，所以数字可直接对上）。

## 来源与许可

数据来自 **Spider: A Large-Scale Human-Labeled Dataset for Complex and
Cross-Domain Semantic Parsing and Text-to-SQL**（Yu et al., EMNLP 2018），
由 Yale LILY 实验室公开。原始许可：CC BY-SA 4.0（详见数据集官方仓库）。
本目录仅保留评测所需的最小片段（1 个库 + 该库的 120 条开发集问题），
并保留其原始内容与出处，未作改写。

## 复现

```bash
# 离线（用本目录内联语料）
python evaluation/eval_external_bank.py --report-dir evaluation/reports

# 用完整 Spider（若本地有完整数据集）
python evaluation/eval_external_bank.py --spider-root "/path/to/spider" --report-dir evaluation/reports
```

下载完整数据集的帮助见 `tools/download_data.py`（Spider 的官方分发渠道）。

## 多库扩展（换域成本量化）现状 —— 如实标注

本目录**只内联了 1 个库（`world_1`）**，`eval_external_bank.py` 报告因此只有一个世界的数字。
"扩到 Spider 多个 dev 库、量化换域成本"需要**完整 Spider 数据集**（仓库外资源，需联网获取）；
本机在评测时**无外网**（`tools/download_data.py` / `tools/fetch_external_corpus.py` 均无法连通），
故**未做**多库扩展，**不虚构多库数字**。

已有可复核的"换域成本"证据仅覆盖 `world_1` 这一个域，其形态是：**换域只改 1 个 YAML
（`config/semantic_world.yaml`）、0 行业务代码**——语义层把"域"外置成配置，新增一个域的
边际成本是"写一份 YAML"，而不是改编译/执行链路。多库版本需在有网机器上补齐完整数据集后复跑
`--spider-root`，届时同一脚本会给出跨库的覆盖率/换域成本对照。
