# ============================================================================
# Spider-dev 评测一键脚本（Windows PowerShell）
# ============================================================================
# 用法：
#   .\run_spider_eval.ps1 -Sample 50            # 推荐：N=50，约 1.5 小时
#   .\run_spider_eval.ps1 -Sample 100           # 完整：N=100，约 3 小时
#   .\run_spider_eval.ps1 -Sample 30 -Quick     # 快速验证：只跑主跑 + 基线
#
# 说明：
#   - 抽样与引擎参数固定为 seed=42 / langgraph，便于他人复现；
#   - 各臂逐题 JSON 落在 eval_results/，请连同数字一起提交以便复核；
#   - 单题平均约 26 秒（Spider 多表 schema 提示较长），请预留整块时间，
#     建议在 PyCharm 里当长任务跑，或后台运行并重定向日志。
# ============================================================================
param(
    [int]$Sample = 50,
    [int]$Seed = 42,
    [switch]$Quick,
    [string]$Model = "qwen3.7-flash"
)

$ErrorActionPreference = "Stop"
$Py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

$OutDir = ".\eval_results"
$LogDir = ".\eval_results\logs"
New-Item -ItemType Directory -Force -Path $OutDir, $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"

Write-Host "=== Spider-dev evaluation ===" -ForegroundColor Cyan
Write-Host "model     : $Model  (must match LLM_MODEL in .env)"
Write-Host "sample    : N=$Sample  seed=$Seed"
Write-Host "engine    : langgraph"
Write-Host "artifacts : $OutDir"
Write-Host "logs      : $LogDir\eval-$Stamp.log"
Write-Host ""

function Invoke-Arm {
    param([string]$Name, [string[]]$ExtraArgs)
    $log = Join-Path $LogDir "eval-$Stamp.$Name.log"
    Write-Host "[$Name] start -> $log" -ForegroundColor Yellow
    $common = @(
        "run_eval.py",
        "--dataset", "spider",
        "--split", "dev",
        "--sample", "$Sample",
        "--seed", "$Seed",
        "--engine", "langgraph",
        "--out-dir", $OutDir
    )
    & $Py @common @ExtraArgs 2>&1 | Tee-Object -FilePath $log
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[$Name] exit code $LASTEXITCODE (see log)" -ForegroundColor Red
    } else {
        Write-Host "[$Name] done" -ForegroundColor Green
    }
}

# 基线对照：单次直出(zero-shot) vs 完整引擎(L1) + 自愈深度消融 L1/L3。
# L0 已由 --baseline 的 zero-shot 臂覆盖，故消融档位只用 1,3，避免重复烧钱。
Invoke-Arm -Name "baseline+ablation" -ExtraArgs @("--baseline", "--ablation", "--ablation-rounds", "1,3")

if (-not $Quick) {
    Invoke-Arm -Name "critic" -ExtraArgs @("--critic-ablation")
    Invoke-Arm -Name "schema-link" -ExtraArgs @("--schema-link-ablation")
}

Write-Host ""
Write-Host "=== all done ===" -ForegroundColor Cyan
Write-Host "artifacts : $OutDir"
Write-Host "send back the JSONs under $OutDir plus the terminal log; README numbers will be updated from them."
