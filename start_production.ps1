# ============================================================
# auto-video-editor 一键启动(生产全栈)—— 唯一完整工作流启动入口
#
# 2026-11-04 单流水线架构收敛(阶段5):OpenStoryline Web 生命周期
# (Phase 0.6/0.7 的 agent_fastapi:app)退役,7860 端口改为承载**只读监控服务**
# monitor_fastapi.py;human 模式参数(STORYLINE_MODE)随 human 模式链删除。
#
# 该脚本封装的 Phase:
#   Phase 0    准备视频输入 + 生成 initial-state.json
#   Phase 0.5  Docker Desktop daemon 就绪
#   Phase 0.58 scripts\preflight.ps1(6 步自检,**只跑一次**)
#              —— 排在监控服务启动之前:preflight Step 4-6 负责拉起 Postgres
#              容器 + 连通性 + checkpoint schema(2026-11-01 故障教训:数据库
#              必须先于 7860 服务在线)。
#   Phase 0.6/0.7  只读监控服务(monitor_fastapi:app,主 .venv)生命周期 + 健康检查
#   Phase 2    scripts\run_workflow.py --production --skip-preflight
#   Phase 3    验收(checkpoints / heartbeat / drafts / outputs)
#
# 关键设计:
#   - 本脚本是**唯一**完整入口(scripts\start_local.ps1 已删除,勿再引用)
#   - Step 1(Docker 服务)需管理员权限;脚本开头自检,非管理员直接退出
#   - Phase 0.6/0.7 只起**一个** uvicorn;7860 已被健康服务占用时,仅当 PID 文件
#     证明归属且配置指纹一致才复用;本项目所有但指纹不一致 → 受控重启;
#     归属不明 → fail-closed exit 23(监控只读,绝不杀未知进程)
#   - 默认 thread-id = video-001;默认源视频来自 jianying-editor skill 演示素材
#   - 全程 UTF-8,避免中文/符号乱码
#   - 每一步写日志到 logs\start_<时间戳>.log;失败保留现场不退
#   - 工作流停在人工关卡(①/②/③)是预期行为:run_workflow.py 退出码 0,
#     用 agent_gate status 或 scripts/resume_workflow.py --dry-run 查看挂起关卡
#   - 日志文件**单写者**:子进程输出顺序 Add-Content,禁止 Tee-Object
#   - -WhatIf 严格零副作用:不建目录、不建日志文件、不起进程
#
# 用法(管理员 Windows PowerShell):
#   .\start_production.ps1                                # 默认参数一键启动
#   .\start_production.ps1 -ThreadId video-002            # 自定义 thread-id
#   .\start_production.ps1 -PostgresUri postgresql://u:p@host:5432/db
#   .\start_production.ps1 -SkipPhase3                     # 不跑验收
#   .\start_production.ps1 -WhatIf                         # 仅打印计划(零副作用)
# ============================================================

