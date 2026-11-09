# AgentOS 部署指南：Windows

写于 2026-11-09。这是 Windows 版的第一份安装说明，**还没有在真的 Windows 电脑上完整跑过**。
标「⚠️ 要实测」的步骤，是我在 Mac 上没法验证的。哪一步卡住了，把屏幕上的报错原样记下来（见最后的「测试记录」）。

---

## 先看懂：它在 Windows 上怎么跑

- Windows 自带一个「Linux 子系统」，叫 **WSL2**。我们在里面装一个 **Ubuntu**（一种 Linux），整套 AgentOS 跑在 Ubuntu 里。
- 这样做的原因：AgentOS 原来是在 Mac 上写的，Mac 和 Linux 很像，大部分代码能直接用；直接改成 Windows 原生的写法，要重写一大半。
- **手机这一头不用装任何东西**，装好飞书就行。
- Ubuntu 里有两个账号：
  - **你的账号**：装 Ubuntu 时自己起的名字。飞书机器人、采集、复盘、备份都以它运行，钥匙放在 `~/.agentos/`。
  - **agentos**：专门跑 AI 的隔离账号，AI 读不到你的钥匙。
- **底座有三种可选**（第五步选）：
  - **A. Claude 订阅**：要代理；
  - **B. Claude Code + 其他公司的接口**（DeepSeek 等）：国内直连，**最省事，推荐 Windows 先用这个测**；
  - **C. WorkBuddy（腾讯的 CodeBuddy Code）**：国内直连，但权限限制要实测。
- **这一版没有的**：微信聊天采集、公众号文章抓取（Windows 版微信的解密方法和 Mac 不同，还没做）。

**大概要多久**：第一次装 2～3 小时。其中：
- 装 WSL 和 Ubuntu：20～30 分钟（要重启一次）；
- 跑安装脚本：20～30 分钟（大部分时间在下载软件）；
- 飞书后台点权限：30～60 分钟。

---

## 第零步：准备

**电脑**
- Windows 11，或者 Windows 10 的 2004 及以上版本；
- 有**管理员**权限；
- 电脑要**一直开着、插着电**。笔记本合盖会睡眠，第三步会教你改。

**账号和钥匙**（提前准备好，**不要发在任何聊天里**）
- **底座**（三选一）：
  - A：Claude Pro 或 Max 订阅，加上一个代理软件；
  - B：比如 DeepSeek 开放平台的 API 密钥（充一点钱），再看一下它文档里「Anthropic API」那一页写的**接口地址**和**模型名**；
  - C：腾讯 CodeBuddy 的 API 密钥，在 https://copilot.tencent.com/profile/ 生成。
- **飞书**：能进飞书开放平台（open.feishu.cn），建「企业自建应用」。
- **邮箱**：QQ 邮箱或 163 邮箱的 **IMAP 授权码**（在邮箱网页版「设置」里开启 IMAP 后生成，不是登录密码）。
- **GitHub**：
  - 能读到 AgentOS **Windows 版代码**的仓库地址；
  - 一个**你自己的私有仓库**，用来备份知识库（比如叫 `my-agentos`，建的时候什么都不勾）；
  - 一个 GitHub 的**个人访问令牌**（Personal access token，勾 `repo` 权限），推送时代替密码。

---

## 第一步：装 WSL 和 Ubuntu

1. 在开始菜单搜「PowerShell」，**右键 →「以管理员身份运行」**，然后运行：

```powershell
wsl --install -d Ubuntu
```

2. 装完**重启电脑**。
3. 重启后会自动弹出 Ubuntu 的窗口（没弹出就在开始菜单里打开「Ubuntu」），让你设一个**用户名**和**密码**。用户名用小写英文；密码输入时屏幕上不显示，是正常的。**这个密码要记住**，后面 `sudo` 都要用。
4. 在 Ubuntu 窗口里检查两件事：

```bash
ps -p 1 -o comm=
```

显示 `systemd` 就对了。如果显示的是别的（比如 `init`），运行下面这条，然后在 Windows 的 PowerShell 里运行 `wsl --shutdown`，再重新打开 Ubuntu：

```bash
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
```

```bash
date
```

时间应该是**北京时间**。不对的话（每晚 23:00 的复盘时间会错）：

```bash
sudo timedatectl set-timezone Asia/Shanghai
```

> **以后说的「Ubuntu 终端」**，就是这个 Ubuntu 窗口。下面所有 `bash` 命令都在这里运行；`powershell` 命令在 Windows 的 PowerShell 里运行。
> 在 Ubuntu 终端里粘贴：右键，或者按 `Ctrl + Shift + V`。

