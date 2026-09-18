# ============================================================
# auto-video-editor 一键启动(生产全栈)
#
# 该脚本封装 plan.md 的 Phase 0~3:
#   Phase 0  准备视频输入 + 生成 initial-state.json
#   Phase 1  scripts\preflight.ps1(6 步自检)
#   Phase 2  scripts\run_workflow.py --production 端到端 invoke
#   Phase 3  验收(checkpoints / heartbeat / drafts / outputs)
#
# 关键设计:
#   - Step 1(Docker 服务)需管理员权限;脚本开头自检,非管理员直接退出
#   - 默认 thread-id = video-001;默认源视频来自 jianying-editor skill 演示素材
#   - 全程 UTF-8,避免中文/符号乱码
#   - 每一步写日志到 logs\start_<时间戳>.log;失败保留现场不退
#   - 抛 GraphInterrupt(node_06 关卡①)是预期行为,不视为失败
#
# 用法(管理员 Windows PowerShell):
#   .\start_production.ps1                                # 默认参数一键启动
#   .\start_production.ps1 -ThreadId video-002             # 自定义 thread-id
#   .\start_production.ps1 -SkipPhase3                     # 不跑验收
#   .\start_production.ps1 -WhatIf                         # 仅打印计划
# ============================================================

[CmdletBinding()]
param(
    [string]$ThreadId       = 'video-001',
    [string]$SourceVideo    = 'E:\Documents\kuaishou\FireRed-OpenStoryline\.claude\skills\jianying-editor\assets\video.mp4',
    [string]$InputsDir      = 'E:\Documents\kuaishou\FireRed-OpenStoryline\inputs',
    [string]$InputsFileName = '30s.mp4',
    [string]$PostgresUri    = 'postgresql://postgres:devpass@localhost:5432/langgraph',
    [switch]$SkipPhase3,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

# UTF-8 控制台(避免中文乱码,与 preflight.ps1 保持一致)
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding  = [System.Text.Encoding]::UTF8
    $OutputEncoding           = [System.Text.Encoding]::UTF8
    $PSDefaultParameterValues['*:Encoding'] = 'utf8'
} catch { }

# ---------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '.')).Path
$LogsDir  = Join-Path $RepoRoot 'logs'
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
}
$Timestamp  = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogFile    = Join-Path $LogsDir ("start_${Timestamp}.log")
$Preflight  = Join-Path $RepoRoot 'scripts\preflight.ps1'
$VenvPy     = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$RunScript  = Join-Path $RepoRoot 'scripts\run_workflow.py'
$StateJson  = Join-Path $RepoRoot ("runtime\initial_state_${ThreadId}.json")
$VenvActivate = Join-Path $RepoRoot '.venv\Scripts\Activate.ps1'

# 派生路径
$InputsFull = Join-Path $InputsDir $InputsFileName

