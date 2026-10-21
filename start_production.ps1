# ============================================================
# auto-video-editor 一键启动(生产全栈)—— 唯一完整工作流启动入口
#
# 该脚本封装 plan.md 的 Phase 0~3:
#   Phase 0    准备视频输入 + 生成 initial-state.json
#   Phase 0.5  Docker Desktop daemon 就绪
#   Phase 0.55 openstoryline 资源/模型校验
#   Phase 0.6/0.7  OpenStoryline Web 生命周期(仅 human 模式)+ 健康检查
#   Phase 1    scripts\preflight.ps1(6 步自检,**只跑一次**)
#   Phase 2    scripts\run_workflow.py --production --skip-preflight
#   Phase 3    验收(checkpoints / heartbeat / drafts / outputs)
#
# 关键设计:
#   - 本脚本是**唯一**完整入口(scripts\start_local.ps1 已删除,勿再引用)
#   - Step 1(Docker 服务)需管理员权限;脚本开头自检,非管理员直接退出
#   - Phase 0.6/0.7 只起**一个** uvicorn;7860 已被健康服务占用 → 复用,
#     不起第二个进程(nodes\node_02 只探测、绝不启动)
#   - 默认 thread-id = video-001;默认源视频来自 jianying-editor skill 演示素材
#   - 全程 UTF-8,避免中文/符号乱码
#   - 每一步写日志到 logs\start_<时间戳>.log;失败保留现场不退
#   - 抛 GraphInterrupt(node_06 关卡①)是预期行为,不视为失败
#   - 日志文件**单写者**:子进程输出顺序 Add-Content,禁止 Tee-Object
#   - -WhatIf 严格零副作用:不建目录、不建日志文件、不起进程
#
# 用法(管理员 Windows PowerShell):
#   .\start_production.ps1                                # 默认参数一键启动
#   .\start_production.ps1 -ThreadId video-002            # 自定义 thread-id
#   .\start_production.ps1 -StorylineMode auto            # 明确走 auto 模式(不起 Web)
#   .\start_production.ps1 -PostgresUri postgresql://u:p@host:5432/db
#   .\start_production.ps1 -SkipPhase3                     # 不跑验收
#   .\start_production.ps1 -WhatIf                         # 仅打印计划(零副作用)
# ============================================================

