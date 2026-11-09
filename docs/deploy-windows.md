# AgentOS 部署指南：Windows

写于 2026-11-09。这是 Windows 版的第一份安装说明，**还没有在真的 Windows 电脑上完整跑过**。
标「⚠️ 要实测」的地方，是在 Mac 上没法验证的。哪一步卡住了，把屏幕上的报错原样记下来（见最后的「测试记录」）。

---

## 先看懂：它在 Windows 上怎么跑

- 整套系统直接跑在 Windows 上，**不用装 Linux、不用虚拟机**。手机上装飞书当遥控器，手机那头什么都不用装。
- 用到的软件：Python（跑脚本）、Git（拿代码、备份）、LibreOffice（出 Word / PDF），加上一个「底座」。
- **底座**就是真正干活的 AI，三选一：
  - **A. Claude 订阅**：效果最好，国内要代理；
  - **B. Claude Code + 其他公司的接口**（DeepSeek 等）：国内直连，不用代理，按用量付费；
  - **C. WorkBuddy**（腾讯的 CodeBuddy Code）：国内直连。
- 定时任务（采集、复盘、备份）和飞书机器人，都放在 Windows 自带的「**任务计划程序**」里，开机登录后自动在后台运行，不弹窗口。
- **这一版没有的**：微信聊天采集、公众号文章抓取（Windows 版微信的读取方法和 Mac 不同，还没做）。

**大概要多久**：第一次装 1.5～2.5 小时。其中装软件 20 分钟，飞书后台点权限 30～60 分钟。

### 和 Mac 版在安全上的一个区别（要知道）

Mac 版专门建了一个 agentos 账号给 AI 用，AI **在系统层面**就读不到你的钥匙。Windows 上这样做太麻烦，所以这一版 **AI 和你用同一个 Windows 账号**，靠下面几道限制护住钥匙：
- 钥匙都放在 `C:\Users\你的用户名\.agentos`，**权限规则禁止 AI 读这个文件夹**，也禁止它读 Claude、CodeBuddy、Git 的登录信息；
- AI 能运行的命令只有「搜索」和「出文件」两个，出文件只能读项目文件夹里的文件；
- AI 不能改代码、不能改自己的规则；
- 对外发邮件，照样要你在飞书卡片上点「发送」。

---

## 第零步：准备

**电脑**
- Windows 10（较新的版本）或 Windows 11；
- 电脑要**一直开着、插着电**，安装脚本会帮你把「插电时睡眠」关掉。

**账号和钥匙**（提前准备好，**不要发在任何聊天里**）
- **底座**（三选一）：
  - A：Claude Pro 或 Max 订阅，加上代理软件；
  - B：比如 DeepSeek 开放平台的 API 密钥（充一点钱），再看一下它文档里「Anthropic API」那一页写的**接口地址**和**模型名**；
  - C：腾讯 CodeBuddy 的 API 密钥，在 https://copilot.tencent.com/profile/ 生成。
- **飞书**：能进飞书开放平台（open.feishu.cn），建「企业自建应用」。
- **邮箱**：QQ 邮箱或 163 邮箱的 **IMAP 授权码**（在邮箱网页版「设置」里开启 IMAP 后生成，不是登录密码）。
- **GitHub**：能看到模板仓库 `19507429700/agentos-windows`；再在**自己的** GitHub 上建一个空的私有仓库 `my-agentos-windows`（建的时候什么都不勾），用来备份知识库。

> **怎么打开 PowerShell**：开始菜单搜「PowerShell」，点开就行（不用管理员）。下面所有命令都在这里运行。
> **怎么粘贴**：在 PowerShell 窗口里点右键。

---

## 第一步：装软件

**已经装过的可以跳过。** 先查一下：

```powershell
py --version
```

```powershell
git --version
```

```powershell
Test-Path "C:\Program Files\LibreOffice\program\soffice.exe"
```

- Python 显示 3.10 到 3.14 都行（最低 3.9），Git 能显示版本号就行，LibreOffice 显示 `True` 就是装过了；
- **`py --version` 报错**（比如 `Unable to create process using ...`）：说明默认的那个 Python 坏了。运行 `py -0p` 看看电脑上还有哪些 Python，挑一个能用的设成默认（下面以 3.12 为例），关掉 PowerShell 重新打开再查：

```powershell
[Environment]::SetEnvironmentVariable("PY_PYTHON", "3.12", "User")
```

缺哪个装哪个。一条一条运行，每条装完再下一条（可能弹窗问「是否允许更改」，点「是」；LibreOffice 比较大，要下载几分钟）：

```powershell
winget install -e --id Python.Python.3.12
```

