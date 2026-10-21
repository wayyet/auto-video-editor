# ============================================================
# 启动本地 EDB Postgres 16.11（Docker 版）
# 配套：auto-video-editor Week 5 PostgresSaver
# 用法：在项目根目录执行 .\scripts\start_postgres.ps1
# ============================================================
[CmdletBinding()]
param(
    # 目标连接串。传了且与管理员 URI 不同,容器 healthy 后会调 align_postgres.py
    # 增量对齐角色/库(只 CREATE / GRANT,不 DROP、不删库)。不传 = 只保证管理员身份可用。
    [string]$TargetUri
)

$ErrorActionPreference = 'Stop'

$Image = 'quay.io/enterprisedb/postgresql:16.11-3.5-postgis-multilang'
$Container = 'edb-postgres16'
$Volume = 'edb_pgdata'
$PgUser = 'postgres'
$PgPassword = 'devpass'
$PgDb = 'langgraph'
$HostPort = 5432

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Resolve-Path (Join-Path $ScriptDir '..')

$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($null -eq $dockerCommand) {
    throw '找不到 docker。请先启动 Docker Desktop。'
}

function Assert-DockerSuccess {
    param([string]$Message)

    if ($LASTEXITCODE -ne 0) {
        throw "$Message（exit=$LASTEXITCODE）"
    }
}

function Get-MaskedUri {
    # 把 URI 里的密码替换成 ***,避免明文进日志
    param([string]$Uri)

    if ([string]::IsNullOrWhiteSpace($Uri)) { return '<空>' }
    try {
        $u = [System.Uri]$Uri
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

Write-Host "==> [1/3] 拉取镜像 $Image" -ForegroundColor Cyan

# 拉取前先看镜像是否已本地缓存,避免 daemon 冷启动时第一次出网
# TLS handshake 抖动导致 pull 失败(常见于 Windows + Docker Desktop + http_proxy 链路)
$prevErrorActionPull = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
$null = & docker image inspect $Image --format '{{.Id}}' 2>&1
$cached = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevErrorActionPull

& docker pull $Image
if ($LASTEXITCODE -ne 0) {
    if ($cached) {
        Write-Host "==> pull 失败,但镜像已本地缓存,继续(daemon 冷启动 TLS 抖动常见)" -ForegroundColor Yellow
    } else {
        throw '拉取 Docker 镜像失败'
    }
}

Write-Host "==> [2/3] 确保命名卷 $Volume 存在" -ForegroundColor Cyan
$prevErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
$null = & docker volume inspect $Volume 2>&1
$ErrorActionPreference = $prevErrorAction
if ($LASTEXITCODE -ne 0) {
    & docker volume create $Volume
    Assert-DockerSuccess '创建 Docker 命名卷失败'
} else {
    Write-Host "==> 命名卷 $Volume 已存在" -ForegroundColor Yellow
}

Write-Host "==> [3/3] 确保容器 $Container 运行" -ForegroundColor Cyan
$existingContainers = @(& docker ps -a --filter "name=^${Container}$" --format '{{.Names}}')
Assert-DockerSuccess '查询 Docker 容器失败'

if ($existingContainers.Count -gt 0) {
    & docker start $Container
    Assert-DockerSuccess "启动容器 $Container 失败"
} else {
    & docker run -d `
        --name $Container `
        -e "POSTGRES_USER=$PgUser" `
        -e "POSTGRES_PASSWORD=$PgPassword" `
        -e "POSTGRES_DB=$PgDb" `
        -v "${Volume}:/var/lib/postgresql/data" `
        -p "${HostPort}:5432" `
        --restart unless-stopped `
        --health-cmd "pg_isready -U $PgUser -d $PgDb" `
        --health-interval 5s `
        --health-timeout 5s `
        --health-retries 10 `
        $Image
    Assert-DockerSuccess "创建容器 $Container 失败"
}

Write-Host '==> 等待 healthcheck...' -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(30)
$healthy = $false
$prevErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
while ((Get-Date) -lt $deadline) {
    $status = @(& docker inspect --format '{{.State.Health.Status}}' $Container 2>&1)
    if ($LASTEXITCODE -eq 0 -and ($status -join "`n").Trim() -eq 'healthy') {
        $healthy = $true
        break
    }
    Start-Sleep -Seconds 1
}
$ErrorActionPreference = $prevErrorAction

if (-not $healthy) {
    $ErrorActionPreference = 'SilentlyContinue'
    $state = @(& docker inspect --format '{{.State.Status}}' $Container 2>&1)
    $ErrorActionPreference = 'SilentlyContinue'  # keep suppressing for logs
    & docker logs --tail 30 $Container
    $ErrorActionPreference = $prevErrorAction
    throw "容器 $Container 未在 30 秒内进入 healthy 状态；当前状态：$($state -join '')"
}

& docker ps --filter "name=^${Container}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
Assert-DockerSuccess '查询容器状态失败'

$uri = "postgresql://${PgUser}:${PgPassword}@localhost:${HostPort}/${PgDb}"
Write-Host ''
Write-Host "==> 完成。POSTGRES_URI=$(Get-MaskedUri $uri)" -ForegroundColor Green
Write-Host '==> 下一步：$env:POSTGRES_URI="postgresql://postgres:devpass@localhost:5432/langgraph"; .\.venv\Scripts\python.exe scripts\setup_postgres_schema.py' -ForegroundColor Green

# ---------------------------------------------------------------
# 角色/库对齐(可选):目标 URI 与管理员 URI 不同时,增量补齐角色与库。
# 只 CREATE / GRANT,不 DROP、不删库、不删卷。对齐失败 → 退出码 20。
# ---------------------------------------------------------------
if (-not [string]::IsNullOrWhiteSpace($TargetUri)) {
    if ($TargetUri -eq $uri) {
        Write-Host '==> 目标 URI 与管理员 URI 一致,跳过对齐' -ForegroundColor Yellow
    } else {
        Write-Host ''
        Write-Host "==> 对齐目标角色/库: $(Get-MaskedUri $TargetUri)" -ForegroundColor Cyan
        $alignScript = Join-Path $ScriptDir 'align_postgres.py'
        $pythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path $pythonExe)) { $pythonExe = 'python' }
        if (-not (Test-Path $alignScript)) {
            Write-Host "✗ 找不到 $alignScript" -ForegroundColor Red
            exit 20
        }
        & $pythonExe $alignScript --target-uri $TargetUri --admin-uri $uri
        if ($LASTEXITCODE -ne 0) {
            Write-Host "✗ 角色/库对齐失败 (exit=$LASTEXITCODE)" -ForegroundColor Red
            exit 20
        }
        Write-Host '==> 角色/库对齐完成' -ForegroundColor Green
    }
}
