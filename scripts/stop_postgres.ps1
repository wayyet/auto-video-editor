# ============================================================
# 停止本地 EDB Postgres 16.11 容器（数据卷保留）
# 用法：.\scripts\stop_postgres.ps1
#       .\scripts\stop_postgres.ps1 -Remove
# ============================================================
[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$Container = 'edb-postgres16'

$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($null -eq $dockerCommand) {
    throw '找不到 docker。请先启动 Docker Desktop。'
}

$existingContainers = @(& docker ps -a --filter "name=^${Container}$" --format '{{.Names}}')
if ($LASTEXITCODE -ne 0) {
    throw '查询 Docker 容器失败'
}
if ($existingContainers.Count -eq 0) {
    Write-Host "容器 $Container 不存在，无需处理。" -ForegroundColor Yellow
    exit 0
}

$runningContainers = @(& docker ps --filter "name=^${Container}$" --format '{{.Names}}')
if ($LASTEXITCODE -ne 0) {
    throw '查询 Docker 容器运行状态失败'
}
if ($runningContainers.Count -gt 0) {
    & docker stop $Container
    if ($LASTEXITCODE -ne 0) {
        throw "停止容器 $Container 失败"
    }
} else {
    Write-Host "容器 $Container 已停止。" -ForegroundColor Yellow
}

if ($Remove) {
    & docker rm $Container
    if ($LASTEXITCODE -ne 0) {
        throw "删除容器 $Container 失败"
    }
    Write-Host "容器 $Container 已删除；命名卷 edb_pgdata 仍保留。" -ForegroundColor Green
} else {
    Write-Host "容器 $Container 已停止；数据卷 edb_pgdata 仍保留。" -ForegroundColor Green
}

# 完全清理会删除 checkpoint 数据，执行前请确认：
# docker rm -f edb-postgres16
# docker volume rm edb_pgdata
