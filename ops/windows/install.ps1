# AgentOS Windows 版安装脚本。在普通（不用管理员）PowerShell 里，项目文件夹中运行：
#   powershell -ExecutionPolicy Bypass -File ops\windows\install.ps1
# 可以重复运行：每次都会补上还没装的部分（哪一项的钥匙填好了，就装哪一项的定时任务），不会弄坏已有的。
# 做的事：
#   1. 建 %USERPROFILE%\.agentos（钥匙、日志、Python 环境），生成空白配置：bridge.env、mail.env、agent.env、wechat.env
#   2. 建专用 Python 环境（venv），装飞书 SDK（lark-oapi）、读 PDF 的 pypdf、读微信库、抓公众号文章、微信语音转文字用的几个包
#   3. 让 Python 默认用 UTF-8（中文 Windows 默认 GBK，读写中文文件会乱码）
#   4. 插电时不睡眠
#   5. 在「任务计划程序」里装定时任务：飞书机器人（登录后常驻）、采集邮件、飞书、微信、讯飞录音笔，抓公众号文章，提取文字（每 10 分钟）、
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
Write-Template (Join-Path $State "wechat.env") @"
# 微信聊天采集（教程第十三步）。都可以不填，用默认值。
# 钥匙文件：默认 %USERPROFILE%\.wechat-cli\all_keys.json（WorkBuddy 的 wechat-cli 技能 init 后放在这里）
WECHAT_KEYS=
# 采哪些群：填群名里的关键词，逗号分隔；私聊一律全采。留空只采私聊
WECHAT_GROUPS=
# 第一次采集每个会话往回看几天
WECHAT_INITIAL_DAYS=3
"@

