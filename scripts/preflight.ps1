# ============================================================
# auto-video-editor 启动前自检(pre-flight)
#
# 顺序检查并按需拉起:
#   Step 1  Docker Desktop Windows 服务(com.docker.service)
#   Step 2  Docker daemon 就绪(docker info)
#   Step 3  Docker 代理配置(check_docker_proxy.ps1,代理空时跳过)
#   Step 4  Postgres 容器(start_postgres.ps1)
#   Step 5  Postgres 连通性(verify_postgres.py)
#   Step 6  LangGraph checkpoint schema(setup_postgres_schema.py)
#
# 退出码约定:
#   0    全部 OK
#   10-16 对应 Step 1-6 失败(Python 侧据此定位失败 step)
#
# 用法:
#   .\scripts\preflight.ps1                                  # 全部步骤
#   .\scripts\preflight.ps1 -SkipDockerService -SkipProxyCheck # 跳过 1 + 3
#   .\scripts\preflight.ps1 -DryRun                          # 只打印计划不执行
#
# 注意:Step 1 需管理员权限;否则 Start-Service 会抛 UnauthorizedAccess,
# 退出码 11。生产部署请用「管理员: Windows PowerShell」运行,
# 或在 README「部署」章节登记 Windows 任务计划程序预先 elevated 启动。
# ============================================================

[CmdletBinding()]
param(
    [int]$DockerServiceWaitSec = 60,
    [int]$PostgresHealthWaitSec = 60,
    [switch]$SkipDockerService,
    [switch]$SkipProxyCheck,
    [switch]$SkipPostgresStart,
    [switch]$SkipSchemaSetup,
    [switch]$DryRun
)

# Windows 默认控制台 GBK,中文/符号会乱码
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding  = [System.Text.Encoding]::UTF8
    $OutputEncoding           = [System.Text.Encoding]::UTF8
} catch { }

$ErrorActionPreference = 'Stop'
$prevErrorAction = $ErrorActionPreference

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Resolve-Path (Join-Path $ScriptDir '..')

function Write-Step {
    param([string]$Text)
    Write-Host "[preflight] $Text" -ForegroundColor Cyan
}

function Exit-WithCode {
    # 故意 exit 0 让 PowerShell 把 exitcode 传到 Python;用 throw 会触发 catch 段。
    param([int]$Code)
    exit $Code
}

# ---------------------------------------------------------------
# Step 1 — Docker Desktop Windows 服务
# ---------------------------------------------------------------
function Step-DockerService {
    Write-Step 'Step 1: Docker Desktop Windows 服务'
    if ($SkipDockerService) {
        Write-Step '  (跳过,-SkipDockerService)'
        return
    }
    try {
        $svc = Get-Service -Name 'com.docker.service' -ErrorAction Stop
    } catch {
        Write-Host "✗ Docker Desktop 服务未安装,无法继续" -ForegroundColor Red
        Write-Host '  请先安装 Docker Desktop for Windows' -ForegroundColor Yellow
        Exit-WithCode 10
    }
    if ($svc.Status -eq 'Running') {
        Write-Step '  ✓ 已在运行'
        return
    }
    try {
        Start-Service -InputObject $svc -ErrorAction Stop
        # 等到 Running
        $svc.WaitForStatus('Running', (New-TimeSpan -Seconds $DockerServiceWaitSec))
        Write-Step "  ✓ 已启动 (waited $DockerServiceWaitSec s)"
    } catch [System.ServiceProcess.TimeoutException] {
        Write-Host "✗ Docker Desktop 服务在 $DockerServiceWaitSec s 内未进入 Running" -ForegroundColor Red
        Exit-WithCode 12
    } catch [System.UnauthorizedAccessException] {
        Write-Host '✗ 启动 Docker Desktop 服务需要管理员权限' -ForegroundColor Red
        Write-Host '  请用「管理员: Windows PowerShell」重新运行,或手动:' -ForegroundColor Yellow
        Write-Host '    1. 开始菜单搜索「服务」' -ForegroundColor Yellow
        Write-Host '    2. 找到「Docker Desktop Service」,右键 → 启动' -ForegroundColor Yellow
        Exit-WithCode 11
    } catch {
        Write-Host "✗ Docker Desktop 服务启动失败:$($_.Exception.Message)" -ForegroundColor Red
        Exit-WithCode 10
    }
}

# ---------------------------------------------------------------
# Step 2 — Docker daemon 就绪(轮询 docker info)
# ---------------------------------------------------------------
function Step-DaemonReady {
    Write-Step 'Step 2: Docker daemon 就绪'
    for ($i = 0; $i -lt $DockerServiceWaitSec; $i++) {
        $ErrorActionPreference = 'SilentlyContinue'
        $info = (& docker info 2>&1) | Out-String
        $ok = $info -notmatch 'cannot connect|Cannot connect|error during connect'
        $ErrorActionPreference = $prevErrorAction
        if ($ok) {
            Write-Step "  ✓ daemon 已就绪 (${i}s)"
            return
        }
        Start-Sleep -Seconds 1
    }
    Write-Host "✗ Docker daemon 在 $DockerServiceWaitSec s 内未就绪" -ForegroundColor Red
    Exit-WithCode 12
}