# ---------------------------------------------------------------
# 日志函数:同时写控制台 + 日志文件
# ---------------------------------------------------------------
function Write-Phase {
    param([string]$Text, [string]$Color = 'Cyan')
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $Text"
    Write-Host $line -ForegroundColor $Color
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Write-PhaseOk   { param([string]$Text) Write-Phase "✓ $Text" 'Green' }
function Write-PhaseWarn { param([string]$Text) Write-Phase "! $Text" 'Yellow' }
function Write-PhaseErr  { param([string]$Text) Write-Phase "✗ $Text" 'Red' }

# ---------------------------------------------------------------
# 顶部 banner
# ---------------------------------------------------------------
Write-Phase ('=' * 60) 'Magenta'
Write-Phase "auto-video-editor 一键启动(生产全栈)" 'Magenta'
Write-Phase ("ThreadId : {0}" -f $ThreadId) 'Magenta'
Write-Phase ("日志路径 : {0}" -f $LogFile) 'Magenta'
Write-Phase ('=' * 60) 'Magenta'

# ---------------------------------------------------------------
# WhatIf:仅打印计划,不做任何改动
# ---------------------------------------------------------------
if ($WhatIf) {
    Write-Phase "[WhatIf] 不会执行任何修改,以下为计划:" 'Yellow'
    Write-Phase "  Phase 0:复制 $SourceVideo -> $InputsFull"
    Write-Phase "         生成 $StateJson"
    Write-Phase "  Phase 1:$Preflight"
    Write-Phase "  Phase 2:$VenvPy $RunScript --production --thread-id $ThreadId --initial-state-json $StateJson"
    if (-not $SkipPhase3) {
        Write-Phase "  Phase 3:checkpoints SQL + heartbeat + outputs/drafts 列目录"
    }
    exit 0
}

# ===============================================================
# 前置:管理员权限自检
# ===============================================================
Write-Phase "前置:管理员权限自检"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-PhaseErr "当前 PowerShell 不是管理员,Step 1(Start-Service com.docker.service)无法执行"
    Write-Phase "    请用「以管理员身份运行」打开 Windows PowerShell 后重跑本脚本" 'Yellow'
    Write-Phase "    或在 README §2.4 手动从「服务」面板启动 Docker Desktop Service" 'Yellow'
    exit 11
}
Write-PhaseOk "已是管理员"

# ===============================================================
# Phase 0 — 准备视频输入 + 生成 initial-state.json
# ===============================================================
Write-Phase ''
Write-Phase "Phase 0:准备视频输入" 'Cyan'

# 1) 复制 mp4 到 inputs 目录
if (-not (Test-Path $SourceVideo)) {
    Write-PhaseErr "源视频不存在: $SourceVideo"
    Write-Phase "    请把 -SourceVideo 指向一个真实 mp4 文件,或手动放进 inputs 目录后重跑" 'Yellow'
    exit 12
}
if (-not (Test-Path $InputsDir)) {
    New-Item -ItemType Directory -Force -Path $InputsDir | Out-Null
}
$dstLeaf = Split-Path $InputsFull -Leaf
if (-not (Test-Path $InputsFull)) {
    Copy-Item -Path $SourceVideo -Destination $InputsFull -Force
    Write-PhaseOk "已复制源视频 -> $InputsFull"
} else {
    Write-Phase "  视频文件已存在,跳过复制: $dstLeaf"
}
$dstInfo = Get-Item $InputsFull
Write-Phase ("  视频大小: {0:N1} MB" -f ($dstInfo.Length / 1MB))

# 2) 生成 initial-state.json
$state = [ordered]@{
    session_id       = $ThreadId
    video_input_path = $InputsFull
    error_log        = @()
}
$stateJsonText = $state | ConvertTo-Json -Depth 4
$runtimeDir = Split-Path $StateJson -Parent
if (-not (Test-Path $runtimeDir)) {
    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($StateJson, $stateJsonText, $utf8NoBom)
Write-PhaseOk "生成 initial-state.json -> $StateJson"

# ===============================================================
# Phase 0.5 — 启动 Docker Desktop(如 daemon 不可达)
# ===============================================================
Write-Phase ''
Write-Phase "Phase 0.5:确保 Docker Desktop daemon 在线" 'Cyan'

$dockerExe = (Get-Command docker -ErrorAction SilentlyContinue).Source
$prevErrorAction2 = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
$null = & docker info 2>&1
$daemonReady = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevErrorAction2

if ($daemonReady) {
    Write-PhaseOk "Docker daemon 已在线(无需启动 Desktop)"
} else {
    $ddPath = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path $ddPath)) {
        Write-PhaseErr "找不到 Docker Desktop: $ddPath"
        exit 17
    }
    Write-Phase "  daemon 不可达,启动 Docker Desktop ..."
    Start-Process -FilePath $ddPath | Out-Null

    # 轮询 daemon 就绪(最多 90s)
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if ($dockerExe) {
            $ErrorActionPreference = 'SilentlyContinue'
            $null = & docker info 2>&1
            $ok = ($LASTEXITCODE -eq 0)
            $ErrorActionPreference = $prevErrorAction2
            if ($ok) { $daemonReady = $true; break }
        }
        Start-Sleep -Seconds 2
    }
    if (-not $daemonReady) {
        Write-PhaseErr "Docker Desktop 启动后 90s 内 daemon 未就绪"
        Write-Phase "    请手动打开 Docker Desktop GUI,等待 'Docker Desktop is running' 后重跑" 'Yellow'
        exit 17
    }
    Write-PhaseOk "Docker Desktop daemon 已就绪"
}

