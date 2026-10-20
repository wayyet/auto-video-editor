# ============================================================
# auto-video-editor 一键启动(本地开发版,不需要管理员)
#
# 与 start_production.ps1 的区别:
#   - 不做一刀切管理员自检(真正需要 admin 的只有 Start-Service
#     com.docker.service,而 Docker daemon 在线时这一步根本用不到)
#   - 跳过 Docker 代理自检(避免自动 docker desktop restart 打断当前会话)
#   - 自动生成 runtime\initial_state_<thread>.json
#     (该文件被 .gitignore 排除,新克隆/清理后必然缺失;
#      start_production.ps1 在 Phase 0 做同一件事)
#   - 默认 STORYLINE_MODE=auto(19 节点确定性图,能跑完出成片);
#     用 -StorylineMode human 切回旧的人工关卡链
#
# 用法(普通 Windows PowerShell 即可):
#   .\scripts\start_local.ps1                          # 默认 thread-id=video-001
#   .\scripts\start_local.ps1 -ThreadId video-002
#   .\scripts\start_local.ps1 -StorylineMode human     # 走人工关卡链
#   .\scripts\start_local.ps1 -SkipPreflight           # 环境已就绪,跳过自检
#   .\scripts\start_local.ps1 -WhatIf                   # 只打印计划
# ============================================================

[CmdletBinding()]
param(
    [string]$ThreadId        = 'video-001',
    [string]$VideoPath       = '',
    [ValidateSet('auto', 'human')]
    [string]$StorylineMode   = 'auto',
    [string]$PostgresUri     = 'postgresql://postgres:devpass@localhost:5432/langgraph',
    [switch]$SkipPreflight,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding  = [System.Text.Encoding]::UTF8
    $OutputEncoding           = [System.Text.Encoding]::UTF8
    $PSDefaultParameterValues['*:Encoding'] = 'utf8'
} catch { }

$RepoRoot   = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$LogsDir    = Join-Path $RepoRoot 'logs'
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null }
$Timestamp  = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogFile    = Join-Path $LogsDir ("start_local_${Timestamp}.log")
$VenvPy     = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$RunScript  = Join-Path $RepoRoot 'scripts\run_workflow.py'
$StateJson  = Join-Path $RepoRoot ("runtime\initial_state_${ThreadId}.json")

function Write-Phase {
    param([string]$Text, [string]$Color = 'Cyan')
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $Text"
    Write-Host $line -ForegroundColor $Color
    # 日志写失败(文件被占用等)不应中断主流程
    Add-Content -Path $LogFile -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}
function Write-PhaseOk   { param([string]$Text) Write-Phase "✓ $Text" 'Green' }
function Write-PhaseWarn { param([string]$Text) Write-Phase "! $Text" 'Yellow' }
function Write-PhaseErr  { param([string]$Text) Write-Phase "✗ $Text" 'Red' }

Write-Phase ('=' * 60) 'Magenta'
Write-Phase "auto-video-editor 一键启动(本地开发版,免管理员)" 'Magenta'
Write-Phase ("ThreadId     : {0}" -f $ThreadId) 'Magenta'
Write-Phase ("StorylineMode: {0}" -f $StorylineMode) 'Magenta'
Write-Phase ("日志路径     : {0}" -f $LogFile) 'Magenta'
Write-Phase ('=' * 60) 'Magenta'

if ($WhatIf) {
    Write-Phase "[WhatIf] 不执行任何修改,计划如下:" 'Yellow'
    Write-Phase "  Phase 0:生成 $StateJson"
    Write-Phase "  Phase 1:scripts\start_postgres.ps1(起 edb-postgres16)"
    if (-not $SkipPreflight) {
        Write-Phase "  Phase 2:scripts\preflight.ps1 -SkipDockerService -SkipProxyCheck"
    } else {
        Write-Phase "  Phase 2:[跳过 -SkipPreflight]"
    }
    Write-Phase "  Phase 3:run_workflow.py --production --thread-id $ThreadId"
    exit 0
}

# ---------------------------------------------------------------
# Phase 0 — 生成 initial-state.json
# ---------------------------------------------------------------
Write-Phase ''
Write-Phase "Phase 0:准备 initial state" 'Cyan'

if (-not $VideoPath) {
    $VideoPath = Join-Path $RepoRoot 'inputs\30s.mp4'
}
if (-not (Test-Path $VideoPath)) {
    Write-PhaseErr "找不到素材视频: $VideoPath"
    Write-Phase "    放一个 >=5s 的 mp4 到 inputs\30s.mp4,或用 -VideoPath 指定" 'Yellow'
    exit 12
}
$VideoPath = (Resolve-Path $VideoPath).Path

$state = [ordered]@{
    session_id       = $ThreadId
    video_input_path = $VideoPath
    error_log        = @()
}
$stateJsonText = $state | ConvertTo-Json -Depth 4
$runtimeDir = Split-Path $StateJson -Parent
if (-not (Test-Path $runtimeDir)) { New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null }
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($StateJson, $stateJsonText, $utf8NoBom)
Write-PhaseOk "生成 $StateJson"
Write-Phase ("  video_input_path = {0}" -f $VideoPath)

