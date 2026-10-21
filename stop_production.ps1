# ============================================================
# auto-video-editor 一键停止(生产全栈)
#
# 与 start_production.ps1 对称 —— 把启动时拉起的进程 / 容器按逆序停掉:
#   Phase 1  python run_workflow.py + agent_gate worker
#   Phase 2  OpenStoryline 本地 Web(按 PID 文件 kill + 命令行兜底)
#   Phase 3  edb-postgres16 容器 (复用 scripts\stop_postgres.ps1)
#   Phase 4  -StopDockerDesktop 才停 Docker Desktop(docker desktop stop)
#   Phase 5  -CleanState 才删 initial_state JSON
#   Phase 6  -CleanHeartbeat 才删 heartbeat.txt
#
# 设计:
#   - 完全幂等:任一目标已停 / 不存在都 warn-and-continue
#   - 默认不删任何数据(命名卷、outputs、drafts、checkpoints 全保留)
#   - 不需要管理员权限(只有 -StopDockerDesktop 路径需要)
#   - UTF-8 控制台 + 日志,与 start_production.ps1 同风格
#   - **所有权口径**:PID 文件里 ``reused=true`` 表示那个 Web 是外部进程,
#     本脚本**不拥有**它 → PID kill 与命令行兜底都不执行,只提示手动处理。
#     PID 文件缺失 / 解析失败 → 保持现有命令行兜底。
#
# 用法(普通 Windows PowerShell 即可):
#   .\stop_production.ps1                                # 默认安全停止
#   .\stop_production.ps1 -RemoveContainers              # stop + rm 两个容器
#   .\stop_production.ps1 -StopDockerDesktop             # 顺带关 Docker Desktop
#   .\stop_production.ps1 -CleanState -CleanHeartbeat    # 清残留
#   .\stop_production.ps1 -WhatIf                        # 只打印计划(零副作用)
# ============================================================

[CmdletBinding()]
param(
    [string]$ThreadId        = 'video-001',
    [switch]$RemoveContainers,
    [switch]$StopDockerDesktop,
    [switch]$CleanState,
    [switch]$CleanHeartbeat,
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

# ---------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------
$RepoRoot      = (Resolve-Path (Join-Path $PSScriptRoot '.')).Path
$LogsDir       = Join-Path $RepoRoot 'logs'
$Timestamp     = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogFile       = Join-Path $LogsDir ("stop_${Timestamp}.log")
$StopPostgres  = Join-Path $RepoRoot 'scripts\stop_postgres.ps1'
$StateJson     = Join-Path $RepoRoot ("runtime\initial_state_{0}.json" -f $ThreadId)
$HeartbeatFile = 'C:\ProgramData\VideoWorkflow\heartbeat.txt'

# ---------------------------------------------------------------
# WhatIf:只显示计划,不做任何修改
#   必须在"建 logs/ 目录 / 建日志文件 / banner"之前 —— 零副作用。
#   用 Write-Plan(只 Write-Host),不碰 $LogFile。
# ---------------------------------------------------------------
function Write-Plan {
    param([string]$Text, [string]$Color = 'Cyan')
    Write-Host $Text -ForegroundColor $Color
}

if ($WhatIf) {
    Write-Plan '[WhatIf] 不会执行任何修改,以下为计划:' 'Yellow'
    Write-Plan '  Phase 1:kill python run_workflow.py (--production) + agent_gate worker'
    Write-Plan '  Phase 2:Stop-Process OpenStoryline Web (按 PID 文件 + 命令行兜底)'
    Write-Plan '         (PID 文件标记 reused=true 时 → 复用进程保留不动,只提示手动处理)'
    if ($RemoveContainers) {
        Write-Plan "  Phase 3:$StopPostgres -Remove (容器删,卷永远保留)"
    } else {
        Write-Plan "  Phase 3:$StopPostgres (容器保留,卷永远保留)"
    }
    if ($StopDockerDesktop) {
        Write-Plan '  Phase 4:docker desktop stop(关 Docker Desktop 本身,免管理员)'
    } else {
        Write-Plan '  Phase 4:[跳过 -StopDockerDesktop 未传]'
    }
    if ($CleanState) {
        Write-Plan "  Phase 5:删除 $StateJson"
    } else {
        Write-Plan '  Phase 5:[跳过 -CleanState 未传]'
    }
    if ($CleanHeartbeat) {
        Write-Plan "  Phase 6:删除 $HeartbeatFile"
    } else {
        Write-Plan '  Phase 6:[跳过 -CleanHeartbeat 未传]'
    }
    exit 0
}

# ---------------------------------------------------------------
# 从这里开始才允许有副作用
# ---------------------------------------------------------------
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null }

