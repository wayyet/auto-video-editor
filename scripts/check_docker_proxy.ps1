# ============================================================
# Docker Desktop 代理自检（防 containerd/buildkit 指向失效本地代理）
#
# 背景：
#   Docker Desktop 拉镜像时实际用的是 containerd/buildkit 的"静态系统代理"层，
#   不在 %APPDATA%\Docker\settings-store.json 的 vm.proxy 段控制范围内。
#   2026-09-09 夜间观察到该层被改成了 http://127.0.0.1:3067（Karing 端口），
#   而 Karing 没运行 → 拉镜像立即失败。
#   Docker Desktop 重启后该层自愈为 http://http.docker.internal:3128（公司 hubproxy）。
#
# 用途：
#   跑 docker pull / docker build 之前先调一次本脚本，发现代理指向
#   127.0.0.1:<port>（且该端口没人监听）就报错。
#   也可以加 -RestartDocker 让脚本尝试自动重启 Docker Desktop 恢复。
#
# 用法：
#   .\scripts\check_docker_proxy.ps1                  # 只检查，失败抛错
#   .\scripts\check_docker_proxy.ps1 -RestartDocker   # 检查失败时自动重启 Docker Desktop
# ============================================================

[CmdletBinding()]
param(
    [switch]$RestartDocker
)

# Windows 默认 PowerShell 控制台 GBK，中文 / 符号会乱码
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding  = [System.Text.Encoding]::UTF8
    $OutputEncoding           = [System.Text.Encoding]::UTF8
} catch { }

$ErrorActionPreference = 'Stop'
$prevErrorAction = $ErrorActionPreference

function Get-DockerProxyFromInfo {
    # 从 docker info 里取 HTTP Proxy: / HTTPS Proxy: 行
    # 注意:无代理时 Select-String 返回空数组 Object[],直接 .Trim() 会抛 "不包含 Trim 方法"
    $info = & docker info 2>&1
    $httpLine  = $info | Select-String -Pattern '^\s*HTTP Proxy:\s*(.+)$'  | Select-Object -First 1
    $httpsLine = $info | Select-String -Pattern '^\s*HTTPS Proxy:\s*(.+)$' | Select-Object -First 1
    $httpProxy  = if ($httpLine)  { ($httpLine  -replace '^\s*HTTP Proxy:\s*', '').Trim() }  else { '' }
    $httpsProxy = if ($httpsLine) { ($httpsLine -replace '^\s*HTTPS Proxy:\s*', '').Trim() } else { '' }
    return @{ Http = $httpProxy; Https = $httpsProxy }
}

function Test-LocalPortListening {
    param([string]$Url)
    # 解析 socks5://host:port 或 http://host:port 拿 host/port
    if ([string]::IsNullOrWhiteSpace($Url)) { return $false }
    $pattern = '^(?<proto>[a-z0-9+\-.]+)://(?<host>[^:]+):(?<port>\d+)/?$'
    if ($Url -notmatch $pattern) { return $true }  # 解析不了的（如 unix socket）放行
    $host = $matches['host']
    $port = [int]$matches['port']
    # host.docker.internal / localhost / 0.0.0.0 都视为本机可达目标
    $loopbackHost = @('localhost', '127.0.0.1', '::1', 'host.docker.internal', '0.0.0.0')
    if ($loopbackHost -notcontains $host) { return $true }  # 远端代理默认放行
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect($host, $port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(1500, $false) -and $tcp.Connected
        $tcp.Close()
        return $ok
    } catch {
        return $false
    }
}

function Restart-DockerDesktop {
    Write-Host '==> 尝试重启 Docker Desktop...' -ForegroundColor Cyan
    $dockerExe = (Get-Command docker -ErrorAction SilentlyContinue).Source
    if (-not $dockerExe) { throw '找不到 docker 命令' }
    # 优先用 docker desktop restart（CLI 等价 GUI 重启）
    $ErrorActionPreference = 'SilentlyContinue'
    & docker desktop restart 2>&1 | Out-Null
    $ErrorActionPreference = $prevErrorAction
    # 等 daemon 就绪
    for ($i = 0; $i -lt 60; $i++) {
        $ErrorActionPreference = 'SilentlyContinue'
        $ok = (& docker info 2>&1) -notmatch 'cannot connect|Cannot connect'
        $ErrorActionPreference = $prevErrorAction
        if ($ok) {
            Write-Host "==> Docker Desktop 已就绪（${i}s）" -ForegroundColor Green
            return
        }
        Start-Sleep -Seconds 1
    }
    throw 'Docker Desktop 重启后 60s 内 daemon 未就绪'
}

# --- 主流程 ---
Write-Host '==> 检查 Docker 代理配置...' -ForegroundColor Cyan
$proxy = Get-DockerProxyFromInfo
Write-Host ("    HTTP Proxy  : {0}" -f ($(if ($proxy.Http)  { $proxy.Http  } else { '<空>' })))
Write-Host ("    HTTPS Proxy : {0}" -f ($(if ($proxy.Https) { $proxy.Https } else { '<空>' })))

$issues = @()
foreach ($pair in @('Http', 'Https')) {
    $url = $proxy.$pair
    if ([string]::IsNullOrWhiteSpace($url)) { continue }
    if ($url -match '^https?://(127\.0\.0\.1|localhost|0\.0\.0\.0):(\d+)/?$') {
        $port = [int]$matches[2]
        $listening = Test-LocalPortListening -Url $url
        if (-not $listening) {
            $issues += "${pair} 指向 ${url}，但端口 ${port} 无监听"
        }
    }
}

if ($issues.Count -eq 0) {
    Write-Host '✓ 代理配置可用' -ForegroundColor Green
    exit 0
}

Write-Host ''
Write-Host "⚠️ 检测到代理问题（${($issues.Count)} 项）：" -ForegroundColor Yellow
foreach ($msg in $issues) { Write-Host "    - $msg" -ForegroundColor Yellow }

if ($RestartDocker) {
    Restart-DockerDesktop
    Write-Host ''
    Write-Host '==> 重新检查代理...' -ForegroundColor Cyan
    $proxy = Get-DockerProxyFromInfo
    Write-Host ("    HTTP Proxy  : {0}" -f ($(if ($proxy.Http)  { $proxy.Http  } else { '<空>' })))
    Write-Host ("    HTTPS Proxy : {0}" -f ($(if ($proxy.Https) { $proxy.Https } else { '<空>' })))
    $stillBad = $false
    foreach ($pair in @('Http', 'Https')) {
        $url = $proxy.$pair
        if ([string]::IsNullOrWhiteSpace($url)) { continue }
        if ($url -match '^https?://(127\.0\.0\.1|localhost|0\.0\.0\.0):(\d+)/?$') {
            if (-not (Test-LocalPortListening -Url $url)) { $stillBad = $true }
        }
    }
    if ($stillBad) {
        throw '重启 Docker Desktop 后代理仍异常，请人工排查（可能要启对应的代理进程）'
    }
    Write-Host '✓ 重启后代理配置可用' -ForegroundColor Green
    exit 0
} else {
    Write-Host ''
    Write-Host '可能原因：containerd/buildkit 静态代理指向了本机失效端口。' -ForegroundColor Yellow
    Write-Host '  - 加 -RestartDocker 参数让脚本自动 docker desktop restart'
    Write-Host '  - 或手动跑: docker desktop restart'
    exit 1
}