---

## 第二步：拿代码，换成你自己的备份仓库

代码**必须放在 `~/AgentOS`**，安装脚本按这个位置找。

```bash
git clone https://github.com/<放 Windows 版代码的账号>/agentos-windows.git ~/AgentOS
```

（会问用户名和密码时：用户名填 GitHub 用户名，密码填**个人访问令牌**。）

让 Git 记住令牌，以后每天备份推送时不用再输：

```bash
git config --global credential.helper store
```

把备份地址换成**你自己的私有仓库**。这一步一定要做，不然每天 03:00 的知识库备份会推到放代码的那个仓库：

```bash
git -C ~/AgentOS remote set-url origin https://github.com/<你的账号>/my-agentos.git
```

```bash
git -C ~/AgentOS push -u origin main
```

再设一下提交时用的名字（随便填）：

```bash
git config --global user.name "你的名字" && git config --global user.email "you@example.com"
```

---

## 第三步：Windows 这一头的设置（防睡眠、开机自动运行）

把设置脚本复制到 Windows 能看到的地方：

```bash
cp ~/AgentOS/ops/windows/setup-windows.ps1 /mnt/c/Users/Public/
```

然后在**管理员 PowerShell** 里运行：

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\Public\setup-windows.ps1
```

它会做三件事：
1. 插电时不睡眠、不休眠；
2. 建一个计划任务「AgentOS-WSL」：每次登录 Windows 后，在后台启动 Ubuntu，并且一直开着；
3. 马上启动一次。

最后显示「计划任务状态：Running」就对了。⚠️ 要实测：关掉所有 Ubuntu 窗口，过两分钟再打开，运行 `uptime`，看 up 后面的时间是不是一直在累加（如果是刚开机的几秒钟，说明 Ubuntu 被关过）。

**笔记本**还要改一处：控制面板 → 电源选项 →「选择关闭笔记本计算机盖的功能」→「接通电源」那一列改成「**不采取任何操作**」。

---

## 第四步：初始化（建隔离账号、装软件、装 Agent）

**如果底座选 A（Claude 订阅）**，要走代理。先确认代理软件开着，并且：
- 在代理软件里打开「**允许局域网连接**」；
- 查一下它的端口，比如 Clash 常见 7890。

然后设置 WSL 用和 Windows 一样的网络（这样 Ubuntu 里的 `127.0.0.1` 就能连到 Windows 上的代理）：用记事本新建 `C:\Users\<你的Windows用户名>\.wslconfig`，内容：

```
[wsl2]
networkingMode=mirrored
```

保存后在 PowerShell 里运行 `wsl --shutdown`，再打开 Ubuntu。⚠️ 要实测：mirrored 需要 Windows 11 22H2 以上；Windows 10 不支持，要把下面的 `127.0.0.1` 换成 Windows 的局域网 IP。

**运行初始化**（会问你的 Ubuntu 密码；要下载 LibreOffice 等软件，大约 10～20 分钟）：

- 底座选 **B（其他接口）**：

```bash
sudo ~/AgentOS/ops/bootstrap-linux.sh
```

- 底座选 **A（Claude 订阅，要代理）**，把端口换成你的：

```bash
sudo AGENTOS_PROXY=http://127.0.0.1:7890 ~/AgentOS/ops/bootstrap-linux.sh
```

- 底座选 **C（WorkBuddy）**：

```bash
sudo AGENT_ENGINE=codebuddy ~/AgentOS/ops/bootstrap-linux.sh
```

它会：
- 装系统软件：git、Python、LibreOffice、中文字体、识字工具；
- 建 agentos 账号，把代码复制一份到 `/home/agentos/agentos`（运行副本）；
- 建令牌文件 `/home/agentos/.env`，配置免密规则，装 Agent。

最后显示「完成」就对了。

**如果提示「没装上（多半是连不上外网）」**：按它给的两条命令，用国内镜像装，然后**再运行一次**同样的初始化命令。

**如果 apt 下载特别慢**：可以把软件源换成国内镜像（比如清华、阿里云）再重跑。这一步各种教程很多，这里不展开。

---

## 第五步：选底座，填钥匙

下面用 `set-key.sh` 填，不用编辑器：粘贴、回车，最后显示「✅ 已填好」就行。令牌、密钥粘贴时屏幕上不显示，是正常的。会先问一次你的 Ubuntu 密码。

先设一个简写，后面命令短一点（只对当前这个终端有效）：

```bash
K="sudo -u agentos /home/agentos/agentos/ops/set-key.sh /home/agentos/.env"
```

### A. Claude 订阅

```bash
sudo -u agentos -H bash -c 'cd ~ && set -a && . ~/.env && set +a && PATH=~/.local/bin:~/.npm-global/bin:$PATH claude setup-token'
```

浏览器打开授权页（没自动打开，就复制终端里的网址到 Windows 浏览器），登录 Claude、同意。终端打印出 `sk-ant-oat01-` 开头的令牌，复制下来，然后：

```bash
$K CLAUDE_CODE_OAUTH_TOKEN
```

### B. Claude Code + 其他公司的接口（以 DeepSeek 为例）

接口地址和模型名**以厂商文档为准**（DeepSeek 文档里「Anthropic API」那一页）。

```bash
$K ANTHROPIC_BASE_URL https://api.deepseek.com/anthropic
```

```bash
$K ANTHROPIC_MODEL <文档里写的模型名>
```

```bash
$K ANTHROPIC_AUTH_TOKEN
```

（最后这条会让你粘贴 DeepSeek 的 API 密钥。）**`CLAUDE_CODE_OAUTH_TOKEN` 那一行留空。**

### C. WorkBuddy（CodeBuddy Code）

```bash
$K AGENT_ENGINE codebuddy
```

```bash
$K CODEBUDDY_INTERNET_ENVIRONMENT internal
```

```bash
$K CODEBUDDY_API_KEY
```

⚠️ 要实测：CodeBuddy 官方文档说「不开界面运行时要加 `-y`（跳过所有权限检查）」。我们**故意没加**：加了就等于拆掉「AI 只能跑那几条命令、读不到令牌」的防线。如果第六步报权限相关的错，把报错记下来，不要自己加 `-y`。

### 检查

```bash
~/AgentOS/ops/check-agent.sh
```

看到 `Agent 回复：OK` 和后面的 `OK`，底座就通了。报错的话，看最后一行：
- `OAuth token revoked` 或 `401`：钥匙不对，重新填；
- `连不上`、`timeout`：网络或代理问题；
- `command not found`：Agent 没装上，回第四步看「没装上」那段。

---

## 第六步：飞书应用（第一部分）

飞书开放平台 → 创建「企业自建应用」：
- 添加「**机器人**」能力；
- 权限管理里开（**应用身份**）：`im:message.p2p_msg:readonly`、`im:message:send_as_bot`、`im:resource`；
- 记下「凭证与基础信息」里的 **App ID** 和 **App Secret**。

---

## 第七步：装飞书机器人

```bash
~/AgentOS/ops/install-bridge.sh
```

第一次会生成配置文件，提示「先填好 FEISHU_APP_ID / FEISHU_APP_SECRET」。填：

```bash
~/AgentOS/ops/set-key.sh ~/.agentos/bridge.env FEISHU_APP_ID
```

```bash
~/AgentOS/ops/set-key.sh ~/.agentos/bridge.env FEISHU_APP_SECRET
```

再运行一次，这次会启动机器人：

```bash
~/AgentOS/ops/install-bridge.sh
```

看日志，出现 `connected to wss://` 就是连上飞书了（按 `Ctrl + C` 退出查看）：