```powershell
winget install -e --id Git.Git
```

```powershell
winget install -e --id TheDocumentFoundation.LibreOffice
```

装完**关掉 PowerShell，重新打开一个**（新装的软件要重新打开才找得到）。检查一下：

```powershell
py --version
```

```powershell
git --version
```

都能显示版本号就对了。

> 提示 `winget` 找不到：在微软商店里搜「应用安装程序」装上；或者去官网手动下载安装 Python（安装时勾上「Add python.exe to PATH」）、Git、LibreOffice。
> LibreOffice 用 winget 下载太慢：去清华镜像 `https://mirrors.tuna.tsinghua.edu.cn/libreoffice/libreoffice/stable/` → 最新版本号 → `win` → `x86_64`，下载 `Win_x86-64.msi` 结尾的那个，双击安装，不要改安装位置。

---

## 第二步：拿代码，设好两个仓库

这一步做三件事：
1. **把代码下载到这台电脑**；
2. **记住以后从哪里拉更新**：模板仓库 `19507429700/agentos-windows`，在这台电脑上叫 `upstream`（更新来源）；
3. **设好知识库备份到哪里**：你自己的私有仓库 `my-agentos-windows`，在这台电脑上叫 `origin`（备份去向）。每天 03:00 把画像、人物页、复盘推上去，电脑坏了也不丢。

**第 3 件是可选的。** 只是试用、不需要云端备份的话，做完「改名为 upstream」那一条就可以直接去第三步，**不用建空仓库**；安装脚本发现没有备份仓库，就不装每天的备份任务。
（就算忘了改名，备份脚本发现备份地址还是模板仓库，也会拒绝备份，不会把你的知识库推进模板。）

拿代码（第一次会弹出 GitHub 登录窗口，用浏览器登录就行）：

```powershell
git clone https://github.com/19507429700/agentos-windows.git $HOME\AgentOS
```

进入项目文件夹（**后面所有命令都在这个文件夹里运行**；重新打开 PowerShell 后要再进一次）：

```powershell
cd $HOME\AgentOS
```

把模板改名为 `upstream`：

```powershell
git remote rename origin upstream
```

**要备份的话**，把你自己的仓库设成 `origin`（先在 GitHub 上建好空的私有仓库 `my-agentos-windows`，建的时候什么都不勾）。**把下面网址里的「你的GitHub用户名」换成你的用户名**（只换这几个字，别的不动）：

```powershell
git remote add origin https://github.com/你的GitHub用户名/my-agentos-windows.git
```

> 比如用户名是 19507429700，就是 `https://github.com/19507429700/my-agentos-windows.git`。

推上去，再检查一下：

```powershell
git push -u origin main
```

```powershell
git remote -v
```

应该看到 `origin` 指向你自己的仓库，`upstream` 指向模板。

设一下提交时用的名字（随便填）：

```powershell
git config --global user.name "AgentOS"; git config --global user.email "agentos@localhost"
```

---

## 第三步：第一次运行安装脚本

```powershell
powershell -ExecutionPolicy Bypass -File ops\windows\install.ps1
```

第一次运行会：
- 建好 `C:\Users\你的用户名\.agentos`，里面有三个空白配置：`bridge.env`（飞书）、`mail.env`（邮箱）、`agent.env`（底座）；
- 建一个专用的 Python 环境，装好飞书 SDK 和读 PDF 的工具；
- 设置 Python 默认用 UTF-8、插电不睡眠；
- 装定时任务。

**第一次跑完，看到这些都是正常的：**
- `用 py -3.12 建专用环境`（或别的版本）：它会自动挑一个能用的 Python，坏掉的会跳过；
- 好几行「**跳过……**」：钥匙都还没填，后面每填好一种就再运行一次本脚本，会自动补装；
- `✅ AgentOS-extract-text`：提取文字的任务，不用钥匙，第一次就装上了；
- `✅ AgentOS-push-kb`：第二步设了自己的备份仓库才会有；没设就是「跳过知识库备份」；
- `⚠️ 底座 claude 还没装`：第四步才装，正常；
- `Git 的 bash：……\bin\bash.exe`：找到了 Git，Claude Code 要用它。如果是「⚠️ 没找到 Git 自带的 bash.exe」，按它的提示处理，或者把这一行发给我。

只要**没有红色的报错**，就可以往下走。

装完**关掉 PowerShell 再重新打开**，然后 `cd $HOME\AgentOS`。

> 装 Python 包很慢或失败：先运行下面这条换成国内镜像，再重跑安装脚本。
> `& $HOME\.agentos\venv\Scripts\python.exe -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`

---