# ---------------------------------------------------------------
# 日志函数:同时写控制台 + 日志文件
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

# ---------------------------------------------------------------
# Banner
# ---------------------------------------------------------------
Write-Phase ('=' * 60) 'Magenta'
Write-Phase "auto-video-editor 一键停止(生产全栈)" 'Magenta'
Write-Phase ("ThreadId : {0}" -f $ThreadId) 'Magenta'
Write-Phase ("日志路径 : {0}" -f $LogFile) 'Magenta'
Write-Phase ('=' * 60) 'Magenta'


# ===============================================================
# Phase 1 — kill python run_workflow.py
# ===============================================================
Write-Phase ''
Write-Phase "Phase 1:停止 Python 工作流进程" 'Cyan'

try {
    # 本项目自产的 python 进程有两类,都要停:
    #   a) run_workflow.py --production   —— 端到端工作流主进程
    #   b) -m agent_gate worker           —— agent_gate 后台 worker
    #      (agent_gate CLI 没有 stop 子命令,只能按进程停;`start` 拉起的后台 worker
    #       若不杀会一直占着线程与 outputs 目录)
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" `
        -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            (
                (($_.CommandLine -match 'run_workflow\.py') -and ($_.CommandLine -match '--production')) -or
                ($_.CommandLine -match '-m\s+agent_gate\s+worker')
            )
        }
    if (-not $procs -or $procs.Count -eq 0) {
        Write-PhaseWarn "未发现 run_workflow.py 进程(可能未启动 / 已完成)"
    } else {
        foreach ($p in $procs) {
            $cl = if ($p.CommandLine) { $p.CommandLine } else { '' }
            $clPreview = if ($cl.Length -gt 120) { $cl.Substring(0, 120) + '...' } else { $cl }
            Write-Phase ("  kill PID={0} CommandLine={1}" -f $p.ProcessId, $clPreview)
            try {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                Write-PhaseOk ("PID {0} 已退出" -f $p.ProcessId)
            } catch {
                Write-PhaseWarn ("PID {0} 终止失败(可能已退出): {1}" -f $p.ProcessId, $_.Exception.Message)
            }
        }
    }
} catch {
    Write-PhaseWarn ("查询 python 进程失败: {0}" -f $_.Exception.Message)
}

# ===============================================================
# Phase 2 — stop OpenStoryline 本地 Web
#   优先按 PID 文件 kill;文件缺失 / 解析失败时按命令行兜底
#   **所有权口径**:PID 文件 reused=true → 那是外部进程,本脚本不拥有它,
#   PID kill 与命令行兜底(agent_fastapi:app)都不执行,只提示手动处理。
# ===============================================================
Write-Phase ''
Write-Phase "Phase 2:停止 OpenStoryline 本地 Web(:7860)" 'Cyan'

# 收集要 kill 的 PID:先扫 PID 文件,再扫命令行兜底
$OsMcpPort = 8001
$OsWebPort = 7860
$pidsToKill = [System.Collections.Generic.HashSet[int]]::new()
$pidsSource = [System.Collections.Generic.List[string]]::new()
# reused=true → 外部复用进程,跳过命令行兜底里的 agent_fastapi:app 匹配
$skipWebCmdFallback = $false
$ownershipState = 'unknown'   # owned | reused | not-found

# 路径 1:读最新 PID 文件(按 LastWriteTime 倒序取一个)
$latestPidFile = Get-ChildItem -Path $LogsDir -Filter 'openstoryline_pids_*.json' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -ne $latestPidFile) {
    try {
        $prev = Get-Content -Path $latestPidFile.FullName -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($null -ne $prev -and $prev.reused -eq $true) {
            # 外部复用:两条路径都不执行
            $skipWebCmdFallback = $true
            $ownershipState = 'reused'
            Write-Phase "  读 PID 文件:{0}" -f $latestPidFile.Name
            Write-Phase "  PID 文件标记为 reused(端口复用)—— 复用的 Web 保留不动,需要手动停止" 'Yellow'
        } elseif ($null -ne $prev) {
            $ownershipState = 'owned'
            foreach ($pidName in @('mcp_pid', 'web_pid', 'web_cmd_pid')) {
                $pidVal = $prev.$pidName
                if ($null -ne $pidVal -and $pidVal -gt 0 -and -not $pidsToKill.Contains([int]$pidVal)) {
                    $pidsToKill.Add([int]$pidVal) | Out-Null
                    $pidsSource.Add("PID 文件 ${pidName}={$pidVal}") | Out-Null
                }
            }
            Write-Phase ("  读 PID 文件:{0}" -f $latestPidFile.Name)
        } else {
            Write-PhaseWarn "PID 文件内容为空,走命令行兜底"
        }
    } catch {
        Write-PhaseWarn "解析 PID 文件失败(走命令行兜底): $($_.Exception.Message)"
    }
} else {
    Write-Phase "  未找到 PID 文件(走命令行兜底)"
}

