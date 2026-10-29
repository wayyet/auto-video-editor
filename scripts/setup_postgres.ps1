# ============================================================
# EDB Postgres 16.15 — Windows 原生一键安装 + 初始化脚本
# ⚠️ 备用方案。本项目 Week 5 推荐走 Docker 版（更轻、可快照、可删即丢）：
#     .\scripts\start_postgres.ps1           # 启动容器 + 等 healthy
#     .\scripts\stop_postgres.ps1            # 停容器，数据卷 edb_pgdata 保留
#     .\scripts\stop_postgres.ps1 -Remove    # 停并删容器，卷仍保留
# Docker 版用镜像 quay.io/enterprisedb/postgresql:16.11-3.5-postgis-multilang，
# 默认库 langgraph / 用户 postgres / 密码 devpass，对应连接串：
#     postgresql://postgres:devpass@localhost:5432/langgraph
# ⚠️ 容器默认库只是"管理员维护库";本项目的运行时库统一为 video_workflow(计划 D1)，
#    由 align_postgres.py 增量建出(只 CREATE / GRANT,不 DROP)。
# ⚠️ 默认库 langgraph 已按方案 U4 在阶段 E 归档删除;管理操作一律连维护库
#    postgres(postgresql://postgres:devpass@localhost:5432/postgres)。
# 仅当 Docker Desktop 跑不了或需要 native Windows 服务时才用本脚本。
# 用法：在“管理员: Windows PowerShell”窗口里运行此脚本
# ============================================================
$ErrorActionPreference = 'Stop'

$installer  = "$env:TEMP\edb_pg_install\postgresql-16.15-installer.exe"
$pgRoot     = "C:\Program Files\PostgreSQL\16"
$pgBin      = "$pgRoot\bin"
$pgData     = "$pgRoot\data"
$pgPassword = 'postgres_dev_2026'
$pgPort     = 5432

# 1) 装（先确认 installer 还在）
if (-not (Test-Path $installer)) {
    throw "找不到 installer: $installer"
}

Write-Host "==> [1/4] 安装 Postgres 16.15 (3-5 分钟)..." -ForegroundColor Cyan
& $installer --mode unattended `
    --superpassword   $pgPassword `
    --servicepassword $pgPassword `
    --serverport      $pgPort `
    --datadir         $pgData `
    --locale          Default
$ec = $LASTEXITCODE
Write-Host "Installer exit code: $ec"
if ($ec -ne 0) { throw "安装失败,exit=$ec" }

# 2) PATH
$env:Path += ";$pgBin"
[Environment]::SetEnvironmentVariable(
    "Path",
    [Environment]::GetEnvironmentVariable("Path","User") + ";$pgBin",
    "User"
)
Write-Host "==> [2/4] PATH 已加入 $pgBin"

# 3) 建库 + 建账号
$env:PGPASSWORD = $pgPassword
Write-Host "==> [3/4] 建库 + 建账号..." -ForegroundColor Cyan
& "$pgBin\psql.exe" -U postgres -h localhost -c "CREATE DATABASE video_workflow;"
& "$pgBin\psql.exe" -U postgres -h localhost -c "CREATE USER ave_app WITH PASSWORD '$pgPassword';"
& "$pgBin\psql.exe" -U postgres -h localhost -c "ALTER DATABASE video_workflow OWNER TO ave_app;"
& "$pgBin\psql.exe" -U postgres -h localhost -d video_workflow -c "GRANT ALL ON SCHEMA public TO ave_app;"
& "$pgBin\psql.exe" -U postgres -h localhost -c "ALTER USER ave_app CREATEDB;"

# 4) .pgpass (免密连接)
$pgpassDir = "$env:APPDATA\postgresql"
New-Item -ItemType Directory -Force -Path $pgpassDir | Out-Null
"localhost:$pgPort:video_workflow:ave_app:$pgPassword" | `
    Out-File -FilePath "$pgpassDir\pgpass.conf" -Encoding ASCII -NoNewline
Write-Host "==> [4/4] .pgpass 已写入"

# 5) 验证
Write-Host "==> 验证连接..." -ForegroundColor Cyan
& "$pgBin\psql.exe" -U ave_app -h localhost -d video_workflow -c "SELECT version();"
Write-Host "`n==> 完成。POSTGRES_URI = postgresql://ave_app:$pgPassword@localhost:$pgPort/video_workflow" -ForegroundColor Green