# =============================================================================
#  OpenStoryline (auto-video-editor 嵌入版) · 一键安装脚本 (Windows)
#  作用:
#    1. 创建 .venv (本目录独立 venv,含 torch/torchaudio/funasr 等重包)
#    2. pip install -r requirements.txt
#    3. (可选) 拉资源 + 模型 — 642 MB,提示人工按需跑 download_*.ps1
#
#  用法(在 openstoryline/ 目录下):
#     powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#
#  设计:
#  - 资源/模型不在本脚本强制拉(避免首次 clone 阻塞 10+ 分钟)
#  - 用户可选择 -SkipResources / -SkipModels 跳过某一步
#  - 由 start_production.ps1 (Phase 0.55) 在启动时校验缺失
# =============================================================================

[CmdletBinding()]
param(
    [switch]$SkipVenv,
    [switch]$SkipPip,
    [switch]$SkipResources,
    [switch]$SkipModels
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
Set-Location $root

Write-Host "[i] OpenStoryline 安装脚本 (auto-video-editor 嵌入版)" -ForegroundColor Cyan
Write-Host "    Root: $root" -ForegroundColor DarkGray

# 1) venv
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if ($SkipVenv) {
    Write-Host "[i] 跳过 venv 创建 (-SkipVenv)" -ForegroundColor Yellow
} else {
    if (Test-Path $venvPy) {
        Write-Host "[OK] 已存在 venv: $venvPy" -ForegroundColor Green
    } else {
        Write-Host "[i] 创建 venv ..." -ForegroundColor Blue
        python -m venv "$root\.venv"
        if ($LASTEXITCODE -ne 0) { throw "venv 创建失败" }
    }
}

# 2) pip install
if ($SkipPip) {
    Write-Host "[i] 跳过 pip install (-SkipPip)" -ForegroundColor Yellow
} else {
    Write-Host "[i] 升级 pip ..." -ForegroundColor Blue
    & $venvPy -m pip install --upgrade pip
    Write-Host "[i] pip install -r requirements.txt (含 torch/torchaudio/funasr,首次约 5-10 分钟) ..." -ForegroundColor Blue
    & $venvPy -m pip install -r "$root\requirements.txt"
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败,请检查网络或 requirements.txt" }
    Write-Host "[OK] 依赖安装完成" -ForegroundColor Green
}

# 3) 资源(可选)
if ($SkipResources) {
    Write-Host "[i] 跳过资源下载 (-SkipResources)" -ForegroundColor Yellow
} else {
    Write-Host "[i] 拉取资源 (~526 MB,按需) ..." -ForegroundColor Blue
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$root\scripts\download_resources.ps1"
}

# 4) 模型(可选)
if ($SkipModels) {
    Write-Host "[i] 跳过模型下载 (-SkipModels)" -ForegroundColor Yellow
} else {
    Write-Host "[i] 拉取模型 (~116 MB,按需) ..." -ForegroundColor Blue
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$root\scripts\download_models.ps1"
}

Write-Host ""
Write-Host "[OK] OpenStoryline 安装完成。" -ForegroundColor Green
Write-Host "    下一步: 在 openstoryline/config.toml 填入 LLM/VLM/TTS API Key,然后:"
Write-Host "      cd .."
Write-Host "      .\start_production.ps1"