[CmdletBinding()]
param(
    [string]$ThreadId       = 'video-001',
    # 默认素材/输入目录改为仓库内 inputs\(2026-11-04 单流水线收敛后不再指向 FireRed-OpenStoryline)
    [string]$SourceVideo    = (Join-Path $PSScriptRoot 'inputs\30s.mp4'),
    [string]$InputsDir      = (Join-Path $PSScriptRoot 'inputs'),
    [string]$InputsFileName = '30s.mp4',
    # Postgres 连接串(唯一真相)。localhost:5432 → 本地托管(起容器 + 对齐角色/库);
    # 其它 host/端口 → 外部模式(不碰本地容器)。
    [string]$PostgresUri    = 'postgresql://postgres:devpass@localhost:5432/video_workflow',
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

# 只读监控服务(Phase 0.6 / 0.7 用;2026-11-04 阶段5 起接管 7860)
# 依赖主 .venv(fastapi/uvicorn 已声明于 requirements.txt + pyproject.toml);
# .venv-openstoryline/ 已随旧聊天运行时退役,本脚本不再引用。
$MonitorScript = Join-Path $RepoRoot 'monitor_fastapi.py'
$MonitorPort   = 7860
$MonitorLog    = Join-Path $LogsDir ("monitor_${Timestamp}.log")
$MonitorPidFile = Join-Path $LogsDir ("monitor_pids_${Timestamp}.json")

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
function Test-WebHealthy {
    # GET <url>,200 即健康
    param([string]$Url, [int]$TimeoutSec = 3)
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Test-PortOpen {
    # 端口是否在听
    param([int]$Port)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    $open = (Test-NetConnection -ComputerName '127.0.0.1' -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue) -eq $true
    $ErrorActionPreference = $prev
    return $open
}

function Test-TcpPortOpen {
    # 快速 TCP 探测(TcpClient 异步连接 + 显式超时)。
    # 比 Test-NetConnection 快;SYN 被丢弃(防火墙/代理)时也能在超时内返回 $false。
    param([string]$Hostname, [int]$Port, [int]$TimeoutMs = 1500)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($Hostname, $Port)
        if ($task.Wait($TimeoutMs) -and $client.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-WebConfigFingerprint {
    # C-T2(执行设计 §6.6.C 第一阶段):本次启动期望的服务配置指纹。
    # sha256("<PostgresUri>") 前 16 位十六进制(阶段5:STORYLINE_MODE 已删,
    # 监控服务配置只随 Postgres DSN 变化)。
    # 单向哈希 —— 不泄密码;只用于比对「上次启动服务的配置」与「这次想用的配置」
    # 是否一致,解决「HTTP 200 健康证明不了配置正确」的复用误判(§4.2 P0)。
    param([string]$Uri)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes("$Uri")
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLower().Substring(0, 16)
    } finally {
        $sha.Dispose()
    }
}

function Find-OwningWebRecord {
    # C-T2:在本仓库 logs\ 的历史 PID 文件里,找「仍存活的、由本项目启动的
    # 监控服务」的最新一条记录。归属口径:reused=false 且 monitor_pid>0 且该
    # PID 仍存活 —— reused=true 的记录本来就不记 PID(复用没有进程所有权),
    # 不能作归属证据。找不到归属记录返回 $null(调用方按未知进程 fail-closed)。
    $candidates = Get-ChildItem -Path $LogsDir -Filter 'monitor_pids_*.json' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne $MonitorPidFile } |
        Sort-Object LastWriteTime -Descending
    foreach ($f in $candidates) {
        try {
            $prev = Get-Content -Path $f.FullName -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json -ErrorAction SilentlyContinue
        } catch { continue }
        if ($null -eq $prev -or $prev.reused -eq $true) { continue }
        $pidVal = $prev.web_pid
        if ($null -eq $pidVal -or [int]$pidVal -le 0) { continue }
        if ($null -eq (Get-Process -Id $pidVal -ErrorAction SilentlyContinue)) { continue }
        # PID 复用防御:进程活着 ≠ 还是当初那个监控服务。命令行里没有
        # monitor_fastapi 的(PID 已被其它进程复用)不算归属证据 ——
        # 宁可 fail-closed 也不误杀。
        try {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pidVal" -ErrorAction SilentlyContinue
            if ($null -ne $proc -and $proc.CommandLine -and $proc.CommandLine -notmatch 'monitor_fastapi') { continue }
        } catch { }   # 命令行查不到(权限等)→ 维持 PID 文件归属口径
        return @{
            web_pid            = [int]$pidVal
            web_cmd_pid        = $prev.web_cmd_pid
            config_fingerprint = $prev.config_fingerprint
            source_file        = Split-Path $f.FullName -Leaf
        }
    }
    return $null
}

# ---------------------------------------------------------------
# WhatIf:仅打印计划,**零副作用**
#   必须在"建 logs/ 目录 / 建日志文件 / banner / 生成 initial_state JSON"之前。
# ---------------------------------------------------------------
if ($WhatIf) {
    Write-Host '[WhatIf] 不会执行任何修改,以下为计划:' -ForegroundColor Yellow
    Write-Host ''
    Write-Host "  ThreadId      : $ThreadId" -ForegroundColor Cyan
    Write-Host "  Postgres URI  : $maskedUri" -ForegroundColor Cyan
    Write-Host "  URI 分类      : $(if ($target.Kind -eq 'local-managed') { '本地托管(起 edb-postgres16 容器 + 对齐角色/库)' } else { "外部(host=$($target.Host):$($target.Port),不碰本地容器)" })" -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  Phase 0   : 复制视频 + 生成 initial_state JSON' -ForegroundColor Cyan
    Write-Host '  Phase 0.5 : 确保 Docker Desktop daemon 在线(只服务 Postgres)' -ForegroundColor Cyan
    Write-Host "  Phase 0.58: $Preflight(完整预检,先拉起 Postgres 再起监控服务;**本次运行只执行一次**)" -ForegroundColor Cyan
    Write-Host "  Phase 0.6/0.7: 只读监控服务 monitor_fastapi(:$MonitorPort,主 .venv)—— 端口在听且健康 → 按 PID 归属+配置指纹判定:一致→复用;本项目所有但不一致→受控重启;归属不明→报冲突退出(23);端口空闲 → 起一个 uvicorn;占用但 HTTP≠200 → 报冲突退出(23)" -ForegroundColor Cyan
    Write-Host "              写 PID 文件:$MonitorPidFile" -ForegroundColor Cyan
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
#   POSTGRES_URI / THREAD_ID 由本脚本显式注入给子进程,
#   不受调用方 shell 残留污染;脚本结束(finally)时还原原值。
#   (STORYLINE_MODE 已随 human 模式链删除,阶段5 起不再注入。)
#   ⚠️ 注入必须发生在任何 Start-Process 之前(含 Phase 0.5 的 Docker Desktop
#   与 Phase 0.6 的 Uvicorn):子进程只在创建瞬间复制父进程的环境快照,
#   父进程之后补设的变量不会到达已运行的子进程 —— 2026-10-31 worker
#   "no password supplied" 故障的 P0 根因,就是注入被放在 Phase 0.7 之后,
#   先起的 Uvicorn → MCode → MCP → Worker 整条进程链都拿不到本次
#   POSTGRES_URI。
# ---------------------------------------------------------------
$envBackup = @{}
foreach ($name in @(
    'POSTGRES_URI',
    'AGENT_GATE_POSTGRES_URI',   # agent_gate 测试隔离变量,优先级高于 POSTGRES_URI;生产启动前备份并清除,防遗留值覆盖生产 DSN
    'APP_ENV',                   # 统一环境标识(B-T4);本脚本钉 production,resolver 据此拒绝测试库变量
    'AGENT_GATE_CKPT_BACKEND',   # agent_gate checkpoint 后端;本脚本钉 postgres(显式声明,防残留非法值)
    'STORYLINE_MODE',            # legacy:human 模式已删;仍备份+清除,防调用方残留值触发 config import 期 fail-fast
    'THREAD_ID',
    'PYTHONUNBUFFERED',
    'PYTHONIOENCODING',
    'AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START'
)) {
    $envBackup[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

# 统一注入(早于所有子进程创建):监控服务 → run_workflow 整条进程链
# 都从这里继承本次启动的 DSN。
$env:POSTGRES_URI  = $PostgresUri
Remove-Item Env:AGENT_GATE_POSTGRES_URI -ErrorAction SilentlyContinue
# B-T4/C-T1:生产标记。统一解析层(db.postgres_config)在 APP_ENV=production
# 且存在 AGENT_GATE_POSTGRES_URI 时会直接抛配置错误 —— 上一行已清除该变量,
# 这里钉上标记做双保险:万一后续链路再混入测试库变量,Worker 连接前就 fail-fast。
$env:APP_ENV = 'production'
# C-T1:钉住 checkpoint 后端为 postgres(显式声明,防残留的空值/非法值)。
$env:AGENT_GATE_CKPT_BACKEND = 'postgres'
# STORYLINE_MODE 显式清除(human 模式已删;防调用方残留值让 config import 即报错)
Remove-Item Env:STORYLINE_MODE -ErrorAction SilentlyContinue
$env:THREAD_ID      = $ThreadId
# Windows 下 python stdout 默认 GBK,PowerShell 按 UTF-8 解码 → 中文乱码。
# 显式钉 UTF-8,让 Phase 2/Phase 3 的 Python 子进程输出在控制台和日志里都可读。
$env:PYTHONIOENCODING = 'utf-8'
# 强制 Python 不缓冲(便于 Web 日志实时落盘)
$env:PYTHONUNBUFFERED = '1'
# 外部 URI 模式:Python 侧预检也不该去起本地容器
if ($target.Kind -ne 'local-managed') {
    $env:AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START = '1'
    Write-Phase "  AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START=1(外部 Postgres 模式)"
}
Write-Phase ("注入环境变量(所有子进程启动之前;脚本结束时还原): POSTGRES_URI = {0}, APP_ENV = {1}, AGENT_GATE_CKPT_BACKEND = {2}, THREAD_ID = {3}, PYTHONIOENCODING = {4}(STORYLINE_MODE 已清除:human 模式已删)" -f $maskedUri, $env:APP_ENV, $env:AGENT_GATE_CKPT_BACKEND, $ThreadId, $env:PYTHONIOENCODING)

# ---------------------------------------------------------------
# 顶部 banner
# ---------------------------------------------------------------
Write-Phase ('=' * 60) 'Magenta'
Write-Phase "auto-video-editor 一键启动(生产全栈;auto-only 单流水线)" 'Magenta'
Write-Phase ("ThreadId      : {0}" -f $ThreadId) 'Magenta'
Write-Phase ("Postgres URI  : {0}" -f $maskedUri) 'Magenta'
Write-Phase ("日志路径      : {0}" -f $LogFile) 'Magenta'
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

    # (Phase 0.55 已删除 —— 2026-11-04 阶段5,R-16:仓库根 resource/ 与
    #   .storyline/models 只有旧聊天运行时/vendored ASR 消费,阶段2/3 已删;
    #   auto 链资源 storyline_capabilities/resource/*.yaml 已入库,无需下载校验。)

    # ===============================================================
    # Phase 0.58 — preflight.ps1(本次运行**唯一一次**完整预检)
    #   2026-11-01 前移到 7860 服务启动之前(原 Phase 1,排在 Phase 0.7 之后):
    #   preflight Step 4-6 负责拉起 Postgres 容器 + 连通性 + checkpoint schema。
    #   旧顺序下容器一旦未运行,7860 服务永远不健康 → 在 Phase 0.7 exit 22,本块
    #   永远执行不到(2026-11-01 01:16 线上故障根因)。前移后:数据库先于
    #   7860 服务在线;preflight 失败也会带着精确的 Step 退出码(10-16/20)直接中止。
    #   注:POSTGRES_URI 等环境变量已在脚本头部(envBackup 段)、任何
    #   Start-Process 之前注入,preflight 与 Phase 2/3 子进程直接继承本次值。
    # ===============================================================
    Write-Phase ''
    Write-Phase "Phase 0.58:Pre-flight 自检(6 步:先拉起 Postgres,再起监控服务;本次运行只执行一次)" 'Cyan'

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
    Write-PhaseOk "preflight.ps1 全部通过(Postgres 已在线,checkpoint schema 已就绪)"

    # ===============================================================
    # Phase 0.6 / 0.7 — 只读监控服务生命周期(monitor_fastapi,主 .venv)
    #   2026-11-04 阶段5:7860 从 OpenStoryline Web(agent_fastapi:app +
    #   .venv-openstoryline)切换为**只读**监控(monitor_fastapi:app,主 .venv)。
    #   四分支(沿用 C-T2 归属+指纹纪律,不因迁移放松):
    #     复用     —— 7860 在听 + HTTP 200 + PID 文件证明归属 + 配置指纹一致
    #     受控重启 —— 归本项目所有,但指纹不一致 → 停本项目进程后重起
    #     端口冲突 —— 7860 在听 但 HTTP != 200,或健康但归属不明 → exit 23(不杀未知进程)
    #     端口空闲 —— 起一次 uvicorn,等 PID 存活 + 端口在听 + HTTP 200
    #   监控只读:不启动/恢复/修改任何工作流状态;触发只走 run_workflow CLI
    #   与 agent_gate(MCP / drive)。
    # ===============================================================
    $monPidReused = $false
    $webPyPid     = $null
    $webProc      = $null

    Write-Phase ''
    Write-Phase "Phase 0.6:只读监控服务 monitor_fastapi(:$MonitorPort,主 .venv)" 'Cyan'

    # 路径校验(主 venv 与入口文件)
    if (-not (Test-Path $VenvPy)) {
        Write-PhaseErr "找不到 $VenvPy"
        Write-Phase "    请先建主 .venv 并装依赖: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt(含 fastapi/uvicorn)" 'Yellow'
        exit 18
    }
    if (-not (Test-Path $MonitorScript)) {
        Write-PhaseErr "找不到 $MonitorScript(阶段1 监控拆分产物,请确认代码完整)" 'Yellow'
        exit 18
    }

    $webUrl      = "http://127.0.0.1:$MonitorPort/"
    $webPortOpen = Test-PortOpen $MonitorPort
    # C-T2:本次启动期望的配置指纹(启动/复用/重启时都写进 PID 文件,供下次比对)
    $expectedFp = Get-WebConfigFingerprint -Uri $PostgresUri

    if ($webPortOpen) {
        if (-not (Test-WebHealthy -Url $webUrl -TimeoutSec 5)) {
            # 分支 2:端口冲突 —— fail-closed,不杀未知进程
            Write-PhaseErr "端口 $MonitorPort 已被占用,但 GET $webUrl 不返回 200(端口冲突)"
            Write-Phase "    本脚本不会杀任何未确认归属的进程。请手动处理占用该端口的进程后重跑。" 'Yellow'
            exit 23
        }

        # 服务健康 → 先验「归属 + 配置指纹」(C-T2):
        # HTTP 200 证明不了这个进程由本仓库、以本次 -PostgresUri 启动。
        $owner = Find-OwningWebRecord
        if ($null -eq $owner) {
            # 分支 2b:健康但归属不明 → fail-closed(不杀未知进程)
            Write-PhaseErr "端口 $MonitorPort 上有健康服务,但找不到 PID 文件能证明它由本项目启动 → 按未知进程处理(fail-closed)"
            $listener = Get-NetTCPConnection -LocalPort $MonitorPort -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($null -ne $listener) {
                $op = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
                if ($null -ne $op) {
                    Write-Phase ("    占用进程:PID={0}({1})。请确认归属后手动停止再重跑。" -f $op.Id, $op.ProcessName) 'Yellow'
                }
            }
            Write-Phase "    本脚本只复用/重启 PID 文件明确归本项目所有的监控服务,不自动杀未知进程。" 'Yellow'
            exit 23
        }

        if ($owner.config_fingerprint -and ($owner.config_fingerprint -eq $expectedFp)) {
            # 分支 1:归属 + 配置指纹双确认 → 复用
            $monPidReused = $true
            Write-PhaseOk ("监控服务已在运行且健康,配置指纹一致({0}),复用现有进程" -f $expectedFp)
            $pidObj = [ordered]@{
                timestamp   = $Timestamp
                reused      = $true
                os_dir      = $RepoRoot
                web_pid     = $null
                web_cmd_pid = $null
                web_log     = $null
                config_fingerprint = $expectedFp
                reused_from = $owner.source_file
            }
            $pidObj | ConvertTo-Json | Set-Content -Path $MonitorPidFile -Encoding UTF8
            Write-PhaseOk ("PID 文件(reused): {0}" -f $MonitorPidFile)
        } else {
            # 分支 1b:归本项目所有,但配置无法证明一致(指纹不同)
            # → 受控重启:只杀 PID 文件明确归属的进程,再以本次注入的环境重启。
            $curFp = '<无指纹>'
            if ($owner.config_fingerprint) { $curFp = $owner.config_fingerprint }
            Write-PhaseWarn ("现有监控服务归本项目所有(PID={0},来自 {1}),但配置指纹不一致(现有 {2},期望 {3})→ 受控重启" -f $owner.web_pid, $owner.source_file, $curFp, $expectedFp)
            foreach ($killPid in @($owner.web_pid, $owner.web_cmd_pid)) {
                if ($null -ne $killPid -and [int]$killPid -gt 0) {
                    Stop-Process -Id $killPid -Force -ErrorAction SilentlyContinue
                }
            }
            $portFreeDeadline = (Get-Date).AddSeconds(15)
            while ((Get-Date) -lt $portFreeDeadline -and (Test-PortOpen $MonitorPort)) {
                Start-Sleep -Milliseconds 500
            }
            if (Test-PortOpen $MonitorPort) {
                Write-PhaseErr "受控重启失败:停止本项目监控服务后端口 $MonitorPort 仍被占用(可能另有进程抢占)"
                Write-Phase "    本脚本不会杀未知进程。请手动处理后重跑。" 'Yellow'
                exit 23
            }
            Write-PhaseOk "旧监控服务已停止、端口已释放;按本次配置重新启动"
        }
    }

    if (-not $monPidReused) {
        # 分支 3:端口空闲(或刚受控重启释放)—— 起一次 uvicorn
        # 孤儿清理:扫本仓库之前的 PID 文件,把仍活着的 monitor python 进程 kill
        Get-ChildItem -Path $LogsDir -Filter 'monitor_pids_*.json' -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -ne $MonitorPidFile } |
            Sort-Object LastWriteTime -Descending |
            ForEach-Object {
                try {
                    $prev = Get-Content -Path $_.FullName -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json -ErrorAction SilentlyContinue
                    if ($null -ne $prev -and $prev.reused -eq $true) { return }
                    $pidVal = $prev.web_pid
                    if ($null -ne $pidVal -and $pidVal -gt 0) {
                        $alive = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
                        if ($null -ne $alive) {
                            Write-Phase ("  杀孤儿 monitor PID={0}" -f $pidVal)
                            Stop-Process -Id $pidVal -Force -ErrorAction SilentlyContinue
                        }
                    }
                } catch { }
            }

        # 强制 Python 不缓冲(便于日志实时落盘)
        $env:PYTHONUNBUFFERED = '1'

        Write-Phase "  启动监控服务(uvicorn monitor_fastapi:app --host 127.0.0.1 --port $MonitorPort)..."
        $monBatPath = Join-Path $env:TEMP "monitor_${Timestamp}.bat"
        # bat 以纯 ASCII 落盘(规避 cmd 代码页差异),因此内容里不能出现任何非
        # ASCII 字符:仓库路径含中文(如 "…\开发代码\…")时,绝对路径会被编码成
        # "???",cmd 解析不了 → python 起不来(2026-11-03 启动失败根因)。
        # bat 由下方 -WorkingDirectory $RepoRoot 启动,故 python 与日志一律写相对路径。
        $monVenvPyRel = $VenvPy.Substring($RepoRoot.Length).TrimStart('\')
        $monLogRel    = $MonitorLog.Substring($RepoRoot.Length).TrimStart('\')
        $monBatContent = @"
@echo off
set PYTHONUNBUFFERED=1
"$monVenvPyRel" -m uvicorn monitor_fastapi:app --host 127.0.0.1 --port $MonitorPort 1> "$monLogRel" 2>&1
"@
        [System.IO.File]::WriteAllText($monBatPath, $monBatContent, [System.Text.Encoding]::ASCII)
        $webProc = Start-Process `
            -FilePath 'cmd.exe' `
            -ArgumentList "/c `"$monBatPath`"" `
            -WorkingDirectory $RepoRoot `
            -WindowStyle Hidden `
            -PassThru
        $webPyPid = $null
        $webDeadline = (Get-Date).AddSeconds(8)
        while ((Get-Date) -lt $webDeadline -and $null -eq $webPyPid) {
            Start-Sleep -Milliseconds 500
            try {
                $child = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($webProc.Id)" -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'monitor_fastapi:app' } |
                    Select-Object -First 1
                if ($null -ne $child) { $webPyPid = [int]$child.ProcessId }
            } catch { }
        }
        if ($null -eq $webPyPid) {
            Write-PhaseWarn ("未在 8s 内抓到 python.exe 子进程,先用 cmd.exe PID={0} 兜底" -f $webProc.Id)
            $webPyPid = $webProc.Id
        }
        Write-Phase ("  monitor cmd PID={0} -> python PID={1}" -f $webProc.Id, $webPyPid)

        # 写 PID 文件(供 stop_production.ps1 读;config_fingerprint 供下次启动的复用判定)
        $pidObj = [ordered]@{
            timestamp   = $Timestamp
            reused      = $false
            os_dir      = $RepoRoot
            web_pid     = $webPyPid
            web_cmd_pid = $webProc.Id
            web_log     = $MonitorLog
            config_fingerprint = $expectedFp
        }
        $pidObj | ConvertTo-Json | Set-Content -Path $MonitorPidFile -Encoding UTF8
        Write-PhaseOk ("PID 文件: {0}" -f $MonitorPidFile)
    }

    # -----------------------------------------------------------
    # Phase 0.7:健康检查(本次启动:PID 存活 + 端口在听 + HTTP 200;复用:端口 + HTTP 200)
    # -----------------------------------------------------------
    Write-Phase ''
    Write-Phase "Phase 0.7:校验监控服务健康" 'Cyan'

    $healthyDeadline = (Get-Date).AddSeconds(60)
    $monHealthy = $false
    while ((Get-Date) -lt $healthyDeadline) {
        if (-not $monPidReused) {
            $webAlive = ($null -ne (Get-Process -Id $webPyPid -ErrorAction SilentlyContinue))
            if (-not $webAlive) {
                Write-PhaseErr ("监控服务进程 (PID={0}) 已退出,日志尾:" -f $webPyPid)
                if (Test-Path $MonitorLog) {
                    Get-Content $MonitorLog -Tail 80 -ErrorAction SilentlyContinue |
                        ForEach-Object { Write-Phase ("    $_") }
                }
                exit 21
            }
        }

        $portsOk = Test-PortOpen $MonitorPort
        $httpOk  = Test-WebHealthy -Url $webUrl -TimeoutSec 3
        if ($portsOk -and $httpOk) {
            $monHealthy = $true
            break
        }
        Start-Sleep -Seconds 2
    }

    if (-not $monHealthy) {
        Write-PhaseErr "60s 内监控服务未就绪"
        Write-Phase ("    端口 : {0} 在听 = {1}" -f $MonitorPort, (Test-PortOpen $MonitorPort)) 'Yellow'
        Write-Phase ("    URL  : {0}" -f $webUrl) 'Yellow'
        Write-Phase ("    复用 : {0}" -f $monPidReused) 'Yellow'

        # 精确根因提示:先探测 Postgres 端口,把「数据库未在线」这个最常见
        # 根因从笼统的超时里摘出来再报错。
        $pgTcpOk = Test-TcpPortOpen -Hostname $target.Host -Port $target.Port
        if (-not $pgTcpOk) {
            Write-Phase ("    Postgres : {0}:{1} 不可达(TCP 探测 {2}ms 超时)" -f $target.Host, $target.Port, 1500) 'Yellow'
            if ($target.Kind -eq 'local-managed') {
                Write-Phase '    → 根因大概率:本地 Postgres 容器(edb-postgres16)未运行。' 'Yellow'
                Write-Phase '      先 docker start edb-postgres16(或 scripts\start_postgres.ps1)再重跑。' 'Yellow'
            } else {
                Write-Phase "    → 外部 Postgres(host=$($target.Host):$($target.Port))不可达,请检查实例与网络后重跑。" 'Yellow'
            }
        } else {
            Write-Phase ("    Postgres : {0}:{1} 端口可达(数据库在线;监控服务未就绪是应用自身问题,见下方日志尾)" -f $target.Host, $target.Port) 'Yellow'
        }

        if (-not $monPidReused) {
            Write-Phase ("    进程 : PID={0} 存活 = {1}" -f $webPyPid, ($null -ne (Get-Process -Id $webPyPid -ErrorAction SilentlyContinue))) 'Yellow'
            if (Test-Path $MonitorLog) {
                Write-Phase '    最近日志尾:' 'Yellow'
                Get-Content $MonitorLog -Tail 40 -ErrorAction SilentlyContinue |
                    ForEach-Object { Write-Phase ("    $_") }
            }
        }
        Write-Phase '    不启动工作流(未就绪的服务不参与生产)。' 'Yellow'
        exit 22
    }
    Write-PhaseOk "监控服务健康(:$MonitorPort 就绪;只读,不触发工作流)"

    # ===============================================================
    # Phase 2 — run_workflow.py --production --skip-preflight
    #   --skip-preflight:预检已在 Phase 0.58 做过,这里不能再跑第二遍。
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

    # 注意:停在人工关卡(①/②/③)时 run_workflow.py **正常返回退出码 0**
    # (run_with_events 先落 run.interrupted 事件,恢复经 agent_gate resume 或
    # scripts\resume_workflow.py --checkpoint ① --value true 放行)。
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

    if ($pyExit -eq 0) {
        Write-PhaseOk "run_workflow.py 退出码 0(跑完,或停在人工关卡等待放行 —— 用 agent_gate status 查看挂起关卡)"
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
    # 原生命令非零退出码不要当场抛异常 —— 关掉 PSNativeCommandUseErrorActionPreference,
    # 让下面的 $countExit 分支给出友好提示(否则此处直接 NativeCommandExitException)。
    $prevNativePref = $PSNativeCommandUseErrorActionPreference
    $PSNativeCommandUseErrorActionPreference = $false
    try {
        # 同步 psycopg:Windows 下 psycopg 异步只支持 Selector 事件循环,asyncio.run 默认是
        # ProactorEventLoop 会直接报错(2026-09 提交 8b42830f 引入,2026-10-29 修复)。
        $pyCount = & $VenvPy -c @"
import os, sys
try:
    import psycopg
except ImportError:
    print('psycopg 未安装,未验收(可选依赖)')
    sys.exit(0)
try:
    tid = os.environ['THREAD_ID']
    # 续跑的 checkpoint 可能落在 psycopg 把 bytes 写进 TEXT 列产生的 '\x<hex>' thread_id 下,
    # 两套都要数,否则只查文本 thread_id 会漏掉续跑数据。
    tid_hex = '\\x' + tid.encode('utf-8').hex()
    def label(v):
        # 本机 Postgres 容器是 SQL_ASCII 编码,psycopg 会把 TEXT 列返回成 bytes
        s = v.decode('utf-8', 'replace') if isinstance(v, bytes) else str(v)
        if s.startswith('\\x'):
            try:
                return bytes.fromhex(s[2:]).decode('utf-8') + ' (hex)'
            except Exception:
                pass
        return s
    with psycopg.connect(os.environ['POSTGRES_URI']) as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s", (tid,))
            (n_text,) = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s", (tid_hex,))
            (n_hex,) = cur.fetchone()
            print(f'checkpoints: {n_text + n_hex}  (文本 thread_id={n_text}, 续跑 hex thread_id={n_hex})')
            cur.execute("SELECT thread_id, COUNT(*) FROM checkpoints GROUP BY thread_id ORDER BY 2 DESC")
            for r in cur.fetchall():
                print(f'  - thread {label(r[0])}: {r[1]}')
except Exception as e:
    print(f'查询失败: {e}', file=sys.stderr)
    sys.exit(1)
"@ 2>&1
        $countExit = $LASTEXITCODE
    } finally {
        $PSNativeCommandUseErrorActionPreference = $prevNativePref
    }
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
