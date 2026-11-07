# =============================================================================
#  OpenStoryline · Windows 模型下载脚本
#  作用: 下载并解压 models.zip 到仓库根 .storyline\models\ 下。
#  与 download_resources.ps1 拆开,便于单独重试(模型 ~116 MB)。
#  用法(2026-11-01 扁平化后,在仓库根目录):
#         powershell -ExecutionPolicy Bypass -File .\scripts\download_models.ps1
#  仓库根由 $PSScriptRoot 推导(脚本并入根 scripts/ 后不再依赖调用方 CWD),
#  模型一律解压到仓库根 .storyline/models/,不会落到旧 openstoryline/ 路径。
# =============================================================================

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
$base = "https://image-url-2-feature-1251524319.cos.ap-shanghai.myqcloud.com/openstoryline"

Write-Host "[i] 准备目录 .storyline\models ..." -ForegroundColor Blue
New-Item -ItemType Directory -Force -Path ".storyline" | Out-Null
New-Item -ItemType Directory -Force -Path ".storyline\models" | Out-Null

function Get-Zip($url, $out) {
    if (Test-Path $out) { Write-Host "[i] 已存在,跳过下载: $out" -ForegroundColor Yellow; return }
    Write-Host "[i] 下载 $url" -ForegroundColor Blue
    # Windows 自带 curl.exe(比 Invoke-WebRequest 快很多,适合大文件)
    curl.exe -L --fail -o $out $url
    if ($LASTEXITCODE -ne 0) { throw "下载失败: $url" }
}

# 1) 模型 → .storyline\models\
Get-Zip "$base/models.zip" ".storyline\models.zip"
Write-Host "[i] 解压 models.zip -> .storyline\models\" -ForegroundColor Blue
Expand-Archive -Path ".storyline\models.zip" -DestinationPath ".storyline\models" -Force
Remove-Item ".storyline\models.zip" -Force

# 自检关键模型文件是否就位
$weights = ".storyline\models\transnetv2-pytorch-weights.pth"
if (Test-Path $weights) {
    Write-Host "[OK] 镜头分割模型已就位: $weights" -ForegroundColor Green
    Write-Host "[i] TransNetV2 已接入运行时(2026-11-08 选项C):storyline_capabilities/split_shots.py" -ForegroundColor Blue
    Write-Host "    主 venv 进程内推理,需 torch(仅 CPU 轮即可);STORYLINE_SPLIT_SHOTS_DETECTOR 控制检测器。" -ForegroundColor Blue
} else {
    Write-Host "[!] 未找到 $weights —— 解压目录可能多套了一层,请到 .storyline\models 下确认实际路径," -ForegroundColor Yellow
    Write-Host "    必要时把 .pth 文件移到 .storyline\models\ 下(仓库约定模型目录;config.toml 已于 2026-11-04 作废删除," -ForegroundColor Yellow
    Write-Host "    缺权重时切镜自动回退帧差版,原因见监控事件流 TRANSNET_FALLBACK)。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[OK] 模型下载完成。" -ForegroundColor Green