# ===============================================================
# Phase 0.6 — 拉起 OpenStoryline Web(7860,供 node_03 打开预览)
# ===============================================================
Write-Phase ''
Write-Phase "Phase 0.6:确保 OpenStoryline Web 在 7860 在线" 'Cyan'

$OsContainer = 'openstoryline-web'
$OsImage     = 'openstoryline/openstoryline:v1.0.1'
$OsPort      = 7860
$OsRoot      = Join-Path $RepoRoot 'openstoryline-config'

$prevErrorAction3 = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
$osRunning = (@(& docker ps --filter "name=^${OsContainer}$" --format '{{.Names}}' 2>&1) -contains $OsContainer)
$ErrorActionPreference = $prevErrorAction3

if ($osRunning) {
    Write-PhaseOk "OpenStoryline 容器已在运行"
} else {
    if (-not (Test-Path (Join-Path $OsRoot 'config.toml'))) {
        Write-PhaseErr "找不到 $OsRoot\config.toml(请确认 openstoryline-config/ 已就绪)" 'Yellow'
        exit 18
    }

    # 清理已存在但停止的同名容器(避免 docker run 报 name in use)
    $ErrorActionPreference = 'SilentlyContinue'
    $null = & docker rm -f $OsContainer 2>&1
    $ErrorActionPreference = $prevErrorAction3

    # 构建 docker run 参数;outputs 目录可不存在(容器会自己创建);config.toml 必备
    $runArgs = @(
        'run', '-d', '--name', $OsContainer,
        '-v', "${OsRoot}\config.toml:/app/config.toml",
        '-p', "${OsPort}:${OsPort}"
    )
    if (Test-Path (Join-Path $OsRoot 'outputs')) {
        $runArgs += @('-v', "${OsRoot}\outputs:/app/outputs")
        Write-Phase "  挂载 outputs/"
    } else {
        Write-Phase "  outputs/ 不存在,容器会自建" 'Yellow'
    }
    $runArgs += $OsImage

    & docker @runArgs 2>&1 | Out-Null
    $dockerRunExit = $LASTEXITCODE
    if ($dockerRunExit -ne 0) {
        # 退码非 0 时再 stdout 一次拿真实错误
        Write-PhaseErr "docker run 失败 (exit=$dockerRunExit)"
        & docker @runArgs
        exit 19
    }
    Write-Phase "  已 docker run $OsImage,等待容器 created ..."

    # 短轮询容器创建(最多 30s;真正健康校验放 Phase 0.7)
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $ErrorActionPreference = 'SilentlyContinue'
        $exists = (@(& docker ps -a --filter "name=^${OsContainer}$" --format '{{.Names}}' 2>&1) -contains $OsContainer)
        $ErrorActionPreference = $prevErrorAction3
        if ($exists) { break }
        Start-Sleep -Seconds 1
    }
    Write-PhaseOk "OpenStoryline 容器已创建(健康校验见 Phase 0.7)"
}

# ===============================================================
# Phase 0.7 — OpenStoryline 容器健康校验
#   只看端口会漏:uvicorn 短暂 listen 后 lifespan 抛 pydantic 报错,容器退出。
#   必须看 (a) container status=running  (b) 日志含 startup complete  (c) 端口在听
# ===============================================================
Write-Phase ''
Write-Phase "Phase 0.7:校验 OpenStoryline 健康(容器 running + 日志 startup complete + 端口监听)" 'Cyan'

