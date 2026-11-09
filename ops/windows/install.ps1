# AgentOS Windows 版安装脚本。在普通（不用管理员）PowerShell 里，项目文件夹中运行：
#   powershell -ExecutionPolicy Bypass -File ops\windows\install.ps1
# 可以重复运行：每次都会补上还没装的部分（哪一项的钥匙填好了，就装哪一项的定时任务），不会弄坏已有的。
# 做的事：
#   1. 建 %USERPROFILE%\.agentos（钥匙、日志、Python 环境），生成空白配置：bridge.env、mail.env、agent.env
#   2. 建专用 Python 环境（venv），装飞书 SDK（lark-oapi）和读 PDF 的 pypdf
#   3. 让 Python 默认用 UTF-8（中文 Windows 默认 GBK，读写中文文件会乱码）
#   4. 插电时不睡眠
#   5. 在「任务计划程序」里装定时任务：飞书机器人（登录后常驻）、采集邮件和飞书、提取文字（每 10 分钟）、
#      每日复盘（23:00）、知识库备份（03:00）。电脑关着错过了，开机后补跑。
$ErrorActionPreference = "Stop"

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$State = Join-Path $env:USERPROFILE ".agentos"
$Logs = Join-Path $State "logs"
$Venv = Join-Path $State "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Pyw = Join-Path $Venv "Scripts\pythonw.exe"
$Utf8 = New-Object Text.UTF8Encoding $false

function Say($msg) { Write-Host $msg }
function Has-Value($file, $name) {
    if (-not (Test-Path $file)) { return $false }
    return [bool](Select-String -Path $file -Pattern ("^\s*" + [regex]::Escape($name) + "\s*=\s*\S") -Quiet)
}
function Write-Template($file, $text) {
    if (Test-Path $file) { return }
    [IO.File]::WriteAllText($file, ($text -replace "`r?`n", "`r`n"), $Utf8)
    Say "    已生成 $file"
}

Say "项目文件夹：$Repo"
if ($Repo -match "\s") { Say "⚠️ 项目文件夹的路径里有空格，定时任务可能出问题。建议放在 %USERPROFILE%\AgentOS。" }

# ---------- 1. 钥匙和日志的文件夹 ----------
Say "==> 1/5 配置文件夹 $State"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
Write-Template (Join-Path $State "bridge.env") @"
# 飞书开放平台 → 企业自建应用 → 凭证与基础信息
FEISHU_APP_ID=
FEISHU_APP_SECRET=
# 第一次给机器人发消息，它会回你的 open_id，填到这里后重新运行安装脚本
OWNER_OPEN_ID=
# 可选：告警和复盘同时推到微信（PushPlus 的令牌）
PUSHPLUS_TOKEN=
# 以下一般不用改
MAX_CONCURRENCY=2
STUCK_NOTICE_MIN=15
HARD_TIMEOUT_MIN=45
"@
Write-Template (Join-Path $State "mail.env") @"
# 邮箱 IMAP 只读采集。授权码在邮箱网页版「设置」里开启 IMAP 后生成，不是登录密码
# QQ 邮箱 imap.qq.com ｜ 163 邮箱 imap.163.com ｜ 腾讯企业邮 imap.exmail.qq.com
MAIL_IMAP_HOST=
MAIL_USER=
MAIL_PASSWORD=
MAIL_FOLDERS=INBOX
MAIL_INITIAL_DAYS=7
"@
Write-Template (Join-Path $State "agent.env") @"
# 用哪个底座：claude（Claude Code，默认）或 codebuddy（腾讯 WorkBuddy 的 CodeBuddy Code）
AGENT_ENGINE=claude

# ---- 底座 A：Claude 订阅（claude setup-token 打印的一年期令牌）----
CLAUDE_CODE_OAUTH_TOKEN=

