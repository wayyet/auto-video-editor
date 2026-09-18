# ============================================================
# auto-video-editor 一键停止(生产全栈)
#
# 与 start_production.ps1 对称 —— 把启动时拉起的进程 / 容器按逆序停掉:
#   Phase 1  python run_workflow.py
#   Phase 2  openstoryline-web 容器
#   Phase 3  edb-postgres16 容器 (复用 scripts\stop_postgres.ps1)
#   Phase 4  -StopDockerDesktop 才停 com.docker.service
#   Phase 5  -CleanState 才删 initial_state JSON
#   Phase 6  -CleanHeartbeat 才删 heartbeat.txt
#
# 设计:
#   - 完全幂等:任一目标已停 / 不存在都 warn-and-continue
#   - 默认不删任何数据(命名卷、outputs、drafts、checkpoints 全保留)
#   - 不需要管理员权限(只有 -StopDockerDesktop 路径需要)
#   - UTF-8 控制台 + 日志,与 start_production.ps1 同风格
#
# 用法(普通 Windows PowerShell 即可):
#   .\stop_production.ps1                                # 默认安全停止
#   .\stop_production.ps1 -RemoveContainers              # stop + rm 两个容器
#   .\stop_production.ps1 -StopDockerDesktop             # 顺带关 Docker Desktop
#   .\stop_production.ps1 -CleanState -CleanHeartbeat    # 清残留
#   .\stop_production.ps1 -WhatIf                        # 只打印计划
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
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null }
$Timestamp     = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogFile       = Join-Path $LogsDir ("stop_${Timestamp}.log")
$StopPostgres  = Join-Path $RepoRoot 'scripts\stop_postgres.ps1'
$StateJson     = Join-Path $RepoRoot ("runtime\initial_state_{0}.json" -f $ThreadId)
$HeartbeatFile = 'C:\ProgramData\VideoWorkflow\heartbeat.txt'
$OsContainer   = 'openstoryline-web'

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
# Banner
# ---------------------------------------------------------------
Write-Phase ('=' * 60) 'Magenta'
Write-Phase "auto-video-editor 一键停止(生产全栈)" 'Magenta'
Write-Phase ("ThreadId : {0}" -f $ThreadId) 'Magenta'
Write-Phase ("日志路径 : {0}" -f $LogFile) 'Magenta'
Write-Phase ('=' * 60) 'Magenta'

# ---------------------------------------------------------------
# WhatIf:只显示计划,不做任何修改
# ---------------------------------------------------------------
if ($WhatIf) {
    Write-Phase "[WhatIf] 不会执行任何修改,以下为计划:" 'Yellow'
    Write-Phase "  Phase 1:kill python run_workflow.py (--production)"
    if ($RemoveContainers) {
        Write-Phase "  Phase 2:docker stop $OsContainer + rm"
    } else {
        Write-Phase "  Phase 2:docker stop $OsContainer (容器保留)"
    }
    if ($RemoveContainers) {
        Write-Phase "  Phase 3:$StopPostgres -Remove (容器删,卷永远保留)"
    } else {
        Write-Phase "  Phase 3:$StopPostgres (容器保留,卷永远保留)"
    }
    if ($StopDockerDesktop) {
        Write-Phase "  Phase 4:Stop-Service com.docker.service (需管理员)"
    } else {
        Write-Phase "  Phase 4:[跳过 -StopDockerDesktop 未传]"
    }
    if ($CleanState) {
        Write-Phase "  Phase 5:删除 $StateJson"
    } else {
        Write-Phase "  Phase 5:[跳过 -CleanState 未传]"
    }
    if ($CleanHeartbeat) {
        Write-Phase "  Phase 6:删除 $HeartbeatFile"
    } else {
        Write-Phase "  Phase 6:[跳过 -CleanHeartbeat 未传]"
    }
    exit 0
}

# ===============================================================
# Phase 1 — kill python run_workflow.py
# ===============================================================
Write-Phase ''
Write-Phase "Phase 1:停止 Python 工作流进程" 'Cyan'

try {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" `
        -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match 'run_workflow\.py' -and
            $_.CommandLine -match '--production'
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
# Phase 2 — stop openstoryline-web
# ===============================================================
Write-Phase ''
Write-Phase "Phase 2:停止 $OsContainer 容器" 'Cyan'

$dockerExe = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $dockerExe) {
    Write-PhaseWarn "找不到 docker 命令,跳过 Phase 2 / 3(请确认 Docker Desktop 是否就绪)"
} else {
    $prevEA = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    $osExisting = @(& docker ps -a --filter "name=^${OsContainer}$" --format '{{.Names}}' 2>&1)
    $osRunning  = @(& docker ps     --filter "name=^${OsContainer}$" --format '{{.Names}}' 2>&1)
    $ErrorActionPreference = $prevEA

    if (-not $osExisting -or $osExisting.Count -eq 0 -or -not ($osExisting -contains $OsContainer)) {
        Write-PhaseWarn "容器 $OsContainer 不存在"
    } elseif ($osRunning -and $osRunning -contains $OsContainer) {
        & docker stop $OsContainer
        if ($LASTEXITCODE -eq 0) {
            Write-PhaseOk "$OsContainer 已停止"
        } else {
            Write-PhaseErr "docker stop $OsContainer 失败(exit=$LASTEXITCODE)"
        }
    } else {
        Write-Phase "  $OsContainer 已处于停止态"
    }

    if ($RemoveContainers) {
        if ($osExisting -and $osExisting -contains $OsContainer) {
            & docker rm $OsContainer
            if ($LASTEXITCODE -eq 0) {
                Write-PhaseOk "$OsContainer 已删除"
            } else {
                Write-PhaseErr "docker rm $OsContainer 失败(exit=$LASTEXITCODE)"
            }
        } else {
            Write-Phase "  $OsContainer 已不存在,无需 rm"
        }
    } else {
        Write-Phase "  容器已保留(传 -RemoveContainers 才删)"
    }
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
    Write-Phase "Phase 4:停止 Docker Desktop(Stop-Service)" 'Cyan'
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