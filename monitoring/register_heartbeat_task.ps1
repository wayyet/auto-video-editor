# monitoring/register_heartbeat_task.ps1
# Week 3 一次性执行 — 把 heartbeat_monitor.ps1 注册到 Windows 任务计划程序。
# 用法(管理员 PowerShell):
#   .\register_heartbeat_task.ps1
# 或带参数覆盖路径:
#   .\register_heartbeat_task.ps1 -ScriptPath "D:\other\heartbeat_monitor.ps1"

[CmdletBinding()]
param(
    [string]$ScriptPath = "E:\Documents\kuaishou\auto-video-editor\monitoring\heartbeat_monitor.ps1",
    [string]$TaskName = "VideoWorkflow-HeartbeatMonitor",
    [int]$IntervalMinutes = 2
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    Write-Error "脚本路径不存在: $ScriptPath"
    exit 1
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger `
    -Description '视频剪辑自动化工作流编排进程心跳监控' `
    -RunLevel Highest

Write-Output "已注册任务: $TaskName (每 $IntervalMinutes 分钟触发一次,执行 $ScriptPath)"
Write-Output "查询命令: Get-ScheduledTask -Name $TaskName"
Write-Output "手动执行: & $ScriptPath"