$healthyDeadline = (Get-Date).AddSeconds(180)
$osHealthy = $false
while ((Get-Date) -lt $healthyDeadline) {
    $ErrorActionPreference = 'SilentlyContinue'
    $osRunning = (@(& docker ps --filter "name=^${OsContainer}$" --filter 'status=running' --format '{{.Names}}' 2>&1) -contains $OsContainer)
    $osLogs = (& docker logs --tail 200 $OsContainer 2>&1) -join "`n"
    $osPort = (& netstat.exe -ano 2>&1 | Select-String ":${OsPort}\s.*LISTENING")
    $ErrorActionPreference = $prevErrorAction3

    if (-not $osRunning) {
        Write-PhaseErr "OpenStoryline 容器已退出(应用启动失败)"
        & docker logs --tail 80 $OsContainer 2>&1
        exit 21
    }
    if ($osLogs -match 'Application startup complete|Uvicorn running on' -and $osPort) {
        $osHealthy = $true
        break
    }
    Start-Sleep -Seconds 2
}
if (-not $osHealthy) {
    Write-PhaseWarn "180s 内未同时满足 startup complete + 端口监听;容器仍在跑,继续(后续 7860 可能仍拒连)"
} else {
    Write-PhaseOk "OpenStoryline 健康(uvicorn + 7860 都就绪)"
}

# ===============================================================
# Phase 1 — preflight.ps1
# ===============================================================
Write-Phase ''
Write-Phase "Phase 1:Pre-flight 自检(6 步)" 'Cyan'

if (-not (Test-Path $Preflight)) {
    Write-PhaseErr "找不到 $Preflight"
    exit 13
}

# 用 & 直接调用,exit code 经 $LASTEXITCODE 回传
& $Preflight *>&1 | Tee-Object -FilePath $LogFile -Append
$preflightExit = $LASTEXITCODE
if ($preflightExit -ne 0) {
    Write-PhaseErr "preflight.ps1 退出码 $preflightExit(对应 Step $(([int]$preflightExit - 9)) 失败)"
    Write-Phase "    请查看上方日志或 README 「失败与恢复」表处理后重跑" 'Yellow'
    exit $preflightExit
}
Write-PhaseOk "preflight.ps1 全部通过"

# ===============================================================
# Phase 2 — run_workflow.py --production
# ===============================================================
Write-Phase ''
Write-Phase "Phase 2:端到端 invoke(run_workflow.py --production)" 'Cyan'

if (-not (Test-Path $VenvPy)) {
    Write-PhaseErr "找不到 .venv\Scripts\python.exe,请确认虚拟环境已就绪"
    exit 14
}
if (-not (Test-Path $RunScript)) {
    Write-PhaseErr "找不到 scripts\run_workflow.py"
    exit 15
}

# 注意:GraphInterrupt(node_06 关卡①)会抛到外层,这是预期行为。
# Python 进程退出码 != 0 不一定代表失败 — 我们只对"非 interrupt 报错"判失败。
$pyOutput = & $VenvPy $RunScript `
    --production `
    --thread-id $ThreadId `
    --initial-state-json $StateJson `
    2>&1 | Tee-Object -FilePath $LogFile -Append

$pyExit = $LASTEXITCODE
$isGraphInterrupt = $false
foreach ($line in $pyOutput) {
    if ($line -match 'GraphInterrupt|checkpoint.*①|"checkpoint":\s*"①"') {
        $isGraphInterrupt = $true
        break
    }
}