# ---------------------------------------------------------------
# Phase 1 — Postgres 容器
# ---------------------------------------------------------------
Write-Phase ''
Write-Phase "Phase 1:启动 Postgres 容器" 'Cyan'
& pwsh -NoProfile -File (Join-Path $RepoRoot 'scripts\start_postgres.ps1') 2>&1 | ForEach-Object {
    $t = "$_"
    Write-Host "  $t" -ForegroundColor DarkGray
    Add-Content -Path $LogFile -Value "  $t" -Encoding UTF8 -ErrorAction SilentlyContinue
}
if ($LASTEXITCODE -ne 0) {
    Write-PhaseErr "start_postgres.ps1 退出 $LASTEXITCODE"
    exit 14
}
Write-PhaseOk "Postgres 容器 healthy"

# ---------------------------------------------------------------
# Phase 2 — preflight 自检
# ---------------------------------------------------------------
if (-not $SkipPreflight) {
    Write-Phase ''
    Write-Phase "Phase 2:pre-flight 自检(跳过需管理员的 Step1 与代理 Step3)" 'Cyan'
    & pwsh -NoProfile -File (Join-Path $RepoRoot 'scripts\preflight.ps1') `
        -SkipDockerService -SkipProxyCheck 2>&1 | ForEach-Object {
        $t = "$_"
        Write-Host "  $t" -ForegroundColor DarkGray
        Add-Content -Path $LogFile -Value "  $t" -Encoding UTF8 -ErrorAction SilentlyContinue
    }
    if ($LASTEXITCODE -ne 0) {
        Write-PhaseErr "preflight.ps1 退出 $LASTEXITCODE"
        exit 15
    }
    Write-PhaseOk "pre-flight 全部通过"
} else {
    Write-Phase ''
    Write-Phase "Phase 2:跳过(-SkipPreflight)" 'Yellow'
}

# ---------------------------------------------------------------
# Phase 3 — run_workflow.py
# ---------------------------------------------------------------
Write-Phase ''
Write-Phase "Phase 3:端到端 invoke" 'Cyan'

if (-not (Test-Path $VenvPy)) {
    Write-PhaseErr "找不到 $VenvPy(先建 .venv 并装依赖)"
    exit 16
}

# 环境变量:checkpointer / 图模式 / preflight 跳过项
$env:POSTGRES_URI = $PostgresUri
$env:STORYLINE_MODE = $StorylineMode
$env:AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_DOCKER_SERVICE = '1'
$env:AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_PROXY_CHECK = '1'

# ffmpeg 不在默认 PATH 时补上(渲染/抽帧依赖)
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    $ffmpegBin = Get-ChildItem 'C:\ffmpeg' -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'bin' } |
        Where-Object { Test-Path (Join-Path $_ 'ffmpeg.exe') } |
        Select-Object -First 1
    if ($ffmpegBin) {
        $env:PATH = "$ffmpegBin;$env:PATH"
        Write-Phase "  ffmpeg: $ffmpegBin"
    } else {
        Write-PhaseWarn "PATH 里没有 ffmpeg,渲染步骤可能失败(装到 C:\ffmpeg\ffmpeg-*\bin)"
    }
}

$pyOutput = @()
& $VenvPy $RunScript `
    --production `
    --thread-id $ThreadId `
    --initial-state-json $StateJson `
    2>&1 | ForEach-Object {
    $t = "$_"
    $pyOutput += $t
    Write-Host "  $t" -ForegroundColor DarkGray
    Add-Content -Path $LogFile -Value "  $t" -Encoding UTF8 -ErrorAction SilentlyContinue
}

$pyExit = $LASTEXITCODE

# GraphInterrupt 落在人工关卡是预期行为,不算失败
$isGraphInterrupt = $false
foreach ($line in $pyOutput) {
    if ("$line" -match 'GraphInterrupt|checkpoint.*①|"checkpoint":\s*"①"') {
        $isGraphInterrupt = $true
        break
    }
}

if ($pyExit -eq 0) {
    Write-PhaseOk "run_workflow.py 退出码 0(端到端跑完)"
} elseif ($isGraphInterrupt) {
    Write-PhaseOk "run_workflow.py 在人工关卡抛 GraphInterrupt(预期行为,需在剪映手动操作后 resume)"
} else {
    Write-PhaseErr "run_workflow.py 退出码 $pyExit(日志尾: $LogFile)"
    exit $pyExit
}

Write-Phase ''
Write-Phase ('=' * 60) 'Magenta'
Write-Phase "一键启动完成" 'Green'
Write-Phase ("完整日志: {0}" -f $LogFile) 'Cyan'
Write-Phase "停止请跑 .\stop_production.ps1" 'Cyan'
Write-Phase ('=' * 60) 'Magenta'
exit 0