[CmdletBinding()]
param(
    [string]$ThreadId       = 'video-001',
    [string]$SourceVideo    = 'E:\Documents\kuaishou\FireRed-OpenStoryline\.claude\skills\jianying-editor\assets\video.mp4',
    [string]$InputsDir      = 'E:\Documents\kuaishou\FireRed-OpenStoryline\inputs',
    [string]$InputsFileName = '30s.mp4',
    # Postgres 连接串(唯一真相)。localhost:5432 → 本地托管(起容器 + 对齐角色/库);
    # 其它 host/端口 → 外部模式(不碰本地容器)。
    [string]$PostgresUri    = 'postgresql://postgres:devpass@localhost:5432/langgraph',
    # human = 起 OpenStoryline Web + 走人工关卡(默认);auto = 19 节点确定性图,不起 Web
    [ValidateSet('auto', 'human')]
    [string]$StorylineMode  = 'human',
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
$Timestamp  = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogFile    = Join-Path $LogsDir ("start_${Timestamp}.log")
$Preflight  = Join-Path $RepoRoot 'scripts\preflight.ps1'
$VenvPy     = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$RunScript  = Join-Path $RepoRoot 'scripts\run_workflow.py'
$StateJson  = Join-Path $RepoRoot ("runtime\initial_state_${ThreadId}.json")
$VenvActivate = Join-Path $RepoRoot '.venv\Scripts\Activate.ps1'

# OpenStoryline 本地直起路径(Phase 0.6 / 0.7 用,2026-09 迁移解耦后)
# 仓库根下的 openstoryline/ 子目录(含独立 .venv/ + agent_fastapi.py)。
# 取消对外部 FireRed-OpenStoryline 仓库的引用 — 改为本仓自包含子模块。
$OsDir     = Join-Path $RepoRoot 'openstoryline'
$OsVenvPy  = Join-Path $OsDir '.venv\Scripts\python.exe'
$OsAgent   = Join-Path $OsDir 'agent_fastapi.py'
$OsSrc     = Join-Path $OsDir 'src'
# 端口:Web 7860(走 uvicorn agent_fastapi:app);MCP 8001 保留常量但不再起子进程。
$OsMcpPort = 8001
$OsWebPort = 7860
$OsWebLog  = Join-Path $LogsDir ("openstoryline_web_${Timestamp}.log")
$OsPidFile = Join-Path $LogsDir ("openstoryline_pids_${Timestamp}.json")

# 派生路径
$InputsFull = Join-Path $InputsDir $InputsFileName

# ---------------------------------------------------------------
# URI 脱敏 / 分类(纯计算,无副作用 —— 必须在 WhatIf 之前完成)
# ---------------------------------------------------------------
function Get-MaskedUri {
    # 把 URI 里的密码替换成 ***,避免明文进日志
    param([string]$Uri)

    if ([string]::IsNullOrWhiteSpace($Uri)) { return '<空>' }
    try {
        $u = [System.Uri]$Uri
        if (-not $u.IsAbsoluteUri) { return '<无法解析的 URI>' }
        if ([string]::IsNullOrEmpty($u.UserInfo)) { return $Uri }
        $user = $u.UserInfo.Split(':')[0]
        $builder = New-Object System.Text.StringBuilder
        [void]$builder.Append("$($u.Scheme)://$user`:***@")
        [void]$builder.Append($u.Host)
        if ($u.Port -gt 0) { [void]$builder.Append(":$($u.Port)") }
        [void]$builder.Append($u.AbsolutePath)
        return $builder.ToString()
    } catch {
        return '<无法解析的 URI>'
    }
}

function Get-PostgresTarget {
    # 解析 URI,返回 @{ Kind; Host; Port; Database; User }
    # Kind = 'local-managed'(localhost + 5432) | 'external'(其它)
    # 解析失败(非法 URI / 相对 URI) → 返回 $null
    param([string]$Uri)

    $u = $null
    try {
        $u = [System.Uri]$Uri
        if (-not $u.IsAbsoluteUri) { return $null }
        if ($u.Scheme -ne 'postgresql' -and $u.Scheme -ne 'postgres') { return $null }
        $null = $u.Port
    } catch {
        return $null
    }

    $port = $u.Port
    if ($port -le 0) { $port = 5432 }
    $info = @{
        Kind     = 'external'
        Host     = $u.Host
        Port     = $port
        Database = $u.AbsolutePath.TrimStart('/')
        User     = ($u.UserInfo.Split(':')[0])
    }
    if ([string]::IsNullOrWhiteSpace($info['Database'])) { return $null }
    if ([string]::IsNullOrWhiteSpace($info['User']))     { return $null }

    $localHosts = @('localhost', '127.0.0.1', '::1', '[::1]')
    if ($localHosts -contains $u.Host -and $port -eq 5432) {
        $info['Kind'] = 'local-managed'
    }
    return $info
}

$target = Get-PostgresTarget $PostgresUri
if ($null -eq $target) {
    Write-Host "✗ -PostgresUri 非法:$(Get-MaskedUri $PostgresUri)" -ForegroundColor Red
    Write-Host '  期望格式:postgresql://<user>:<password>@<host>:<port>/<database>' -ForegroundColor Yellow
    exit 19
}
$maskedUri = Get-MaskedUri $PostgresUri

# ---------------------------------------------------------------
# Web 健康探测(纯读,无副作用)
# ---------------------------------------------------------------
function Test-OsWebHealthy {
    # GET <url>,200 即健康
    param([string]$Url, [int]$TimeoutSec = 3)
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Test-OsPortOpen {
    # 端口是否在听
    param([int]$Port)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    $open = (Test-NetConnection -ComputerName '127.0.0.1' -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue) -eq $true
    $ErrorActionPreference = $prev
    return $open
}

# ---------------------------------------------------------------
# WhatIf:仅打印计划,**零副作用**
#   必须在"建 logs/ 目录 / 建日志文件 / banner / 生成 initial_state JSON"之前。
# ---------------------------------------------------------------
if ($WhatIf) {
    Write-Host '[WhatIf] 不会执行任何修改,以下为计划:' -ForegroundColor Yellow
    Write-Host ''
    Write-Host "  ThreadId      : $ThreadId" -ForegroundColor Cyan
    Write-Host "  StorylineMode : $StorylineMode" -ForegroundColor Cyan
    Write-Host "  Postgres URI  : $maskedUri" -ForegroundColor Cyan
    Write-Host "  URI 分类      : $(if ($target.Kind -eq 'local-managed') { '本地托管(起 edb-postgres16 容器 + 对齐角色/库)' } else { "外部(host=$($target.Host):$($target.Port),不碰本地容器)" })" -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  Phase 0   : 复制视频 + 生成 initial_state JSON' -ForegroundColor Cyan
    Write-Host '  Phase 0.5 : 确保 Docker Desktop daemon 在线(只服务 Postgres)' -ForegroundColor Cyan
    Write-Host '  Phase 0.55: 校验 openstoryline 资源/模型(.storyline/models + resource/)' -ForegroundColor Cyan
    if ($StorylineMode -eq 'auto') {
        Write-Host '  Phase 0.6/0.7: 跳过(auto 模式不起 OpenStoryline Web)' -ForegroundColor Cyan
    } else {
        Write-Host "  Phase 0.6/0.7: OpenStoryline Web(:$OsWebPort)—— 端口在听且 HTTP 200 → 复用;端口空闲 → 起一个 uvicorn;端口占用但 HTTP≠200 → 报冲突退出(23)" -ForegroundColor Cyan
        Write-Host "              写 PID 文件:$OsPidFile(MCP 端口 $OsMcpPort 不再起子进程,仅作常量保留)" -ForegroundColor Cyan
    }
    Write-Host "  Phase 1   : $Preflight(完整预检,**本次运行只执行一次**)" -ForegroundColor Cyan
    Write-Host "  Phase 2   : $VenvPy $RunScript --production --skip-preflight --thread-id $ThreadId --initial-state-json $StateJson" -ForegroundColor Cyan
    if (-not $SkipPhase3) {
        Write-Host '  Phase 3   : checkpoints SQL(带 THREAD_ID)+ heartbeat + outputs/drafts 列目录' -ForegroundColor Cyan
    } else {
        Write-Host '  Phase 3   : [跳过 -SkipPhase3]' -ForegroundColor Cyan
    }
    exit 0
}

# ---------------------------------------------------------------
# 从这里开始才允许有副作用
# ---------------------------------------------------------------
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
}

# ---------------------------------------------------------------
# 日志函数:同时写控制台 + 日志文件
#   日志文件**单写者**:子进程输出也走 Add-Content 顺序写,
#   禁止 Tee-Object(会与 Add-Content 抢同一文件句柄 → Windows 文件锁 → 脚本崩)。
# ---------------------------------------------------------------
function Write-Phase {
    param([string]$Text, [string]$Color = 'Cyan')
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $Text"
    Write-Host $line -ForegroundColor $Color
    Add-Content -Path $LogFile -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}

function Write-PhaseOk   { param([string]$Text) Write-Phase "✓ $Text" 'Green' }
function Write-PhaseWarn { param([string]$Text) Write-Phase "! $Text" 'Yellow' }
function Write-PhaseErr  { param([string]$Text) Write-Phase "✗ $Text" 'Red' }

# 子进程输出:顺序转发到控制台 + 日志文件(单写者,不与 Tee-Object 并发)
function Write-SubProcessOutput {
    param([object[]]$Lines, [string]$Prefix = '  ')
    if ($null -eq $Lines) { return }
    foreach ($l in $Lines) {
        $t = "$l"
        Write-Host "$Prefix$t" -ForegroundColor DarkGray
        Add-Content -Path $LogFile -Value "$Prefix$t" -Encoding UTF8 -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------
# 环境变量注入 + 还原
#   POSTGRES_URI / STORYLINE_MODE / THREAD_ID 由本脚本显式注入给子进程,
#   不受调用方 shell 残留污染;脚本结束(finally)时还原原值。
# ---------------------------------------------------------------
$envBackup = @{}
foreach ($name in @(
    'POSTGRES_URI',
    'STORYLINE_MODE',
    'THREAD_ID',
    'PYTHONUNBUFFERED',
    'AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START'
)) {
    $envBackup[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

# ---------------------------------------------------------------
# 顶部 banner
# ---------------------------------------------------------------
Write-Phase ('=' * 60) 'Magenta'
Write-Phase "auto-video-editor 一键启动(生产全栈)" 'Magenta'
Write-Phase ("ThreadId      : {0}" -f $ThreadId) 'Magenta'
Write-Phase ("StorylineMode : {0}" -f $StorylineMode) 'Magenta'
Write-Phase ("Postgres URI  : {0}" -f $maskedUri) 'Magenta'
Write-Phase ("日志路径      : {0}" -f $LogFile) 'Magenta'
Write-Phase ("OsDir (OpenStoryline 子系统) : {0}" -f $OsDir) 'Magenta'
Write-Phase ('=' * 60) 'Magenta'

try {
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
    # Phase 0.55 — 校验 openstoryline 资源/模型(2026-09 迁移后新增)
    #   resource/ ~526 MB; .storyline/models/ ~116 MB; 入 .gitignore,需手工拉。
    #   auto 模式也保留本步(auto 依赖 vendored venv 的 ASR)。
    # ===============================================================
    Write-Phase ''
    Write-Phase "Phase 0.55:校验 openstoryline 资源/模型" 'Cyan'

    $OsResource = Join-Path $OsDir 'resource'
    $OsModels   = Join-Path $OsDir '.storyline\models'

    $needDownload = $false
    if (-not (Test-Path $OsResource) -or -not (Get-ChildItem $OsResource -ErrorAction SilentlyContinue)) {
        Write-PhaseWarn "openstoryline/resource/ 缺失或为空"
        $needDownload = $true
    }
    if (-not (Test-Path $OsModels) -or -not (Get-ChildItem $OsModels -ErrorAction SilentlyContinue)) {
        Write-PhaseWarn "openstoryline/.storyline/models/ 缺失或为空"
        $needDownload = $true
    }
    if ($needDownload) {
        Write-Phase "    请运行以下命令手动下载(总大小约 642 MB):" 'Yellow'
        Write-Phase "      .\openstoryline\scripts\download_resources.ps1" 'Yellow'
        Write-Phase "      .\openstoryline\scripts\download_models.ps1" 'Yellow'
        Write-Phase "    或一键: .\openstoryline\scripts\install.ps1" 'Yellow'
        Write-Phase "    完成后重跑 start_production.ps1" 'Yellow'
        exit 26
    }
    Write-PhaseOk "openstoryline 资源/模型已就绪"

    # ===============================================================
    # Phase 0.6 / 0.7 — OpenStoryline Web 生命周期(仅 human 模式)
    #   方案 A:生产脚本管 Web 生命周期;nodes\node_02 只探测复用,绝不启动。
    #   三分支:
    #     复用     —— 7860 在听 且 GET / 返回 200 → reused=true,不起进程
    #     端口冲突 —— 7860 在听 但 HTTP ≠ 200      → exit 23(不杀未知进程)
    #     端口空闲 —— 起一次 uvicorn,等 PID 存活 + 端口在听 + HTTP 200
    # ===============================================================
    $osPidReused = $false
    $webPyPid    = $null
    $webProc     = $null

    if ($StorylineMode -eq 'auto') {
        Write-Phase ''
        Write-Phase "Phase 0.6/0.7:跳过(auto 模式不依赖 OpenStoryline Web UI)" 'Yellow'
    } else {
        Write-Phase ''
        Write-Phase "Phase 0.6:OpenStoryline Web(:$OsWebPort,MCP 链路已迁移解耦)" 'Cyan'

        # 路径校验(venv / 入口文件 / 包)
        if (-not (Test-Path $OsVenvPy)) {
            Write-PhaseErr "找不到 $OsVenvPy"
            Write-Phase "    请先跑 openstoryline\scripts\install.ps1 建 venv 并装依赖" 'Yellow'
            exit 18
        }
        if (-not (Test-Path $OsAgent)) {
            Write-PhaseErr "找不到 $OsAgent(请确认 openstoryline/ 子目录代码搬运完整)" 'Yellow'
            exit 18
        }
        if (-not (Test-Path (Join-Path $OsSrc 'open_storyline\__init__.py'))) {
            Write-PhaseErr "找不到 $OsSrc\open_storyline\__init__.py" 'Yellow'
            exit 18
        }

        $webUrl     = "http://127.0.0.1:$OsWebPort/"
        $webPortOpen = Test-OsPortOpen $OsWebPort

        if ($webPortOpen) {
            if (Test-OsWebHealthy -Url $webUrl -TimeoutSec 5) {
                # 分支 1:复用
                $osPidReused = $true
                Write-PhaseOk "OpenStoryline Web 已在运行且健康(:$OsWebPort),复用现有进程(不启新进程)"
                $pidObj = [ordered]@{
                    timestamp = $Timestamp
                    reused    = $true
                    os_dir    = $OsDir
                    web_pid   = $null
                    web_cmd_pid = $null
                    web_log   = $null
                }
                $pidObj | ConvertTo-Json | Set-Content -Path $OsPidFile -Encoding UTF8
                Write-PhaseOk ("PID 文件(reused): {0}" -f $OsPidFile)
            } else {
                # 分支 2:端口冲突 —— fail-closed,不杀未知进程
                Write-PhaseErr "端口 $OsWebPort 已被占用,但 GET $webUrl 不返回 200(端口冲突)"
                Write-Phase "    本脚本不会杀任何未确认归属的进程。请手动处理占用该端口的进程后重跑。" 'Yellow'
                exit 23
            }
        } else {
            # 分支 3:端口空闲 —— 起一次 uvicorn
            # 孤儿清理:扫本仓库之前的 PID 文件,把仍活着的 web python 进程 kill
            Get-ChildItem -Path $LogsDir -Filter 'openstoryline_pids_*.json' -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -ne $OsPidFile } |
                Sort-Object LastWriteTime -Descending |
                ForEach-Object {
                    try {
                        $prev = Get-Content -Path $_.FullName -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json -ErrorAction SilentlyContinue
                        if ($null -ne $prev -and $prev.reused -eq $true) { return }
                        $pidVal = $prev.web_pid
                        if ($null -ne $pidVal -and $pidVal -gt 0) {
                            $alive = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
                            if ($null -ne $alive) {
                                Write-Phase ("  杀孤儿 web PID={0}" -f $pidVal)
                                Stop-Process -Id $pidVal -Force -ErrorAction SilentlyContinue
                            }
                        }
                    } catch { }
                }

            # 强制 Python 不缓冲(便于日志实时落盘)
            $env:PYTHONUNBUFFERED = '1'

            Write-Phase "  启动 Web 服务(uvicorn agent_fastapi:app --host 127.0.0.1 --port $OsWebPort)..."
            $webBatPath = Join-Path $env:TEMP "openstoryline_web_${Timestamp}.bat"
            $webBatContent = @"
@echo off
set PYTHONUNBUFFERED=1
"$OsVenvPy" -m uvicorn agent_fastapi:app --host 127.0.0.1 --port $OsWebPort 1> "$OsWebLog" 2>&1
"@
            [System.IO.File]::WriteAllText($webBatPath, $webBatContent, [System.Text.Encoding]::ASCII)
            $webProc = Start-Process `
                -FilePath 'cmd.exe' `
                -ArgumentList "/c `"$webBatPath`"" `
                -WorkingDirectory $OsDir `
                -WindowStyle Hidden `
                -PassThru
            $webPyPid = $null
            $webDeadline = (Get-Date).AddSeconds(8)
            while ((Get-Date) -lt $webDeadline -and $null -eq $webPyPid) {
                Start-Sleep -Milliseconds 500
                try {
                    $child = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($webProc.Id)" -ErrorAction SilentlyContinue |
                        Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'agent_fastapi:app' } |
                        Select-Object -First 1
                    if ($null -ne $child) { $webPyPid = [int]$child.ProcessId }
                } catch { }
            }
            if ($null -eq $webPyPid) {
                Write-PhaseWarn "未在 8s 内抓到 python.exe 子进程,先用 cmd.exe PID={0} 兜底" -f $webProc.Id
                $webPyPid = $webProc.Id
            }
            Write-Phase ("  Web cmd PID={0} -> python PID={1}" -f $webProc.Id, $webPyPid)

            # 写 PID 文件(供 stop_production.ps1 读)
            $pidObj = [ordered]@{
                timestamp   = $Timestamp
                reused      = $false
                os_dir      = $OsDir
                web_pid     = $webPyPid
                web_cmd_pid = $webProc.Id
                web_log     = $OsWebLog
            }
            $pidObj | ConvertTo-Json | Set-Content -Path $OsPidFile -Encoding UTF8
            Write-PhaseOk ("PID 文件: {0}" -f $OsPidFile)
        }

        # -----------------------------------------------------------
        # 健康检查(本次启动:PID 存活 + 端口在听 + HTTP 200;复用:端口 + HTTP 200)
        # -----------------------------------------------------------
        Write-Phase ''
        Write-Phase "Phase 0.7:校验 OpenStoryline Web 健康" 'Cyan'

        $healthyDeadline = (Get-Date).AddSeconds(180)
        $osHealthy = $false
        while ((Get-Date) -lt $healthyDeadline) {
            if (-not $osPidReused) {
                $webAlive = ($null -ne (Get-Process -Id $webPyPid -ErrorAction SilentlyContinue))
                if (-not $webAlive) {
                    Write-PhaseErr "Web 进程 (PID={0}) 已退出,日志尾:" -f $webPyPid
                    if (Test-Path $OsWebLog) {
                        Get-Content $OsWebLog -Tail 80 -ErrorAction SilentlyContinue |
                            ForEach-Object { Write-Phase ("    $_") }
                    }
                    exit 21
                }
            }

            $portsOk = Test-OsPortOpen $OsWebPort
            $httpOk  = Test-OsWebHealthy -Url $webUrl -TimeoutSec 3
            if ($portsOk -and $httpOk) {
                $osHealthy = $true
                break
            }
            Start-Sleep -Seconds 2
        }

        if (-not $osHealthy) {
            Write-PhaseErr "180s 内 OpenStoryline Web 未就绪"
            Write-Phase ("    端口 : {0} 在听 = {1}" -f $OsWebPort, (Test-OsPortOpen $OsWebPort)) 'Yellow'
            Write-Phase ("    URL  : {0}" -f $webUrl) 'Yellow'
            Write-Phase ("    复用 : {0}" -f $osPidReused) 'Yellow'
            if (-not $osPidReused) {
                Write-Phase ("    进程 : PID={0} 存活 = {1}" -f $webPyPid, ($null -ne (Get-Process -Id $webPyPid -ErrorAction SilentlyContinue))) 'Yellow'
                if (Test-Path $OsWebLog) {
                    Write-Phase '    最近日志尾:' 'Yellow'
                    Get-Content $OsWebLog -Tail 40 -ErrorAction SilentlyContinue |
                        ForEach-Object { Write-Phase ("    $_") }
                }
            }
            Write-Phase '    不启动工作流(未就绪的服务不参与生产)。' 'Yellow'
            exit 22
        }
        Write-PhaseOk "OpenStoryline Web 健康(:$OsWebPort 就绪)"
    }

    # ===============================================================
    # 环境变量注入(preflight 与 Python 子进程都会继承)
    # ===============================================================
    Write-Phase ''
    Write-Phase "注入环境变量(脚本结束时还原)" 'Cyan'
    $env:POSTGRES_URI  = $PostgresUri
    $env:STORYLINE_MODE = $StorylineMode
    $env:THREAD_ID      = $ThreadId
    # 外部 URI 模式:Python 侧预检也不该去起本地容器
    if ($target.Kind -ne 'local-managed') {
        $env:AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START = '1'
        Write-Phase "  AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START=1(外部 Postgres 模式)"
    }
    Write-Phase ("  POSTGRES_URI   = {0}" -f $maskedUri)
    Write-Phase ("  STORYLINE_MODE = {0}" -f $StorylineMode)
    Write-Phase ("  THREAD_ID      = {0}" -f $ThreadId)

    # ===============================================================
    # Phase 1 — preflight.ps1(本次运行**唯一一次**完整预检)
    # ===============================================================
    Write-Phase ''
    Write-Phase "Phase 1:Pre-flight 自检(6 步,本次运行只执行一次)" 'Cyan'

    if (-not (Test-Path $Preflight)) {
        Write-PhaseErr "找不到 $Preflight"
        exit 13
    }

    # 用 & 直接调用,exit code 经 $LASTEXITCODE 回传。
    # 顺序写日志(不用 Tee-Object):见 Write-Phase 的「单写者」说明。
    # 同时保持**实时**输出(preflight 里的 docker pull 可能耗时较久)。
    & $Preflight -PostgresUri $PostgresUri *>&1 | ForEach-Object {
        $t = "$_"
        Write-Host "  $t" -ForegroundColor DarkGray
        Add-Content -Path $LogFile -Value "  $t" -Encoding UTF8 -ErrorAction SilentlyContinue
    }
    $preflightExit = $LASTEXITCODE
    if ($preflightExit -ne 0) {
        Write-PhaseErr "preflight.ps1 退出码 $preflightExit"
        Write-Phase "    请查看上方日志或 README 「失败与恢复」表处理后重跑" 'Yellow'
        exit $preflightExit
    }
    Write-PhaseOk "preflight.ps1 全部通过"

    # ===============================================================
    # Phase 2 — run_workflow.py --production --skip-preflight
    #   --skip-preflight:预检已在 Phase 1 做过,这里不能再跑第二遍。
    # ===============================================================
    Write-Phase ''
    Write-Phase "Phase 2:端到端 invoke(run_workflow.py --production --skip-preflight)" 'Cyan'

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
    # 顺序写日志(单写者)+ 实时输出 + 保留全部行用于诊断。
    $pyOutput = @()
    & $VenvPy $RunScript `
        --production `
        --skip-preflight `
        --thread-id $ThreadId `
        --initial-state-json $StateJson `
        2>&1 | ForEach-Object {
        $t = "$_"
        $pyOutput += $t
        Write-Host "  $t" -ForegroundColor DarkGray
        Add-Content -Path $LogFile -Value "  $t" -Encoding UTF8 -ErrorAction SilentlyContinue
    }
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
    } elseif ($pyExit -eq 3) {
        # run_workflow.py 判定 node_02 未就绪 → 工作流提前终止
        Write-PhaseErr "run_workflow.py 退出码 3:OpenStoryline 未就绪,工作流提前终止"
        Write-Phase ($pyOutput | Select-Object -Last 20) 'Yellow'
        exit 24
    } elseif ($isGraphInterrupt) {
        Write-PhaseOk "run_workflow.py 在 node_06 关卡① 抛 GraphInterrupt(预期行为,需用户在 JianyingPro 手动重排)"
    } else {
        Write-PhaseErr "run_workflow.py 退出码 $pyExit,日志最后 20 行:"
        Write-Phase ($pyOutput | Select-Object -Last 20) 'Yellow'
        exit 27
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
        print('psycopg 未安装,未验收(可选依赖)')
        sys.exit(0)
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
        sys.exit(1)
asyncio.run(m())
"@ 2>&1
    $countExit = $LASTEXITCODE
    Write-SubProcessOutput -Lines $pyCount -Prefix '    '
    if ($countExit -ne 0) {
        Write-PhaseErr "验收未完成:checkpoint 查询失败(退出码 $countExit)"
        Write-Phase "    请检查 Postgres 连通性与 $env:POSTGRES_URI(脱敏: $maskedUri)" 'Yellow'
        exit 25
    }

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
}
finally {
    # 还原环境变量(不污染调用方 PowerShell 会话)
    foreach ($name in $envBackup.Keys) {
        $old = $envBackup[$name]
        if ($null -eq $old) {
            [Environment]::SetEnvironmentVariable($name, $null, 'Process')
        } else {
            [Environment]::SetEnvironmentVariable($name, $old, 'Process')
        }
    }
}