```bash
tail -f ~/.agentos/logs/bridge.log
```

---

## 第八步：飞书应用（第二部分）——必须在第七步连上之后

- 事件与回调 → 订阅方式选「**使用长连接接收事件**」→ 添加事件 `im.message.receive_v1`；
- 回调配置 → 同样选长连接 → 添加「**卡片回传交互**」（发邮件前的确认卡片要用）；
- 创建版本并发布。

---

## 第九步：绑定你自己

在手机飞书里搜到这个机器人，随便发一句，它会回一个 `open_id`（`ou_` 开头）。填进去：

```bash
~/AgentOS/ops/set-key.sh ~/.agentos/bridge.env OWNER_OPEN_ID
```

```bash
~/AgentOS/ops/install-bridge.sh
```

之后机器人只响应你本人。**发一句「介绍一下你能做什么」，一两分钟内有回复，最核心的部分就通了。**

---

## 第十步：飞书采集（读你的聊天、文档、日程）

开放平台 → 权限管理，开下面这些（**用户身份**）：
`offline_access`、`im:message:readonly`、`im:message.p2p_msg:get_as_user`、`im:message.group_msg:get_as_user`、`im:chat:readonly`、`search:message`、`contact:user.base:readonly`、`contact:contact.base:readonly`、`docx:document:readonly`、`wiki:wiki:readonly`、`calendar:calendar:readonly`、`minutes:minutes.transcript:export`、`docs:document.media:download`、`sheets:spreadsheet:readonly`、`bitable:app:readonly`、`drive:drive:readonly`