# ---- 底座 B：Claude Code + 其他公司的接口（DeepSeek、Kimi、智谱等提供 Anthropic 兼容接口的）----
# 用它时上面的 CLAUDE_CODE_OAUTH_TOKEN 留空。地址和模型名以厂商文档为准。
ANTHROPIC_BASE_URL=
ANTHROPIC_AUTH_TOKEN=
ANTHROPIC_MODEL=
ANTHROPIC_SMALL_FAST_MODEL=
# Claude Code 不往 Anthropic 发统计、不自动检查更新（用 B 时国内没代理也不会卡在这上面）
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# ---- 底座 C：WorkBuddy（CodeBuddy Code）----
# 中国版密钥在 https://copilot.tencent.com/profile/ 生成；中国版必须同时设 internal
CODEBUDDY_API_KEY=
CODEBUDDY_INTERNET_ENVIRONMENT=internal
"@

# ---------- 2. Python 环境 ----------
Say "==> 2/5 Python 环境"
if (-not (Test-Path $Py)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv $Venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv $Venv }
    else { throw "没找到 Python。先运行：winget install Python.Python.3.12 ，关掉 PowerShell 重新打开，再跑本脚本。" }
    if (-not (Test-Path $Py)) { throw "建 Python 环境失败（$Venv）" }
}
& $Py -m pip install -q --disable-pip-version-check --upgrade pip
& $Py -m pip install -q --disable-pip-version-check -r (Join-Path $Repo "ops\bridge\requirements.txt") pypdf
if ($LASTEXITCODE -ne 0) { throw "装 Python 包失败。国内网络慢的话，先运行：$Py -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple ，再重跑本脚本。" }
Say "    $Py"

# ---------- 3. UTF-8 ----------
Say "==> 3/5 Python 默认用 UTF-8"
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "User")
$env:PYTHONUTF8 = "1"
Say "    已设置（新打开的 PowerShell 才生效）"

# ---------- 4. 插电不睡眠 ----------
Say "==> 4/5 插电时不睡眠"
try {
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    Say "    已设置。笔记本还要在「控制面板 → 电源选项 → 选择关闭笔记本计算机盖的功能」里，把「接通电源」改成「不采取任何操作」"
} catch { Say "    没设成功（$($_.Exception.Message)）：到「设置 → 系统 → 电源」里把「插电时睡眠」改成「从不」" }

# ---------- 5. 定时任务 ----------
Say "==> 5/5 定时任务（任务计划程序里名字都以 AgentOS- 开头）"
$me = "$env:USERDOMAIN\$env:USERNAME"
$runJob = Join-Path $Repo "ops\run_job.py"