# 路径 2:命令行兜底(Get-CimInstance 查 python.exe 命令行)
#   reused=true → 跳过 agent_fastapi:app(那是外部复用的 Web,不能杀)
if ($skipWebCmdFallback) {
    Write-Phase "  复用模式:跳过命令行兜底(不杀外部 agent_fastapi:app 进程)" 'Yellow'
} else {
    try {
        $pyProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
        foreach ($p in $pyProcs) {
            $cl = $p.CommandLine
            if ($cl -and ($cl -match 'open_storyline\.mcp\.server' -or $cl -match 'agent_fastapi:app')) {
                if (-not $pidsToKill.Contains([int]$p.ProcessId)) {
                    $pidsToKill.Add([int]$p.ProcessId) | Out-Null
                    $preview = if ($cl.Length -gt 100) { $cl.Substring(0, 100) + '...' } else { $cl }
                    $pidsSource.Add("命令行 PID={0} ({2})" -f $p.ProcessId, $preview) | Out-Null
                }
            }
        }
    } catch {
        Write-PhaseWarn ("查询 python.exe 命令行失败: {0}" -f $_.Exception.Message)
    }
}

if ($pidsToKill.Count -eq 0) {
    Write-Phase "  未发现 OpenStoryline 进程,无需 kill"
    if ($ownershipState -eq 'owned') { $ownershipState = 'not-found' }
} else {
    foreach ($pidVal in $pidsToKill) {
        $alive = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
        if ($null -eq $alive) {
            Write-Phase ("  PID {0} 已退出,跳过" -f $pidVal)
            continue
        }
        Write-Phase ("  kill PID={0} name={1}" -f $pidVal, $alive.ProcessName)
        try {
            Stop-Process -Id $pidVal -Force -ErrorAction Stop
            Write-PhaseOk ("PID {0} 已退出" -f $pidVal)
        } catch {
            Write-PhaseWarn ("PID {0} 终止失败: {1}" -f $pidVal, $_.Exception.Message)
        }
    }
}

# 停止状态三态
switch ($ownershipState) {
    'owned'   { Write-PhaseOk "停止状态:本次启动所有(已终止)" }
    'reused'  { Write-Phase "停止状态:复用(保留)—— 复用的 Web 保留不动,需要手动停止" 'Yellow' }
    'not-found' { Write-Phase "停止状态:未发现进程" 'Yellow' }
    default   { Write-Phase "停止状态:未确认(PID 文件缺失,已走命令行兜底)" 'Yellow' }
}

