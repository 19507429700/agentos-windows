# AgentOS：Windows 这一头的设置（在「以管理员身份运行」的 PowerShell 里跑一次）
#   powershell -ExecutionPolicy Bypass -File setup-windows.ps1
#   发行版名字不是 Ubuntu 时：... -File setup-windows.ps1 -Distro Ubuntu-24.04
# 做三件事：
#   1. 插电时不睡眠、不休眠（电脑睡着了，飞书机器人和定时任务都停）
#   2. 建一个计划任务「AgentOS-WSL」：登录 Windows 后在后台启动 WSL，并一直开着
#      （WSL 没有窗口开着时会自己关掉，里面的飞书机器人、定时任务也跟着停）
#   3. 马上启动一次，不用等下次登录
param([string]$Distro = "Ubuntu")
$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "请右键 PowerShell →「以管理员身份运行」，再跑这个脚本。"; exit 1 }

$distros = (wsl.exe -l -q) -replace "`0", "" | Where-Object { $_.Trim() -ne "" } | ForEach-Object { $_.Trim() }
if ($distros -notcontains $Distro) {
    Write-Host "没找到 WSL 发行版「$Distro」。已安装的有：$($distros -join '、')"
    Write-Host "先运行 wsl --install -d Ubuntu 装好 Ubuntu；名字不同就加参数 -Distro <名字>。"
    exit 1
}

Write-Host "==> 1/3 插电时不睡眠、不休眠"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
Write-Host "    已设置（用电池时照常睡眠；笔记本合盖的行为在「电源选项 → 选择关闭笔记本计算机盖的功能」里改成「不采取任何操作」）"

Write-Host "==> 2/3 登录后在后台启动 WSL 并保持运行"
$conhost = Join-Path $env:WINDIR "System32\conhost.exe"
$wslArgs = "-d $Distro --exec /bin/sleep infinity"
if (Test-Path $conhost) {
    # conhost --headless：不弹黑窗口
    $action = New-ScheduledTaskAction -Execute $conhost -Argument "--headless wsl.exe $wslArgs"
} else {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -Command wsl.exe $wslArgs"
}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "AgentOS-WSL" -Action $action -Trigger $trigger -Settings $settings `
    -Description "AgentOS：保持 WSL 在后台运行（飞书机器人、定时任务在里面）" -Force | Out-Null
Write-Host "    已建计划任务 AgentOS-WSL（任务计划程序里能看到）"

Write-Host "==> 3/3 现在启动"
Start-ScheduledTask -TaskName "AgentOS-WSL"
Start-Sleep -Seconds 5
$state = (Get-ScheduledTask -TaskName "AgentOS-WSL").State
Write-Host "    计划任务状态：$state（Running 就是在跑）"
Write-Host ""
Write-Host "完成。之后每次登录 Windows，WSL 都会在后台自己启动；关机重启后要登录一次 Windows。"