# ---------- 2. Python 环境 ----------
Say "==> 2/5 Python 环境"
function Find-Python {
    # 挨个试，用第一个真能运行、版本 3.9 以上的。电脑上登记过的 Python 可能已经被删了（py 的默认版本就会坏）
    $candidates = @("py -3.12", "py -3.13", "py -3.11", "py -3.14", "py -3.10", "py -3.9", "py -3", "python")
    foreach ($c in $candidates) {
        $parts = $c.Split(" ")
        if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
        try {
            $rest = @($parts | Select-Object -Skip 1)
            $out = (& $parts[0] @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null)
            if ($LASTEXITCODE -eq 0 -and "$out" -match "^3\.(\d+)$" -and [int]$Matches[1] -ge 9) {
                return ,$parts
            }
        } catch { }
    }
    return $null
}
if (-not (Test-Path $Py)) {
    $found = Find-Python
    if ($null -eq $found) { throw "没找到能用的 Python（3.9 以上）。先运行：winget install Python.Python.3.12 ，关掉 PowerShell 重新打开，再跑本脚本。" }
    Say "    用 $($found -join ' ') 建专用环境"
    $rest = @($found | Select-Object -Skip 1)
    & $found[0] @rest -m venv $Venv
    if (-not (Test-Path $Py)) { throw "建 Python 环境失败（$Venv）" }
}
# 默认用清华镜像（国内直连快）；自己用 pip config 配过镜像的照旧用自己的；在国外想用官方源：先 $env:AGENTOS_PIP_INDEX="https://pypi.org/simple"
$pipIndex = $env:AGENTOS_PIP_INDEX
if (-not $pipIndex) {   # 没配过时 pip 会往错误输出写一行，脚本是「出错就停」模式，所以要接住
    try { $pipIndex = (& $Py -m pip config get global.index-url 2>$null) } catch { $pipIndex = $null }
}
if (-not $pipIndex) { $pipIndex = "https://pypi.tuna.tsinghua.edu.cn/simple" }
Say "    装 Python 包（来源 $pipIndex）……"
& $Py -m pip install -q --disable-pip-version-check -i $pipIndex --upgrade pip
& $Py -m pip install -q --disable-pip-version-check -i $pipIndex -r (Join-Path $Repo "ops\bridge\requirements.txt") pypdf cryptography zstandard requests beautifulsoup4 faster-whisper silk-python
if ($LASTEXITCODE -ne 0) { throw "装 Python 包失败（来源 $pipIndex）。看看网络，或者换个来源再重跑：`$env:AGENTOS_PIP_INDEX=`"https://mirrors.aliyun.com/pypi/simple`"" }
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
$jobFailed = @()
function Register-Job($taskName, $action, $trigger, $principal, $settings) {
    # 同名任务以前是在「管理员」PowerShell 里装的，普通 PowerShell 改不了它，会报「拒绝访问」：
    # 先试着删掉旧的再装；还不行就记下来，接着装别的，最后统一告诉你怎么处理（不让一个任务卡住后面所有步骤）
    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
            -Settings $settings -Force -ErrorAction Stop | Out-Null
        return $true
    } catch {
        try {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
                -Settings $settings -ErrorAction Stop | Out-Null
            return $true
        } catch {
            $script:jobFailed += $taskName
            Say "    ❌ $taskName 没装上：$($_.Exception.Message.Trim())"
            return $false
        }
    }
}
function Install-Job($name, $timeoutMin, $trigger, [string[]]$cmd) {
    # 经 run_job.py 跑：写日志、超时、失败告警、healthchecks 打卡
    $argv = @("-X", "utf8", "`"$runJob`"", $name, $timeoutMin, "--") + $cmd
    $action = New-ScheduledTaskAction -Execute $Pyw -Argument ($argv -join " ") -WorkingDirectory $Repo
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    if (Register-Job "AgentOS-$name" $action $trigger $principal (New-Settings ($timeoutMin + 5))) {
        Say "    ✅ AgentOS-$name"
    }
}
function Every10() {
    # 每天 00:00 起、每 10 分钟一次、持续一天，天天如此（比「无限重复」在各版本 Windows 上都稳）
    $t = New-ScheduledTaskTrigger -Daily -At "00:00"
    $t.Repetition = (New-ScheduledTaskTrigger -Once -At "00:00" -RepetitionInterval (New-TimeSpan -Minutes 10) `
        -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
    return $t
}
# 别叫 $py：PowerShell 变量不分大小写，会把上面的 $Py（专用环境）盖掉，后面的检查就跑到系统 Python 上去了
$jobPy = "python"   # run_job.py 会把它换成上面那个 Python
$ops = Join-Path $Repo "ops"

# 飞书机器人：登录 Windows 后启动，一直开着；崩溃了 1 分钟后自动重启
$bridgeEnv = Join-Path $State "bridge.env"
if ((Has-Value $bridgeEnv "FEISHU_APP_ID") -and (Has-Value $bridgeEnv "FEISHU_APP_SECRET")) {
    $action = New-ScheduledTaskAction -Execute $Pyw -Argument ("-X utf8 `"" + (Join-Path $ops "bridge\bridge.py") + "`"") -WorkingDirectory $Repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    if (Register-Job "AgentOS-Bridge" $action $trigger $principal $settings) {
        Stop-ScheduledTask -TaskName "AgentOS-Bridge" -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName "AgentOS-Bridge"
        Say "    ✅ AgentOS-Bridge（飞书机器人，已启动；日志 $Logs\bridge.log）"
    }
} else {
    Say "    跳过飞书机器人：bridge.env 里还没填 FEISHU_APP_ID / FEISHU_APP_SECRET"
}

$mailEnv = Join-Path $State "mail.env"
if (Has-Value $mailEnv "MAIL_PASSWORD") {
    Say "    测试邮箱登录……"
    & $Py -X utf8 (Join-Path $ops "collect_mail.py") --check
    if ($LASTEXITCODE -eq 0) { Install-Job "collect-mail" 8 (Every10) @($jobPy, "`"$ops\collect_mail.py`"") }
    else { Say "    ⚠️ 邮箱登录失败，先检查 mail.env，邮件采集这次没装" }
} else { Say "    跳过邮件采集：mail.env 里还没填 MAIL_PASSWORD" }

if (Test-Path (Join-Path $State "feishu_user_token.json")) {
    Install-Job "collect-feishu" 8 (Every10) @($jobPy, "`"$ops\collect_feishu.py`"")
} else { Say "    跳过飞书采集：还没以你本人身份授权（先运行 py ops\feishu_auth.py）" }

# 微信：有了钥匙文件（第十三步取的）、并且检查能读出会话，才装采集和公众号抓取
$wechatEnv = Join-Path $State "wechat.env"
$wxKeys = ""
if (Test-Path $wechatEnv) {
    $wxKeys = ((Get-Content $wechatEnv -Encoding UTF8 | Where-Object { $_ -match "^\s*WECHAT_KEYS\s*=" }) -replace "^\s*WECHAT_KEYS\s*=\s*", "").Trim().Trim('"')
}
if (-not $wxKeys) { $wxKeys = Join-Path $env:USERPROFILE ".wechat-cli\all_keys.json" }
if (Test-Path $wxKeys) {
    Say "    检查微信……"
    & $Py -X utf8 (Join-Path $ops "collect_wechat.py") --check
    if ($LASTEXITCODE -eq 0) {
        Install-Job "collect-wechat" 8 (Every10) @($jobPy, "`"$ops\collect_wechat.py`"")
        Install-Job "fetch-wechat-articles" 15 (Every10) @($jobPy, "`"$ops\fetch_wechat_articles.py`"")
        # 语音转文字的模型（约 460MB，只下一次）。wechat.env 里写了 WECHAT_VOICE=0 就不下
        if (Select-String -Path $wechatEnv -Pattern "^\s*WECHAT_VOICE\s*=\s*0" -Quiet -ErrorAction SilentlyContinue) {
            Say "    语音转文字已关（WECHAT_VOICE=0），不下模型"
        } else {
            & $Py -X utf8 (Join-Path $ops "download_whisper.py")
            if ($LASTEXITCODE -eq 0) { Say "    ✅ 语音转文字（微信语音会写成「[语音 14秒] 转出的文字」）" }
            else { Say "    ⚠️ 语音模型没下好，语音这次只留「[语音]」；重跑本脚本会接着下" }
        }
    } else { Say "    ⚠️ 微信库读不出来（看上面的原因），微信采集这次没装" }
} else { Say "    跳过微信采集：还没取微信钥匙（教程第十三步）" }

# 讯飞录音笔：运行过 py ops\install_iflyrec.py（装好客户端、授权）才装
if (Test-Path (Join-Path $State "iflyrec-skill\scripts\recording_assistant.py")) {
    Install-Job "collect-iflyrec" 8 (Every10) @($jobPy, "`"$ops\collect_iflyrec.py`"")
} else { Say "    跳过讯飞录音笔：没用的话不用管；要接就先运行 py ops\install_iflyrec.py（教程第十五步）" }

Install-Job "collect-ai" 8 (Every10) @($jobPy, "`"$ops\collect_ai_chats.py`"")

Install-Job "extract-text" 8 (Every10) @($jobPy, "`"$ops\extract_text.py`"")

if (Has-Value $bridgeEnv "OWNER_OPEN_ID") {
    Install-Job "daily-review" 40 (New-ScheduledTaskTrigger -Daily -At "23:00") @($jobPy, "`"$ops\daily_review.py`"")
} else { Say "    跳过每日复盘：bridge.env 里还没填 OWNER_OPEN_ID（先绑定本人）" }

# 知识库备份：origin 设成你自己的仓库才装；origin 还是模板仓库时绝不装（免得把知识库推进模板）
$why = $null
try { $why = (& $Py -X utf8 (Join-Path $ops "push_kb.py") --check 2>&1) } catch { $why = $_.Exception.Message }
if ($LASTEXITCODE -eq 0) {
    Install-Job "push-kb" 15 (New-ScheduledTaskTrigger -Daily -At "03:00") @($jobPy, "`"$ops\push_kb.py`"")
} else {
    Unregister-ScheduledTask -TaskName "AgentOS-push-kb" -Confirm:$false -ErrorAction SilentlyContinue
    Say "    跳过知识库备份：$why"
}

# ---------- 底座 ----------
$engine = ((Get-Content (Join-Path $State "agent.env") -Encoding UTF8 | Where-Object { $_ -match "^\s*AGENT_ENGINE\s*=" }) -replace "^\s*AGENT_ENGINE\s*=\s*", "").Trim()
if (-not $engine) { $engine = "claude" }
# 和机器人真正运行时用同一套找法（run_agent.py）：CodeBuddy 装在 %LOCALAPPDATA%\codebuddy\bin、PATH 还没刷新也算装了
$found = & $Py -X utf8 -c "import sys; sys.path.insert(0, r'$ops'); import run_agent; e, env = run_agent.build_env(); print(run_agent.find_exe(e, env) or '')"
Say ""
if ($found) { Say "底座：$engine 已安装（$found）。" } else { Say "⚠️ 底座 $engine 还没装，按教程「装底座」那一步装。" }
if ($engine -eq "claude") {
    # Claude Code 在 Windows 上要用 Git 自带的 bash。Git 不一定装在 C 盘，顺着 git 命令的位置找
    $bash = & $Py -X utf8 -c "import sys; sys.path.insert(0, r'$ops'); import run_agent; print(run_agent.find_git_bash() or '')"
    if ($bash) { Say "Git 的 bash：$bash" }
    else { Say "⚠️ 没找到 Git 自带的 bash.exe（Claude Code 要用）。装了 Git 的话，在 agent.env 里加一行 CLAUDE_CODE_GIT_BASH_PATH=你的Git目录\bin\bash.exe" }
}
Say "全部定时任务：任务计划程序 → 任务计划程序库，名字以 AgentOS- 开头。日志在 $Logs"
if ($jobFailed.Count -gt 0) {
    Say ""
    Say "⚠️ 有 $($jobFailed.Count) 个定时任务没装上：$($jobFailed -join '、')"
    Say "   多半是以前在「管理员」PowerShell 里装过，普通 PowerShell 改不了。处理：右键开始菜单 →「终端(管理员)」，运行"
    foreach ($t in $jobFailed) { Say "     Unregister-ScheduledTask -TaskName $t -Confirm:`$false" }
    Say "   关掉管理员窗口，回到普通 PowerShell 再运行一次本脚本。"
}