# 路径 3:端口兜底校验 — 等 ≤5s 看 8001 / 7860 是否释放
if ($skipWebCmdFallback) {
    Write-Phase "  复用模式:跳过端口释放校验(外部 Web 仍占着 :7860,属预期)" 'Yellow'
} else {
    $portDeadline = (Get-Date).AddSeconds(5)
    $portsReleased = $false
    while ((Get-Date) -lt $portDeadline) {
        $prevEA = $ErrorActionPreference
        $ErrorActionPreference = 'SilentlyContinue'
        $mcpOpen = (Test-NetConnection -ComputerName '127.0.0.1' -Port $OsMcpPort -InformationLevel Quiet -WarningAction SilentlyContinue) -eq $true
        $webOpen = (Test-NetConnection -ComputerName '127.0.0.1' -Port $OsWebPort -InformationLevel Quiet -WarningAction SilentlyContinue) -eq $true
        $ErrorActionPreference = $prevEA
        if (-not $mcpOpen -and -not $webOpen) { $portsReleased = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $portsReleased) {
        Write-PhaseWarn "5s 内 8001 / 7860 未释放(可能仍有别处占用)"
    }
}

if ($RemoveContainers) {
    Write-Phase "  OpenStoryline 已无容器(本地直起),-RemoveContainers 不作用于 OpenStoryline" 'Yellow'
}

# ===============================================================
# Phase 3 — stop edb-postgres16 (复用 scripts\stop_postgres.ps1)
# ===============================================================
Write-Phase ''
Write-Phase "Phase 3:停止 edb-postgres16 容器(复用 stop_postgres.ps1)" 'Cyan'

if (Test-Path $StopPostgres) {
    $spArgs = @{}
    if ($RemoveContainers) { $spArgs['Remove'] = $true }
    try {
        & $StopPostgres @spArgs 2>&1 | ForEach-Object {
            Write-Phase ("  [stop_postgres] $_")
        }
        $spExit = $LASTEXITCODE
        if ($spExit -eq 0) {
            Write-PhaseOk "stop_postgres.ps1 退出 0"
        } else {
            Write-PhaseWarn "stop_postgres.ps1 退出 $spExit"
        }
    } catch {
        Write-PhaseWarn ("stop_postgres.ps1 抛错: {0}" -f $_.Exception.Message)
    }
} else {
    Write-PhaseWarn "找不到 $StopPostgres,跳过 Phase 3"
}

# ===============================================================
# Phase 4 — Stop Docker Desktop (可选,需 admin)
# ===============================================================
Write-Phase ''
if ($StopDockerDesktop) {
    Write-Phase "Phase 4:停止 Docker Desktop" 'Cyan'

    # 优先用官方 CLI:`docker desktop stop`
    #   - 跨平台、免管理员
    #   - 能真正关掉用户态进程(Docker Desktop.exe / com.docker.backend.exe)
    # Stop-Service com.docker.service 只管 Windows 服务,本机该服务常态是
    # Stopped(Docker Desktop 以用户态运行),所以单靠它往往关不掉。
    $ddStopped = $false
    $prevEA4 = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    $ddOut = & docker desktop stop 2>&1
    $ddExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEA4
    if ($ddOut) { $ddOut | ForEach-Object { Write-Phase ("    $_") } }
    if ($ddExit -eq 0) {
        $ddStopped = $true
        Write-PhaseOk "Docker Desktop 已停止(docker desktop stop)"
    } else {
        Write-PhaseWarn "docker desktop stop 退出 $ddExit,退回 Stop-Service 路径"
    }

    if (-not $ddStopped) {
        $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            Write-PhaseWarn "当前不是管理员,无法 Stop-Service com.docker.service"
            Write-Phase "    请用管理员 PowerShell 重跑 -StopDockerDesktop,或在 Docker Desktop 托盘手动 Quit" 'Yellow'
        } else {
            $svc = Get-Service com.docker.service -ErrorAction SilentlyContinue
            if ($null -eq $svc) {
                Write-PhaseWarn "服务 com.docker.service 不存在(可能 Docker Desktop 未安装)"
            } elseif ($svc.Status -eq 'Stopped') {
                Write-Phase "  com.docker.service 已处于 Stopped"
            } else {
                Stop-Service com.docker.service -Force -ErrorAction SilentlyContinue
                if ($LASTEXITCODE -eq 0) {
                    Write-PhaseOk "com.docker.service 已停止"
                } else {
                    Write-PhaseWarn "Stop-Service com.docker.service 失败"
                }
            }
        }
    }
} else {
    Write-Phase "Phase 4:跳过(-StopDockerDesktop 未传)" 'Yellow'
}

# ===============================================================
# Phase 5 — CleanState
# ===============================================================
Write-Phase ''
if ($CleanState) {
    Write-Phase "Phase 5:删除 initial-state JSON" 'Cyan'
    if (Test-Path $StateJson) {
        Remove-Item -Path $StateJson -Force
        Write-PhaseOk "已删除 $StateJson"
    } else {
        Write-PhaseWarn "$StateJson 不存在"
    }
} else {
    Write-Phase "Phase 5:跳过(-CleanState 未传)" 'Yellow'
}

# ===============================================================
# Phase 6 — CleanHeartbeat
# ===============================================================
Write-Phase ''
if ($CleanHeartbeat) {
    Write-Phase "Phase 6:删除 heartbeat 文件" 'Cyan'
    if (Test-Path $HeartbeatFile) {
        Remove-Item -Path $HeartbeatFile -Force
        Write-PhaseOk "已删除 $HeartbeatFile"
    } else {
        Write-PhaseWarn "$HeartbeatFile 不存在"
    }
} else {
    Write-Phase "Phase 6:跳过(-CleanHeartbeat 未传)" 'Yellow'
}

# ===============================================================
# 收尾
# ===============================================================
Write-Phase ''
Write-Phase ('=' * 60) 'Magenta'
Write-Phase "一键停止完成" 'Green'
Write-Phase ("完整日志: {0}" -f $LogFile) 'Cyan'
Write-Phase "提示:下次启动请跑 .\start_production.ps1" 'Cyan'
Write-Phase ('=' * 60) 'Magenta'
exit 0