if ($pyExit -eq 0) {
    Write-PhaseOk "run_workflow.py 退出码 0(端到端跑完)"
} elseif ($isGraphInterrupt) {
    Write-PhaseOk "run_workflow.py 在 node_06 关卡① 抛 GraphInterrupt(预期行为,需用户在 JianyingPro 手动重排)"
} else {
    Write-PhaseErr "run_workflow.py 退出码 $pyExit,日志最后 20 行:"
    Write-Phase ($pyOutput | Select-Object -Last 20) 'Yellow'
    exit 16
}

# ===============================================================
# Phase 3 — 验收(可选)
# ===============================================================
if ($SkipPhase3) {
    Write-Phase ''
    Write-Phase "Phase 3 已跳过(-SkipPhase3)" 'Yellow'
    Write-Phase "启动完成。日志: $LogFile"
    exit 0
}

Write-Phase ''
Write-Phase "Phase 3:验收记录" 'Cyan'

# 1) Postgres checkpoints 按 thread_id 计数
Write-Phase "  [3.1] checkpoints 表 $ThreadId 记录数:"
$pyCount = & $VenvPy -c @"
import asyncio, os, sys
async def m():
    try:
        import psycopg
    except ImportError:
        print('psycopg 未安装,跳过', file=sys.stderr); sys.exit(0)
    try:
        async with await psycopg.AsyncConnection.connect(os.environ['POSTGRES_URI']) as c:
            async with c.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s", (os.environ['THREAD_ID'],))
                (n,) = await cur.fetchone()
                print(f'checkpoints: {n}')
                await cur.execute("SELECT thread_id, COUNT(*) FROM checkpoints GROUP BY thread_id ORDER BY 2 DESC")
                for r in await cur.fetchall():
                    print(f'  - thread {r[0]}: {r[1]}')
    except Exception as e:
        print(f'查询失败: {e}', file=sys.stderr)
asyncio.run(m())
"@ 2>&1
$pyCount | ForEach-Object { Write-Phase ("    $_") }
Add-Content -Path $LogFile -Value ($pyCount -join "`n") -Encoding UTF8

# 2) heartbeat.txt 时间戳
$hbPath = 'C:\ProgramData\VideoWorkflow\heartbeat.txt'
if (Test-Path $hbPath) {
    $hb = Get-Content $hbPath -Raw
    Write-Phase ("  [3.2] heartbeat.txt: {0}" -f $hb.Trim())
} else {
    Write-Phase "  [3.2] heartbeat.txt 不存在(可能 invoke 未跑到 node_05)" 'Red'
}

# 3) outputs\{thread_id}\
$outDir = Join-Path $RepoRoot ("outputs\{0}" -f $ThreadId)
if (Test-Path $outDir) {
    Write-Phase ("  [3.3] outputs\{0}\ 内容:" -f $ThreadId)
    Get-ChildItem $outDir -Force | Select-Object Name, Length | Format-Table | Out-String -Width 200 | ForEach-Object { Write-Phase ("    $_") }
} else {
    Write-Phase "  [3.3] outputs\$ThreadId\ 未创建" 'Yellow'
}

# 4) drafts\default\ 与 drafts\snapshots\snapshot2\
$defaultDraft = Join-Path $RepoRoot 'drafts\default'
$snap2         = Join-Path $RepoRoot 'drafts\snapshots\snapshot2'
Write-Phase ('  [3.4] drafts\default\ : ' + ($(if (Test-Path (Join-Path $defaultDraft 'draft_content.json')) { 'draft_content.json 存在' } else { '缺失' })))
Write-Phase ('  [3.4] drafts\snapshots\snapshot2\ : ' + ($(if (Test-Path $snap2) { '存在' } else { '缺失' })))

# ===============================================================
# 收尾
# ===============================================================
Write-Phase ''
Write-Phase ('=' * 60) 'Magenta'
Write-Phase "一键启动完成" 'Green'
Write-Phase ("完整日志: {0}" -f $LogFile) 'Cyan'
Write-Phase ('=' * 60) 'Magenta'
exit 0