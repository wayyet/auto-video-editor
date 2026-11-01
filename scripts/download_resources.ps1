# =============================================================================
#  OpenStoryline · Windows 资源下载脚本（替代官方仅限 Linux/Mac 的 download.sh）
#  作用：下载并解压 models.zip 与 resource.zip 到正确目录。
#  用法：在 FireRed-OpenStoryline 仓库根目录用 PowerShell 运行：
#        powershell -ExecutionPolicy Bypass -File .\download_resources.ps1
# =============================================================================

$ErrorActionPreference = "Stop"
$base = "https://image-url-2-feature-1251524319.cos.ap-shanghai.myqcloud.com/openstoryline"

Write-Host "[i] 准备目录 .storyline / resource ..." -ForegroundColor Blue
New-Item -ItemType Directory -Force -Path ".storyline" | Out-Null
New-Item -ItemType Directory -Force -Path ".storyline\models" | Out-Null
New-Item -ItemType Directory -Force -Path "resource" | Out-Null

function Get-Zip($url, $out) {
    if (Test-Path $out) { Write-Host "[i] 已存在，跳过下载: $out" -ForegroundColor Yellow; return }
    Write-Host "[i] 下载 $url" -ForegroundColor Blue
    # 用 Windows 自带 curl.exe（比 Invoke-WebRequest 快很多，适合大文件）
    curl.exe -L --fail -o $out $url
    if ($LASTEXITCODE -ne 0) { throw "下载失败: $url" }
}

# 1) 模型 → .storyline\models\
Get-Zip "$base/models.zip" ".storyline\models.zip"
Write-Host "[i] 解压 models.zip → .storyline\models\" -ForegroundColor Blue
Expand-Archive -Path ".storyline\models.zip" -DestinationPath ".storyline\models" -Force
Remove-Item ".storyline\models.zip" -Force

# 2) 资源 → resource\
Get-Zip "$base/resource.zip" ".storyline\resource.zip"
Write-Host "[i] 解压 resource.zip → resource\" -ForegroundColor Blue
Expand-Archive -Path ".storyline\resource.zip" -DestinationPath "resource" -Force
Remove-Item ".storyline\resource.zip" -Force

# 3) Web UI 图标（非必需，失败不影响主流程）
$webBase = "https://image-url-2-feature-1251524319.cos.ap-shanghai.myqcloud.com/zailin/datasets/open_storyline"
$files = @("brand_black.png","brand_white.png","logo.png","dice.png","github.png","node_map.png","user_guide.png")
if (Test-Path "web\static") {
    foreach ($f in $files) {
        try { curl.exe -L --fail -s -o "web\static\$f" "$webBase/$f" } catch { Write-Host "[!] 跳过 $f" -ForegroundColor Yellow }
    }
}

# 4) 自检关键模型文件是否就位
$weights = ".storyline\models\transnetv2-pytorch-weights.pth"
if (Test-Path $weights) {
    Write-Host "[OK] 镜头分割模型已就位: $weights" -ForegroundColor Green
} else {
    Write-Host "[!] 未找到 $weights —— 解压目录可能多套了一层，请到 .storyline\models 下确认实际路径，" -ForegroundColor Yellow
    Write-Host "    必要时把 .pth 文件移到 .storyline\models\ 下，或改 config.toml 的 split_shots.transnet_weights。" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "[✓] 资源下载完成。下一步: pip install -r requirements.txt" -ForegroundColor Green