## 第四步：装底座、填钥匙（三选一）

填钥匙都用 `py ops\set_key.py`，**不用记事本**：粘贴、回车，最后显示「✅ 已填好」就行。令牌、密钥粘贴时**屏幕上不显示，是正常的**。

### A. Claude 订阅

先让 PowerShell 走代理（端口换成你代理软件的，Clash 常见 7890）：

```powershell
$env:HTTPS_PROXY="http://127.0.0.1:7890"; $env:HTTP_PROXY=$env:HTTPS_PROXY
```

装 Claude Code：

```powershell
irm https://claude.ai/install.ps1 | iex
```

装完关掉 PowerShell 重新打开，`cd $HOME\AgentOS`，再设一次上面的代理，然后生成令牌：

```powershell
claude setup-token
```

浏览器登录 Claude、同意，PowerShell 里会打印 `sk-ant-oat01-` 开头的令牌，复制下来：

```powershell
py ops\set_key.py agent CLAUDE_CODE_OAUTH_TOKEN
```

让机器人和定时任务也走代理：

```powershell
Set-Content $HOME\.agentos\proxy "http://127.0.0.1:7890"
```

### B. Claude Code + 其他公司的接口（以 DeepSeek 为例）

装 Claude Code。国内直连官方脚本可能装不上，就用国内镜像经 npm 装：

```powershell
winget install -e --id OpenJS.NodeJS.LTS
```

关掉 PowerShell 重新打开，`cd $HOME\AgentOS`。

**先允许运行脚本（只需要做一次）。** Windows 默认禁止运行脚本，不改的话，下面的 `npm` 会报「**无法加载文件 ……npm.ps1，因为在此系统上禁止运行脚本**」：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

（问你是否更改时输入 `Y` 回车；没问就是已经改好了。）然后设国内镜像、装 Claude Code：

```powershell
npm config set registry https://registry.npmmirror.com
```

```powershell
npm install -g @anthropic-ai/claude-code
```

装完检查（能显示版本号就装好了）：

```powershell
claude --version
```

> 中间出现几行黄色的 `npm warn install-scripts`，是警告不是错误，`claude --version` 能显示版本号就不用管。

填接口地址、模型名、密钥（地址和模型名**以厂商文档为准**，DeepSeek 看文档里「Anthropic API」那一页）：

```powershell
py ops\set_key.py agent ANTHROPIC_BASE_URL https://api.deepseek.com/anthropic
```

```powershell
py ops\set_key.py agent ANTHROPIC_MODEL 'deepseek-flash[1m]'
```

（DeepSeek 推荐用 `deepseek-flash`：便宜，而且能看图片。`[1m]` 是告诉 Claude Code「上下文有 100 万字」，照 DeepSeek 文档写；**两边的单引号要带上**，不然 PowerShell 会把方括号当成别的意思。用别家的接口，就换成那家文档写的模型名。）

```powershell
py ops\set_key.py agent ANTHROPIC_AUTH_TOKEN
```

（最后这条会让你粘贴 API 密钥。）`CLAUDE_CODE_OAUTH_TOKEN` 那一项留空。

### C. WorkBuddy（CodeBuddy Code）

装 CodeBuddy Code：

```powershell
irm https://www.codebuddy.cn/cli/install.ps1 | iex
```

（装不上的话，按 B 的办法装好 Node.js、允许运行脚本、设好国内镜像，然后 `npm install -g @tencent-ai/codebuddy-code`。）
（报「禁止运行脚本」：先运行 B 里的 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。）

关掉 PowerShell 重新打开，`cd $HOME\AgentOS`，然后：

```powershell
py ops\set_key.py agent AGENT_ENGINE codebuddy
```

```powershell
py ops\set_key.py agent CODEBUDDY_API_KEY
```

CodeBuddy 官方文档说「不开界面运行时要加 `-y`（跳过所有权限检查）」。我们**故意没加**，加了就等于拆掉上面说的那几道限制。2026-11-09 在 Windows 上实测：不加 `-y` 也能读文件、运行搜索，钥匙文件读不到（`py ops\check_agent.py --deep` 三项全过）。如果以后报权限相关的错，把报错记下来，**不要自己加 `-y`**。

### 检查底座通了没有

```powershell
py ops\check_agent.py
```

看到 `Agent 回复：OK` 就通了。报错的话看最后几行：
- `OAuth token revoked`、`401`、`authenticate`：钥匙不对，重新填；
- `连不上`、`timeout`、`ECONNRESET`：网络或代理的问题；
- `找不到 claude 命令` / `找不到 codebuddy 命令`：底座没装好，重新打开 PowerShell 再试，还不行就重装；
- `Git`、`bash`：Claude Code 在 Windows 上要用 Git，确认第一步装了 Git。