function New-Settings([int]$limitMin) {
    $limit = if ($limitMin -gt 0) { New-TimeSpan -Minutes $limitMin } else { [TimeSpan]::Zero }
    return New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit $limit
}
function Install-Job($name, $timeoutMin, $trigger, [string[]]$cmd) {
    # 经 run_job.py 跑：写日志、超时、失败告警、healthchecks 打卡
    $argv = @("-X", "utf8", "`"$runJob`"", $name, $timeoutMin, "--") + $cmd
    $action = New-ScheduledTaskAction -Execute $Pyw -Argument ($argv -join " ") -WorkingDirectory $Repo
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "AgentOS-$name" -Action $action -Trigger $trigger -Principal $principal `
        -Settings (New-Settings ($timeoutMin + 5)) -Force | Out-Null
    Say "    ✅ AgentOS-$name"
}
function Every10() {
    # 每天 00:00 起、每 10 分钟一次、持续一天，天天如此（比「无限重复」在各版本 Windows 上都稳）
    $t = New-ScheduledTaskTrigger -Daily -At "00:00"
    $t.Repetition = (New-ScheduledTaskTrigger -Once -At "00:00" -RepetitionInterval (New-TimeSpan -Minutes 10) `
        -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
    return $t
}
$py = "python"   # run_job.py 会把它换成上面那个 Python
$ops = Join-Path $Repo "ops"

# 飞书机器人：登录 Windows 后启动，一直开着；崩溃了 1 分钟后自动重启
$bridgeEnv = Join-Path $State "bridge.env"
if ((Has-Value $bridgeEnv "FEISHU_APP_ID") -and (Has-Value $bridgeEnv "FEISHU_APP_SECRET")) {
    $action = New-ScheduledTaskAction -Execute $Pyw -Argument ("-X utf8 `"" + (Join-Path $ops "bridge\bridge.py") + "`"") -WorkingDirectory $Repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "AgentOS-Bridge" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Stop-ScheduledTask -TaskName "AgentOS-Bridge" -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName "AgentOS-Bridge"
    Say "    ✅ AgentOS-Bridge（飞书机器人，已启动；日志 $Logs\bridge.log）"
} else {
    Say "    跳过飞书机器人：bridge.env 里还没填 FEISHU_APP_ID / FEISHU_APP_SECRET"
}

$mailEnv = Join-Path $State "mail.env"
if (Has-Value $mailEnv "MAIL_PASSWORD") {
    Say "    测试邮箱登录……"
    & $Py -X utf8 (Join-Path $ops "collect_mail.py") --check
    if ($LASTEXITCODE -eq 0) { Install-Job "collect-mail" 8 (Every10) @($py, "`"$ops\collect_mail.py`"") }
    else { Say "    ⚠️ 邮箱登录失败，先检查 mail.env，邮件采集这次没装" }
} else { Say "    跳过邮件采集：mail.env 里还没填 MAIL_PASSWORD" }

if (Test-Path (Join-Path $State "feishu_user_token.json")) {
    Install-Job "collect-feishu" 8 (Every10) @($py, "`"$ops\collect_feishu.py`"")
} else { Say "    跳过飞书采集：还没以你本人身份授权（先运行 python ops\feishu_auth.py）" }

Install-Job "extract-text" 8 (Every10) @($py, "`"$ops\extract_text.py`"")

if (Has-Value $bridgeEnv "OWNER_OPEN_ID") {
    Install-Job "daily-review" 40 (New-ScheduledTaskTrigger -Daily -At "23:00") @($py, "`"$ops\daily_review.py`"")
} else { Say "    跳过每日复盘：bridge.env 里还没填 OWNER_OPEN_ID（先绑定本人）" }

$origin = $null
try { $origin = (& git -C $Repo remote get-url origin 2>$null) } catch { Say "    ⚠️ 没找到 git：winget install Git.Git" }
if ($origin) {
    Install-Job "push-kb" 15 (New-ScheduledTaskTrigger -Daily -At "03:00") @($py, "`"$ops\push_kb.py`"")
} else { Say "    跳过知识库备份：还没设置 origin（你自己的备份仓库）" }

# ---------- 底座 ----------
$engine = ((Get-Content (Join-Path $State "agent.env") -Encoding UTF8 | Where-Object { $_ -match "^\s*AGENT_ENGINE\s*=" }) -replace "^\s*AGENT_ENGINE\s*=\s*", "").Trim()
if (-not $engine) { $engine = "claude" }
$found = Get-Command $engine -ErrorAction SilentlyContinue
if (-not $found) {
    foreach ($d in @("$env:USERPROFILE\.local\bin", "$env:APPDATA\npm")) {
        if ((Test-Path (Join-Path $d "$engine.exe")) -or (Test-Path (Join-Path $d "$engine.cmd"))) { $found = $true }
    }
}
Say ""
if ($found) { Say "底座：$engine 已安装。" } else { Say "⚠️ 底座 $engine 还没装，按教程「装底座」那一步装。" }
if ($engine -eq "claude" -and -not (Test-Path "C:\Program Files\Git\bin\bash.exe")) {
    Say "⚠️ 没找到 Git（Claude Code 在 Windows 上要用 Git 自带的 bash）：winget install Git.Git"
}
Say "全部定时任务：任务计划程序 → 任务计划程序库，名字以 AgentOS- 开头。日志在 $Logs"
