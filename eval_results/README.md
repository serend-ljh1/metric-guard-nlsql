# Spider-dev 实测记录（N=50）

本目录是 README「真实评测结果」一节的**原始逐题产物**——去掉它，那些数字就无法复核。

## 运行配置

| 项 | 值 |
|---|---|
| 数据集 | Spider-dev（`dev.json` 1034 题 / 20 库），跨库分层随机抽样 **N=50，seed=42** |
| 引擎 | `--engine langgraph`（LangGraph StateGraph） |
| 模型 | **qwen3.7-flash**（阿里云百炼兼容端点），`LLM_TEMPERATURE=0` |
| 命令 | `python run_eval.py --dataset spider --split dev --sample 50 --seed 42 --engine langgraph --baseline --ablation --ablation-rounds 1,3 --out-dir ./eval_results` |
| 花费 | 4 臂合计 **¥0.159** |

## 结果

| 配置 | EX | EM | token/题 | 成本/题 | 平均延迟 | 修复轮次 |
|---|---|---|---|---|---|---|
| L0 单次直出（`baseline_zeroshot`） | 0.82 (41/50) | 0.04 | 2367 | ¥0.00062 | 28.9s | 0.00 |
| L1 引擎（`baseline_engineL1`） | 0.86 (43/50) | 0.02 | 2923 | ¥0.00077 | 36.2s | 0.14 |
| L1 引擎（`ablation_L1`，同配置重跑） | 0.88 (44/50) | 0.02 | 2845 | ¥0.00074 | 39.0s | 0.12 |
| L3 全自愈（`ablation_L3`） | 0.90 (45/50) | 0.04 | 4001 | ¥0.00104 | 47.7s | 0.38 |

结论与不确定度见 README。要点：
- 自愈有效：L0 → L1 为 **+4 ~ +6 pp**；
- L1 → L3 为 **+2 ~ +4 pp**，但**同配置两次运行就差 1 道题（2.0 pp）**，故该幅度不足以作为强结论；
- L3 的 token 是 L0 的 **1.69 倍**。

## 文件说明

- `<臂名>_<db_id>.langgraph.json`：每个库一份，含 `summary` 与 `results`（每题 `final_sql`/`gold_sql`/`ex`/`em`/`gold_failed`/`route`/`repairs`/`attempts`/`latency_ms`/`terminate_reason`/`total_tokens`/`cost`）。
- `_summary_arms.json`：四臂汇总（本文件表格的机器可读版）。
- `run.log`：完整终端日志（含逐题进度与各臂汇总）。

## 复核方法

```bash
python run_eval.py --dataset spider --split dev --sample 50 --seed 42 --engine langgraph \
  --baseline --ablation --ablation-rounds 1,3 --out-dir ./eval_results
```

注意：LLM 在 `temperature=0` 下仍非完全确定，复跑结果可能相差 1~2 道题（±2~4 pp）。