---

## 第五步：飞书应用（第一部分）

飞书开放平台 → 创建「企业自建应用」：
- 添加「**机器人**」能力；
- 权限管理里开（**应用身份**）：`im:message.p2p_msg:readonly`、`im:message:send_as_bot`、`im:resource`；
- 记下「凭证与基础信息」里的 **App ID** 和 **App Secret**。

> **如果你的 Mac 上也在跑 AgentOS**：这里要**另建一个新应用**（比如叫 agentos-win），不要和 Mac 共用一个机器人。两台电脑连着同一个机器人，飞书会把消息随机交给其中一台。

---

## 第六步：填飞书钥匙，启动机器人

```powershell
py ops\set_key.py bridge FEISHU_APP_ID
```

```powershell
py ops\set_key.py bridge FEISHU_APP_SECRET
```

再运行一次安装脚本，这次会启动飞书机器人：

```powershell
powershell -ExecutionPolicy Bypass -File ops\windows\install.ps1
```

看日志，出现 `connected to wss://` 就是连上飞书了（按 `Ctrl + C` 退出查看）：

```powershell
Get-Content $HOME\.agentos\logs\bridge.log -Tail 20 -Wait -Encoding UTF8
```

---

## 第七步：飞书应用（第二部分）——必须在第六步连上之后

- 事件与回调 → 订阅方式选「**使用长连接接收事件**」→ 添加事件 `im.message.receive_v1`；
- 回调配置 → 同样选长连接 → 添加「**卡片回传交互**」（发邮件前的确认卡片要用）；
- 创建版本并发布。

---

## 第八步：绑定你自己

在手机飞书里搜到这个机器人，随便发一句，它会回一个 `open_id`（`ou_` 开头）。填进去，再运行一次安装脚本：

```powershell
py ops\set_key.py bridge OWNER_OPEN_ID
```

```powershell
powershell -ExecutionPolicy Bypass -File ops\windows\install.ps1
```

之后机器人只响应你本人。**发一句「介绍一下你能做什么」，一两分钟内有回复，最核心的部分就通了。**

---

## 第九步：飞书采集（读你的聊天、文档、日程）

开放平台 → 权限管理，开下面这些（**用户身份**）：
`offline_access`、`im:message:readonly`、`im:message.p2p_msg:get_as_user`、`im:message.group_msg:get_as_user`、`im:chat:readonly`、`search:message`、`contact:user.base:readonly`、`contact:contact.base:readonly`、`docx:document:readonly`、`wiki:wiki:readonly`、`calendar:calendar:readonly`、`minutes:minutes.transcript:export`、`docs:document.media:download`、`sheets:spreadsheet:readonly`、`bitable:app:readonly`、`drive:drive:readonly`

安全设置 → 重定向 URL 加：`http://localhost:18080/feishu/callback`。发布新版本，审批通过后授权（会自动打开浏览器，登录、点「授权」）：

```powershell
py ops\feishu_auth.py
```

再运行一次安装脚本，装上飞书采集：

```powershell
powershell -ExecutionPolicy Bypass -File ops\windows\install.ps1
```

> 以后每次新开权限，审批通过后都要**重新运行 `feishu_auth.py`**，新权限才生效。

---

## 第十步：邮箱（可以换邮箱）

**用哪个邮箱都行**，QQ 邮箱、网易 163 邮箱都支持。和 Mac 上用的不是同一个也没关系，只是各自的复盘里只有各自邮箱的邮件。下面按你用的邮箱，**选一组**照着做。

### 先拿授权码（不是登录密码）

- **QQ 邮箱**：网页登录 QQ 邮箱 →「设置」→「账号」（新版界面在「账号与安全」）→ 找到「POP3/IMAP/SMTP 服务」→ 开启「IMAP/SMTP 服务」→ 按提示发短信验证 → 页面显示一串授权码，复制下来。
- **网易 163 邮箱**：网页登录 163 邮箱 → 上方「设置」→「POP3/SMTP/IMAP」→ 开启「IMAP/SMTP 服务」→ 按提示发短信验证 → 页面显示一串授权码（**只显示一次**），复制下来。

### 用 QQ 邮箱，运行这一组

```powershell
py ops\set_key.py mail MAIL_IMAP_HOST imap.qq.com
```

```powershell
py ops\set_key.py mail MAIL_USER 这里换成你的QQ邮箱地址
```

```powershell
py ops\set_key.py mail MAIL_PASSWORD
```

### 用网易 163 邮箱，运行这一组

