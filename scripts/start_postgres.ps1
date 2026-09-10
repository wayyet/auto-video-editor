# ============================================================
# 启动本地 EDB Postgres 16.11（Docker 版）
# 配套：auto-video-editor Week 5 PostgresSaver
# 用法：在项目根目录执行 .\scripts\start_postgres.ps1
# ============================================================
$ErrorActionPreference = 'Stop'

$Image = 'quay.io/enterprisedb/postgresql:16.11-3.5-postgis-multilang'
$Container = 'edb-postgres16'
$Volume = 'edb_pgdata'
$PgUser = 'postgres'
$PgPassword = 'devpass'
$PgDb = 'langgraph'
$HostPort = 5432

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

Write-Host "==> [1/3] 拉取镜像 $Image" -ForegroundColor Cyan
& docker pull $Image
Assert-DockerSuccess '拉取 Docker 镜像失败'

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
Write-Host "==> 完成。POSTGRES_URI=$uri" -ForegroundColor Green
Write-Host '==> 下一步：$env:POSTGRES_URI="postgresql://postgres:devpass@localhost:5432/langgraph"; .\.venv\Scripts\python.exe scripts\setup_postgres_schema.py' -ForegroundColor Green
