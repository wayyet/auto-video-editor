# monitoring/heartbeat_monitor.ps1
# Week 3 外部心跳监控脚本 — 由 Windows 任务计划程序每 2 分钟触发。
# 设计与原文档(Week 3 详细实施计划)6.2 节一致:PowerShell 7+、UTF-8 输出修复、
# 阈值默认 120s、超时仅写本地日志。
# 告警渠道(企业微信/邮件)留 Week 4 对接。

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$heartbeatFile = 'C:\ProgramData\VideoWorkflow\heartbeat.txt'
$thresholdSeconds = 120
$logFile = 'C:\ProgramData\VideoWorkflow\heartbeat_monitor.log'

function Write-Log {
    param([string]$Message)
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Message
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

if (-not (Test-Path -LiteralPath $heartbeatFile)) {
    Write-Log '心跳文件不存在,判定编排进程从未启动或已被清理'
    exit 1
}

try {
    $lastHeartbeat = [double](Get-Content -LiteralPath $heartbeatFile -Encoding utf8 -ErrorAction Stop)
}
catch {
    Write-Log ("心跳文件读取失败: " + $_.Exception.Message)
    exit 2
}

$nowEpoch = [double](Get-Date -UFormat '%s')
$elapsed = $nowEpoch - $lastHeartbeat

if ($elapsed -gt $thresholdSeconds) {
    $rounded = [math]::Round($elapsed)
    Write-Log "心跳超时:距上次更新已 $rounded 秒,超过阈值 $thresholdSeconds 秒,判定编排进程可能已卡死或崩溃"
    # TODO:Week 4 接入企业微信 webhook / 邮件 / Windows 事件日志。本周先落本地日志。
    exit 10
}
else {
    $rounded = [math]::Round($elapsed)
    Write-Log "心跳正常,距上次更新 $rounded 秒"
    exit 0
}