```powershell
py ops\set_key.py mail MAIL_IMAP_HOST imap.163.com
```

```powershell
py ops\set_key.py mail MAIL_USER 这里换成你的163邮箱地址
```

```powershell
py ops\set_key.py mail MAIL_PASSWORD
```

（每组最后一条会让你粘贴授权码，屏幕上不显示，是正常的。邮箱地址只换中文那几个字，比如 `py ops\set_key.py mail MAIL_USER abc@163.com`。）

> 想换邮箱：按另一组再运行一遍，新的会覆盖旧的；然后重跑下面的安装脚本。
> 网易邮箱登录时要先报上客户端的名字，不然会报「Unsafe Login」，采集脚本已经处理好了。

再运行一次安装脚本，它会先测试能不能登录邮箱，再装上邮件采集：

```powershell
powershell -ExecutionPolicy Bypass -File ops\windows\install.ps1
```

**检查定时任务**：开始菜单搜「任务计划程序」→ 左边点「任务计划程序库」，应该能看到这几个：
- `AgentOS-Bridge`（飞书机器人，状态「正在运行」）；
- `AgentOS-collect-mail`、`AgentOS-collect-feishu`、`AgentOS-extract-text`（每 10 分钟）；
- `AgentOS-daily-review`（每天 23:00）、`AgentOS-push-kb`（每天 03:00）。

---

## 第十一步（可选）：告警

- **告警和复盘也推到微信**（PushPlus）：

```powershell
py ops\set_key.py bridge PUSHPLUS_TOKEN
```

- **电脑关机、断网时发邮件提醒**（healthchecks.io）：在网站上给每个任务建一个检查，把地址填进来（每个任务一条，名字是 `HC_` 加任务名，大写、横线换成下划线）：

```powershell
py ops\set_key.py healthchecks HC_COLLECT_MAIL https://hc-ping.com/这里换成你的地址
```

（同样填 `HC_COLLECT_FEISHU`、`HC_DAILY_REVIEW`、`HC_PUSH_KB`。）

---

## 第十二步：验收

1. 手机飞书发「介绍一下你能做什么」，有回复；
2. 发自我介绍、常打交道的人、正在跟的项目，说「整理进我的画像、人物页和项目页」；
3. 问「这周我和某某聊了什么」，每条结论后面有出处；
4. 让它「出一份 Word」，飞书里收到文件，中文不是方框；
5. 发一张带字的截图给机器人，十几分钟后问它截图里的字，能搜到；
6. 当晚 23:00 收到复盘。等不及的话手动跑一次：

```powershell
py ops\daily_review.py
```

7. 手动备份一次，再去 GitHub 上你自己的仓库里看看有没有「知识库备份」：

```powershell
py ops\push_kb.py
```

---

## 日常和排错

- **日志**都在 `C:\Users\你的用户名\.agentos\logs\`：`bridge.log`（机器人）、`collect-mail.log`、`daily-review.log` 等。
  用 PowerShell 看日志要加 `-Encoding UTF8`，不然中文是乱码，比如 `Get-Content $HOME\.agentos\logs\bridge.log -Tail 50 -Encoding UTF8`；用记事本打开也可以。
- **机器人没反应**：先看 `bridge.log` 最后几行；再跑 `py ops\check_agent.py` 看底座通不通。
- **重启机器人**（改了代码、拉了更新之后）：

```powershell
py ops\deploy.py
```

- **拉更新**（模板有新版本时）：

```powershell
git pull upstream main
```

```powershell
py ops\deploy.py
```

  如果更新里加了新的定时任务，再运行一次安装脚本。
- **电脑重启后**：要**登录一次 Windows**，机器人和定时任务才会开始跑。
- **钥匙失效**（机器人突然不回、复盘写不出来）：回第四步重新填，再跑 `check_agent.py`。
- **知识库**（`spaces\me\kb\`）是 AI 写的，要改就在飞书里让它改，自己改容易和备份冲突。

---

## 测试记录（请照着填，发给我）

每一步做完记一下：✅ 通过；❌ 失败 + 报错原文（截图或复制最后几行）。

- 第一步 装软件：
- 第二步 拿代码、两个仓库：
- 第三步 安装脚本第一次运行（有没有红字报错）：
- 第四步 选的哪种底座、`check_agent.py` 的结果：
- 第六步 机器人连上飞书（日志里有没有 `connected`）：
- 第八步 手机发消息有回复：
- 第九步 飞书授权：
- 第十步 用的哪个邮箱、任务计划程序里的 AgentOS- 任务：
- 第十二步 出 Word 中文正常、截图里的字能搜到、复盘、备份：