# ---------------------------------------------------------------
# Step 3 — Docker 代理配置
# ---------------------------------------------------------------
function Step-ProxyCheck {
    Write-Step 'Step 3: Docker 代理配置'
    if ($SkipProxyCheck) {
        Write-Step '  (跳过,-SkipProxyCheck)'
        return
    }
    # check_docker_proxy.ps1: 失败抛错,exit code 由 PowerShell 自然传 1。
    # 我们用 -RestartDocker 让它在失败时自动 docker desktop restart 自愈。
    & pwsh -NoProfile -File (Join-Path $ScriptDir 'check_docker_proxy.ps1') -RestartDocker
    if ($LASTEXITCODE -ne 0) {
        Write-Host "✗ Docker 代理配置异常,无法拉镜像 (exit=$LASTEXITCODE)" -ForegroundColor Red
        Exit-WithCode 13
    }
    Write-Step '  ✓ 代理可用'
}

# ---------------------------------------------------------------
# Step 4 — Postgres 容器启停
# ---------------------------------------------------------------
function Step-PostgresContainer {
    Write-Step 'Step 4: Postgres 容器'
    if ($SkipPostgresStart) {
        Write-Step '  (跳过,-SkipPostgresStart)'
        return
    }
    & pwsh -NoProfile -File (Join-Path $ScriptDir 'start_postgres.ps1')
    if ($LASTEXITCODE -ne 0) {
        Write-Host "✗ Postgres 容器启动失败,见上面 start_postgres.ps1 输出 (exit=$LASTEXITCODE)" -ForegroundColor Red
        Exit-WithCode 14
    }
    Write-Step '  ✓ Postgres 容器 healthy'
}

# ---------------------------------------------------------------
# Step 5 — Postgres 连通性 + Step 6 — schema
# ---------------------------------------------------------------
function Step-PostgresConnect {
    Write-Step 'Step 5: Postgres 连通性'
    if (-not $env:POSTGRES_URI) {
        # 默认与 start_postgres.ps1 末尾打印的一致;确保 verify_postgres.py 能直接读
        $env:POSTGRES_URI = 'postgresql://postgres:devpass@localhost:5432/langgraph'
        Write-Step "  使用默认 POSTGRES_URI=$env:POSTGRES_URI"
    }
    $pythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $pythonExe)) {
        $pythonExe = 'python'
    }
    & $pythonExe (Join-Path $ScriptDir 'verify_postgres.py')
    if ($LASTEXITCODE -ne 0) {
        Write-Host "✗ Postgres 不可达,见上面 verify_postgres.py 输出 (exit=$LASTEXITCODE)" -ForegroundColor Red
        Exit-WithCode 15
    }
    Write-Step '  ✓ Postgres 连通 + checkpoint 表存在'
}

function Step-SchemaSetup {
    Write-Step 'Step 6: LangGraph checkpoint schema'
    if ($SkipSchemaSetup) {
        Write-Step '  (跳过,-SkipSchemaSetup)'
        return
    }
    $pythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $pythonExe)) {
        $pythonExe = 'python'
    }
    & $pythonExe (Join-Path $ScriptDir 'setup_postgres_schema.py')
    if ($LASTEXITCODE -ne 0) {
        Write-Host "✗ checkpoint 表创建失败,见上面 setup_postgres_schema.py 输出 (exit=$LASTEXITCODE)" -ForegroundColor Red
        Exit-WithCode 16
    }
    Write-Step '  ✓ schema 已就绪'
}

# ---------------------------------------------------------------
# DryRun 模式:只打印计划
# ---------------------------------------------------------------
if ($DryRun) {
    Write-Step 'DryRun 模式:仅打印执行计划,不实际执行'
    $plan = @()
    $plan += "  Step 1  Docker Desktop 服务       $(if ($SkipDockerService) { '(跳过)' } else { '执行' })"
    $plan += "  Step 2  Docker daemon 就绪         执行"
    $plan += "  Step 3  Docker 代理配置           $(if ($SkipProxyCheck)     { '(跳过)' } else { '执行' })"
    $plan += "  Step 4  Postgres 容器             $(if ($SkipPostgresStart)  { '(跳过)' } else { '执行' })"
    $plan += "  Step 5  Postgres 连通性            执行"
    $plan += "  Step 6  LangGraph checkpoint schema $(if ($SkipSchemaSetup)  { '(跳过)' } else { '执行' })"
    $plan | ForEach-Object { Write-Host $_ }
    exit 0
}

# ---------------------------------------------------------------
# 主流程:严格串行
# ---------------------------------------------------------------
try {
    Step-DockerService
    Step-DaemonReady
    Step-ProxyCheck
    Step-PostgresContainer
    Step-PostgresConnect
    Step-SchemaSetup
    Write-Step '✓ pre-flight 全部通过'
    exit 0
} catch {
    Write-Host "✗ pre-flight 异常退出:$($_.Exception.Message)" -ForegroundColor Red
    # 不带 exit code 时 throw 通常会让 $LASTEXITCODE 保留上一个调用;
    # 兜底退出 99 让 Python 侧识别为异常。
    exit 99
}