安全设置 → 重定向 URL 加：`http://localhost:18080/feishu/callback`。发布新版本，审批通过后授权：

```bash
python3 ~/AgentOS/ops/feishu_auth.py
```

它会打印一个网址：**复制到 Windows 的浏览器里打开**，登录、点「授权」。浏览器跳回 `localhost:18080` 显示成功就好了。⚠️ 要实测：Windows 浏览器访问 `localhost` 能不能连到 Ubuntu 里（WSL 一般会自动转发）。

> 以后每次新开权限，审批通过后都要**重新运行这一条**，新权限才生效。

---

## 第十一步：邮箱，以及装全部定时任务

```bash
~/AgentOS/ops/install-collectors.sh
```

第一次会生成 `~/.agentos/mail.env`。填（QQ 邮箱的地址是 `imap.qq.com`，163 邮箱是 `imap.163.com`）：

```bash
~/AgentOS/ops/set-key.sh ~/.agentos/mail.env MAIL_IMAP_HOST imap.qq.com
```

```bash
~/AgentOS/ops/set-key.sh ~/.agentos/mail.env MAIL_USER 你的邮箱@qq.com
```

```bash
~/AgentOS/ops/set-key.sh ~/.agentos/mail.env MAIL_PASSWORD
```

再运行一次，这次会装好全部定时任务：

```bash
~/AgentOS/ops/install-collectors.sh
```

检查定时任务都在：

```bash
systemctl --user list-timers 'agentos-*'
```

应该能看到 5 个：
- `agentos-collect-mail`、`agentos-collect-feishu`、`agentos-extract-text`：每 10 分钟；
- `agentos-daily-review`：每天 23:00；
- `agentos-push-kb`：每天 03:00。

---

## 第十二步（可选）：告警

- **推到微信**（PushPlus）：

```bash
~/AgentOS/ops/set-key.sh ~/.agentos/bridge.env PUSHPLUS_TOKEN
```

- **电脑关机、断网时发邮件**（healthchecks.io）：在网站上给每个任务建一个检查，然后：

```bash
touch ~/.agentos/healthchecks.env && chmod 600 ~/.agentos/healthchecks.env
```

```bash
~/AgentOS/ops/set-key.sh ~/.agentos/healthchecks.env HC_COLLECT_MAIL https://hc-ping.com/<地址>
```

（同样再填 `HC_COLLECT_FEISHU`、`HC_DAILY_REVIEW`、`HC_PUSH_KB`。）

---

## 第十三步：验收

1. 手机飞书发「介绍一下你能做什么」，有回复；
2. 发自我介绍、常打交道的人、正在跟的项目，说「整理进我的画像、人物页和项目页」；
3. 问「这周我和某某聊了什么」，每条结论后面有出处；
4. 让它「出一份 Word」，飞书里收到文件，中文不是方框；
5. 当晚 23:00 收到复盘。等不及的话手动跑一次：

```bash
~/AgentOS/ops/daily-review.sh
```

6. 手动备份一次，再去 GitHub 上你自己的仓库里看看有没有「知识库备份」：

```bash
~/AgentOS/ops/push-kb.sh
```

---

## 日常和排错

- **改了代码**：提交后运行 `~/AgentOS/ops/deploy.sh`（同步到运行副本、重启机器人）。
- **机器人没反应**：

```bash
systemctl --user status agentos-bridge
```

```bash
tail -50 ~/.agentos/logs/bridge.log
```

- **某个定时任务报错**：看 `~/.agentos/logs/<任务名>.log`，比如 `collect-mail.log`。
- **电脑重启后**：要**登录一次 Windows**，计划任务才会把 Ubuntu 启动起来。
- **钥匙失效**（机器人突然不回、复盘写不出来）：回第五步重新填，再跑 `check-agent.sh`。
- **知识库不要在 `~/AgentOS` 里改**：它是 AI 在运行副本里写的；要改，在飞书里让机器人改。

---

## 测试记录（请照着填，发给我）

每一步做完，记一下结果：✅ 通过；❌ 失败 + 报错原文（截图或复制最后几行）。

- 第一步 装 WSL、systemd、时区：
- 第二步 拿代码、换备份仓库：
- 第三步 Windows 设置脚本、关掉窗口后 Ubuntu 还在不在：
- 第四步 初始化（选的哪种底座）：
- 第五步 check-agent：
- 第七步 机器人连上飞书：
- 第九步 手机发消息有回复：
- 第十步 飞书授权（浏览器能不能跳回 localhost）：
- 第十一步 定时任务 5 个都在：
- 第十三步 出 Word 中文正常、复盘、备份：
