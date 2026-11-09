# AgentOS 部署指南：搬到别人的电脑上

写于 2026-11-09。依据：「AgentOS 项目启动」和后续几次对话里实际做过、踩过的坑，以及仓库里现有的安装脚本。
标「要实测」的是还没在别的电脑上验证过的，标「推测」的是判断，不是事实。

---

## 先看结论

1. **电脑分 Mac、Windows、Linux 三种；安卓和苹果手机是手机系统，不是电脑。**
   AgentOS 要一台**一直开着的电脑**当「服务器」，手机只是遥控器：对方手机上装飞书就能用，**手机这一头不用部署**。
   安卓手机当服务器不现实，原因见第六节。
2. **现在只有 Mac 能一键装好全部功能。** Windows、Linux 要先改两块代码（定时任务、建隔离账号），微信采集要换工具；见第六节。
3. **你的 GitHub 仓库不能直接给别人。** 里面有你的画像、人物页、复盘，而且就算删掉文件，**Git 历史里也还在**。要先做一份「干净版」，见第一节。
4. **每个人一套，钥匙各用各的**：Claude（或别的底座）、飞书应用、邮箱授权码、微信密钥、GitHub 仓库，全部由对方自己申请。你的任何钥匙都不能给别人用。
5. **底座可以换。** WorkBuddy 背后的命令行工具 CodeBuddy Code 和 Claude Code 几乎一样，有 API 密钥（令牌），改动量不大，但要实测；见第七节。
6. **微信聊天记录**只能在对方**自己的电脑**上读对方**自己的微信**，属于个人自用，有封号风险，不能做成对外产品；**公众号正文抓取**靠伪装微信客户端绕过验证，由使用者自己决定要不要开；见第四、五节。

---

## 一、给别人之前：做一份「干净版」代码

### 为什么不能直接给

你现在的仓库（GitHub `19507429700/agentos`、Gitee `hjx19507429700/agentos`）里有两类东西：

- **代码和规则**：`ops/`、`.claude/`、`CLAUDE.md`、`README.md`、`docs/`。这些可以给。
- **你的知识库** `spaces/me/kb/`：画像 `profile.md`、人物页、项目页、每天的复盘、拍板记录。**这些不能给。**

只删文件不够：对方 `git clone` 时会拿到**全部提交历史**，用 `git log` 就能翻出以前每一版的复盘和人物页。所以要**新建一个没有历史的仓库**。

另外，测试文件里有几处你的名字和手机号邮箱（`ops/test_d2.py`、`ops/bridge/test_bridge.py`），介意的话可以一起换成假名字，不影响运行。

### 做法（在你的 Mac 上，大约 5 分钟）

**第 1 步：先把手上的改动都提交。** 下面的命令只打包「已提交」的内容。

**第 2 步：导出一份只有当前代码、没有历史的副本：**

```bash
mkdir ~/agentos-template
```

```bash
git -C ~/AgentOS archive HEAD | tar -x -C ~/agentos-template
```

**第 3 步：清空知识库，只留空文件夹：**

```bash
find ~/agentos-template/spaces/me/kb -type f -name '*.md' -delete
```

**第 4 步：放回空白的画像和拍板记录**（对方第一次用时，机器人会按画像的空栏「采访」他）：

```bash
printf '# 个人画像\n\n## 我是谁\n\n## 公司\n\n## 常往来的人\n见 people/。\n\n## 正在跟的项目\n见 projects/。\n\n## 对我的期望\n\n## 偏好\n' > ~/agentos-template/spaces/me/kb/profile.md
```

```bash
printf '# 拍板记录\n\n格式：`- YYYY-MM-DD 决定内容（原话）`\n' > ~/agentos-template/spaces/me/kb/decisions.md
```

**第 5 步：检查一遍，确认知识库里只剩空模板：**

```bash
find ~/agentos-template/spaces -type f
```

只应该看到几个 `.gitkeep`，加上刚放回去的 `profile.md`、`decisions.md`。多出别的文件，就是还有个人内容没删干净。

**第 6 步：建一个新的私有仓库，推上去**（比如 GitHub 上新建 `agentos-template`，选 Private，**不要勾**自动生成 README）：

```bash
cd ~/agentos-template && git init -b main && git add -A && git commit -m "AgentOS 模板（不含个人知识库）"
```

```bash
git -C ~/agentos-template remote add origin https://github.com/<你的账号>/agentos-template.git
```

```bash
git -C ~/agentos-template push -u origin main
```

以后要给谁装，就把这个模板仓库的读权限给他，或者打包成 zip 发给他。**你自己的 `agentos` 仓库继续只给你自己用。**

> 代码更新了，想同步到模板：重做第 2～6 步里的 2、3、4，然后在 `~/agentos-template` 里 `git add -A && git commit && git push`。（以后可以写成一个脚本，一条命令做完。）

---

## 二、对方要准备什么（清单）

**硬件和系统**
- 一台 **Mac**，最好是 Apple 芯片（M1～M4）。Intel 芯片的 Mac 也能用，但 Homebrew 装在 `/usr/local`，要改几处写死的 `/opt/homebrew` 路径（`ops/install-collectors.sh` 两处、`ops/launchd/` 里复盘和备份两个配置）。
- 管理员密码（装的时候要建一个隐藏账号 `agentos`）。
- **一直开着、插着电**：系统设置 → 电池 → 打开「插电时防止自动睡眠」；MacBook 合盖会睡，要开着盖。
- 在国内连 Claude，要有**代理软件**，并且在装之前就打开。

**账号和钥匙**（全部由对方自己办）
- **底座**：Claude 的 Pro 或 Max 订阅（用来生成一年期令牌）；或者换成 WorkBuddy 的 API 密钥，见第七节。
- **飞书**：在飞书开放平台建一个「企业自建应用」（个人也能建在自己的组织里）。
- **邮箱**：QQ 邮箱或 163 邮箱的 IMAP **授权码**（在邮箱网页版设置里生成，不是登录密码）。
- **GitHub 私有仓库**：放他自己的知识库备份。想同时备份到 Gitee，再建一个 Gitee 私有仓库。
- 可选：**PushPlus** 令牌（告警和复盘推到微信）、**healthchecks.io** 账号（电脑断网、关机时发邮件提醒）。
- 可选：**微信**（只能读他自己这台电脑上登录的微信），见第四节。

**软件**（第三节第 1 步统一装）
- Xcode 命令行工具（自带 git）、Homebrew、Python 3.14、LibreOffice（出 Word、PDF 用）。

---

## 三、在对方 Mac 上部署（按顺序做）

下面每一步都在**对方自己的账号**的终端里运行。和 `README.md` 里的 D1、D2 清单是同一套，这里按「一次装完」重新排了顺序，并补上了后来加的东西。

### 第 1 步：装基础软件

```bash
xcode-select --install
```

装 Homebrew（按官网 brew.sh 上的那一行命令），然后：

```bash
brew install python@3.14 sqlcipher
```

```bash
brew install --cask libreoffice
```

（`sqlcipher` 只有要接微信才需要。）

### 第 2 步：拿代码，换成他自己的备份仓库

代码**必须放在 `~/AgentOS`**，安装脚本是按这个位置找的。

```bash
git clone https://github.com/<你的账号>/agentos-template.git ~/AgentOS
```

把备份地址换成他自己的私有仓库，以后每天 03:00 的知识库备份推到他那里，不会推到你这里：

```bash
git -C ~/AgentOS remote set-url origin https://github.com/<他的账号>/agentos.git
```

```bash
git -C ~/AgentOS push -u origin main
```

### 第 3 步：初始化 Mac（建隐藏账号、装 Claude Code）

**先打开代理软件**，再运行（要输一次管理员密码）：

```bash
sudo ~/AgentOS/ops/bootstrap-mac.sh
```

它会：建隐藏账号 `agentos`；把代码克隆一份到 `/Users/agentos/agentos`（运行副本，Claude 在这里干活）；建令牌文件 `/Users/agentos/.env`；配置免密规则；以 agentos 身份装 Claude Code。

### 第 4 步：生成 Claude 令牌，写进去

```bash
sudo -u agentos -i
```

在 agentos 的终端里：

```bash
set -a; . ~/.env; set +a
```

```bash
~/.local/bin/claude setup-token
```

浏览器里登录 Claude 并同意，终端会打印一串 `sk-ant-oat01-` 开头的令牌，复制下来。然后运行下面这一整行，粘贴令牌、回车（屏幕上不显示，是正常的）：

```bash
read -s "T?粘贴新令牌后按回车："; echo; grep -q '^CLAUDE_CODE_OAUTH_TOKEN=' ~/.env && sed -i '' "s|^CLAUDE_CODE_OAUTH_TOKEN=.*|CLAUDE_CODE_OAUTH_TOKEN=$T|" ~/.env || echo "CLAUDE_CODE_OAUTH_TOKEN=$T" >> ~/.env; chmod 600 ~/.env; unset T; grep -c '^CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat' ~/.env
```

显示 `1` 就成功了。然后：

```bash
exit
```

> 这台 Mac 的终端是 zsh，不能用 `read -p`（我们踩过这个坑）；上面这行已经是 zsh 的写法。

### 第 5 步：飞书应用（第一部分）

飞书开放平台 → 创建「企业自建应用」：
- 添加「**机器人**」能力；
- 权限管理里开（**应用身份**）：`im:message.p2p_msg:readonly`、`im:message:send_as_bot`、`im:resource`；
- 记下「凭证与基础信息」里的 **App ID** 和 **App Secret**。

### 第 6 步：装飞书机器人（桥接服务）

```bash
~/AgentOS/ops/install-bridge.sh
```

第一次运行会生成 `~/.agentos/bridge.env`。把 `FEISHU_APP_ID=`、`FEISHU_APP_SECRET=` 填好（用文本编辑器打开：`open -e ~/.agentos/bridge.env`），然后再运行一次 `install-bridge.sh`。

验证 Claude 能叫醒：

```bash
~/AgentOS/ops/check-agent.sh
```

应该看到 `Claude 回复：OK`。

### 第 7 步：飞书应用（第二部分）——必须在第 6 步连上之后

- 事件与回调 → 订阅方式选「**使用长连接接收事件**」→ 添加事件 `im.message.receive_v1`；
- 回调配置 → 同样选长连接 → 添加「**卡片回传交互**」（发邮件前的确认卡片要用）；
- 创建版本并发布。

### 第 8 步：绑定本人

在飞书里搜到机器人，随便发一句，它会回一个 `open_id`。填进 `~/.agentos/bridge.env` 的 `OWNER_OPEN_ID=`，再运行一次：

```bash
~/AgentOS/ops/install-bridge.sh
```

之后机器人只响应他本人。

### 第 9 步：飞书采集（以他本人身份读聊天、文档）

开放平台 → 权限管理，开下面这些（**用户身份**）：
`offline_access`、`im:message:readonly`、`im:message.p2p_msg:get_as_user`、`im:message.group_msg:get_as_user`、`im:chat:readonly`、`search:message`、`contact:user.base:readonly`、`contact:contact.base:readonly`、`docx:document:readonly`、`wiki:wiki:readonly`、`calendar:calendar:readonly`、`minutes:minutes.transcript:export`、`docs:document.media:download`、`sheets:spreadsheet:readonly`、`bitable:app:readonly`、`drive:drive:readonly`

安全设置 → 重定向 URL 加：`http://localhost:18080/feishu/callback`。发布新版本，等管理员审批通过后授权：

```bash
python3 ~/AgentOS/ops/feishu_auth.py
```

> 以后每次新开权限，审批通过后都要**重新运行这一条**授权，新权限才生效（我们踩过）。

### 第 10 步：邮箱，以及装全部定时任务

```bash
~/AgentOS/ops/install-collectors.sh
```

第一次会生成 `~/.agentos/mail.env`，填好 `MAIL_IMAP_HOST`（QQ 邮箱 `imap.qq.com`、163 邮箱 `imap.163.com`）、`MAIL_USER`、`MAIL_PASSWORD`（授权码），再运行一次。它会装好：邮件和飞书采集（每 10 分钟）、提取文字（每 10 分钟）、每日复盘（23:00）、知识库备份（03:00）；配了微信的话，还有微信采集和公众号抓取。

### 第 11 步（可选）：告警

- `~/.agentos/bridge.env` 加一行 `PUSHPLUS_TOKEN=…`：告警和复盘同时推到微信；
- `~/.agentos/healthchecks.env`：每个任务一行打卡地址，`HC_COLLECT_MAIL=`、`HC_COLLECT_FEISHU=`、`HC_DAILY_REVIEW=`、`HC_PUSH_KB=`（任务名大写、横线换成下划线）。电脑关机、断网时由 healthchecks 发邮件。

### 第 12 步：验收

- 手机飞书发「介绍一下你能做什么」，一分钟内有回复；
- 发自我介绍、常打交道的人、正在跟的项目，说「整理进我的画像、人物页和项目页」；
- 当晚 23:00 收到第一份复盘。

---

## 四、微信聊天记录（重点）

### 先说清楚性质

- **只能在对方自己的电脑上，读他自己登录的微信。** 不能远程读别人的微信，也不能读手机上的微信。
- 做法是**解密微信存在电脑里的聊天数据库**，违反微信用户协议，有小概率封号；微信升级后可能失效。
- **只适合个人自用。** 要做成对外推广的产品，这条路走不通（未经授权读别人的数据违法）；产品版要走企业微信的「会话存档」（官方、合法，只能读企业工作聊天），或者让用户把要紧的聊天转发给机器人。

### 原理（三步）

1. 微信把聊天存在电脑上的数据库里，用 **SQLCipher 加密**，直接打开是乱码；
2. **取密钥**：密钥只在微信运行时的内存里。工具对微信的一个**副本**重新签名，再用调试器（LLDB）从副本内存里读出密钥，存到 `keys.json`。不动 `/应用程序` 里的微信本体，不用关系统保护；
3. **只读解密**：`ops/collect_wechat.py` 每 10 分钟用密钥打开数据库，把新消息按「几点、谁、说了什么」写进 `raw/wechat/<日期>/`，和飞书、邮件一样进搜索和复盘。聊天里别人发的文件（PDF、Word）也会复制一份，方便读正文。

### 在 Mac 上怎么配（对方本人操作，大约 20 分钟）

用的是开源工具 macos-wechat-export（DaneelOlivaw1），我们读过源码，没有外传数据。

```bash
git clone https://github.com/DaneelOlivaw1/macos-wechat-export.git ~/wechat-tool
```

```bash
cd ~/wechat-tool && sh build.sh
```

（编译时「发消息、操控界面」那部分报错可以不管，我们用不到。）

找出他本人账号的数据库位置（电脑上登录过多个微信号时，按最近修改时间认）：

```bash
cd ~/wechat-tool && python3.14 disk_inventory.py
```

**先在微信里存好草稿、正常退出微信**，再取密钥（中途手机上可能要确认一次登录）：

```bash
cd ~/wechat-tool && python3.14 bootstrap_keys.py --acknowledge-debug-copy
```

验证（能列出和「文件传输助手」的最近几条就成功了）：

```bash
cd ~/wechat-tool && python3.14 wechat_db.py verify
```

然后建配置文件 `~/.agentos/wechat.env`，填这几项：

```
WECHAT_DB_ROOT=…/xwechat_files/<他的账号目录>/db_storage
WECHAT_KEYS=~/wechat-tool/.wechat-local/keys.json
WECHAT_TOOL=~/wechat-tool
# 私聊全采；群只采这几个（群名关键词，逗号分隔）
WECHAT_GROUPS=
# 第一次每个会话往回读几天
WECHAT_INITIAL_DAYS=3
```

再运行一次 `~/AgentOS/ops/install-collectors.sh`。它会建一个**专用 Python**（`~/.agentos/wechat-python/`），只给微信采集用。最后：

**系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 只添加 `~/.agentos/wechat-python/bin/python3.14`。**
不加的话，每 10 分钟会弹一次「python 想访问其他 App 的数据」；也**不要**把平时用的 python3.14 加进去，那样权限就放大了。

### 要记住的

- **工具里的「发消息、操控微信」脚本（`send_verified.py`、`Send.swift` 等）一律不碰**：自动发消息才是真正招封号的，也违反「对外动作必须本人确认」这条底线。
- **密钥不要贴到任何聊天里。**
- **密钥什么时候失效**：关掉微信、重启电脑**不影响**；在微信里点「**退出登录**」、重装微信、微信大版本升级，密钥会失效，采集会报「打开微信库失败」，重做「取密钥」那一步就行。
- Homebrew 升级 Python 后专用 Python 可能失效，重跑 `install-collectors.sh`、重新加一次完全磁盘访问权限。
- 群消息很吵（我们试跑一天就有 6000 多条），**群只采白名单**。

### 不是 Mac 怎么办

- **Windows**：你之前在 Windows 上用 PowerShell 跑通过微信读取（WorkBuddy 里的「微信聊天记录读取」技能，背后是 wechat-cli）。但 Windows 版微信的数据库位置、取密钥方法都和 Mac 不同，`collect_wechat.py` 是按 Mac 工具写的，**要改代码适配、要实测**。
- **Linux 服务器**：没有个人微信可读，跳过这一块。
- **安卓手机上的微信**：数据在系统保护区里，要 root 才能读，**不做**。

---

## 五、公众号文章（「微信的浏览器读取」）

### 它做什么

微信聊天里别人发的公众号链接，以前只能看到一行标题。现在：

1. 微信采集把链接记下来；
2. 每 10 分钟，`ops/fetch_wechat_articles.py` 找出还没读过的链接；
3. 交给 `ops/fetch_wx.py` 去抓正文；
4. `ops/wechat_articles.py` 存成 Markdown，**文章里的图片一起下载**，归档到 `raw/wechat/articles/<发布日期>-<标题>.md`；
5. 搜索、复盘、提问时都能用。

### 原理，以及要知道的风险

- 程序直接打开 `mp.weixin.qq.com` 的文章，会被微信判成「环境异常」，要求验证。
- `fetch_wx.py` 是第三方开源工具（wechat-article-extract，MIT 协议），**把自己伪装成微信客户端**去请求，绕过这道验证。**这属于绕过平台的验证，用不用由使用者自己决定。**
- 它只能绕过「看浏览器标识」那一层；如果微信按 IP 封，就抓不到，会在下一轮重试。
- 被删除、下架的文章会记为「失效」，不再重试。

### 怎么配

**不用单独配**：只要第四节的微信采集配好了，`install-collectors.sh` 会自动建好它用的专用 Python（`~/.agentos/wxfetch-python/`，带 requests、beautifulsoup4）并装上定时任务。

### 不想绕验证的替代办法

- **按需读**：在带浏览器的 Claude 会话里说「把这篇读一下」，用真实浏览器打开，不绕验证；
- **转发**：在微信里打开文章，把正文转发给飞书机器人。

### 不是 Mac 怎么办

抓正文这一步只用 Python，任何系统都能跑；但**链接来自微信采集**，没有微信采集，它就没有链接可抓。

---

## 六、Windows、Linux、安卓：能不能装

### 为什么现在只支持 Mac

这几块是 Mac 专用的写法：
- **定时任务**：用的是 Mac 的 launchd（7 个 `.plist`）；
- **隔离账号**：`bootstrap-mac.sh` 用 Mac 的命令建隐藏账号 `agentos`，让 Claude 拿不到钥匙；
- **微信采集**：用的是 Mac 版微信的解密工具；
- **提取文字**：图片识字、读 PDF 用的是 Mac 自带的 Vision 和 PDFKit；
- **读代理设置**：用的是 Mac 的 `scutil`（小改动）。

其余部分（飞书机器人、邮件和飞书采集、搜索、出文件、告警、Claude Code 本身）都是 Python 或跨平台程序，Windows、Linux 都能跑。

### 各平台的建议

- **Linux 云服务器（推荐给「一直在线」的需求）**：
  - 定时任务改成 cron 或 systemd，建账号用 `useradd` + `sudoers`，和 Mac 的做法几乎一样；
  - 提取文字换成开源的识字工具（比如 tesseract）；
  - **没有个人微信**；国内服务器连 Claude 要代理，或者换成国内能直连的底座（第七节）；
  - 要改代码，估计一两天（推测）。
- **Windows**：
  - 最省事的是在 **WSL2**（Windows 自带的 Linux）里按 Linux 的方式跑；
  - 微信采集要在 Windows 这一边单独做，见第四节；
  - 电脑要一直开着、不睡眠。
- **安卓手机：只当遥控器，不当服务器。**
  - 对方在安卓（或苹果）手机上装**飞书**，和机器人聊天、收复盘，这就是「手机端」，**什么都不用部署**；
  - 为什么不在安卓手机上跑整套系统：
    1. 安卓会在后台**杀掉长时间运行的程序**，机器人和每 10 分钟的定时任务都保不住；
    2. Claude Code、LibreOffice 这些在安卓上没有正式支持。用 Termux（安卓上的 Linux 终端）能勉强跑一部分，但不稳定、很耗电、发热；
    3. 安卓微信的聊天数据要 **root** 才能读，第四节那套也用不了；
    4. 复盘一次要读几十个文件、跑好几分钟，手机不适合。
  - 如果只有一台旧安卓手机、没有电脑：更实际的是租一台便宜的 Linux 云服务器当「服务器」，安卓手机继续当遥控器。

---

## 七、换底座：用 WorkBuddy 代替 Claude 行不行

### 先分清两样东西

- **WorkBuddy 桌面版**：腾讯的 AI 办公助手，是给人用的界面，**不能直接当底座**。机器人需要的是「能在后台被程序调用」的命令行工具。
- **CodeBuddy Code**（命令 `codebuddy`）：WorkBuddy 背后的命令行工具，**可以当底座**。根据官方文档，它和 Claude Code 非常像：
  - 不打开界面、一次性运行：`codebuddy -p`；
  - 输出格式有 `stream-json`，最后有一条 `result` 消息，和我们解析 Claude 输出的方式一致；
  - 能接着上次的对话：`--resume`；
  - 有权限模式 `--permission-mode`（`acceptEdits`、`dontAsk`、`plan`……），有 `allow` / `deny` 权限清单，写法和 `.claude/settings.json` 几乎一样；
  - 有技能（每个技能一个文件夹加 `SKILL.md`）。
  - 来源：[无界面模式](https://www.workbuddy.ai/docs/cli/headless)、[命令行参考](https://www.workbuddy.ai/docs/cli/cli-reference)

### 有没有令牌：有

- **个人开发者用 API 密钥**：中国版在 https://copilot.tencent.com/profile/ 生成（国际版 https://www.codebuddy.ai/profile/keys）。
- 用法是两个环境变量，**中国版必须两个都设**：

```
CODEBUDDY_API_KEY=<密钥>
CODEBUDDY_INTERNET_ENVIRONMENT=internal
```

- 来源：[身份认证](https://www.workbuddy.ai/docs/cli/iam)。**文档没写个人密钥要不要付费、额度多少**，要在密钥页面上看。

### 换的话要改哪些地方（改动不大，但要实测）

1. **安装**：在 agentos 账号里装 `npm install -g @tencent-ai/codebuddy-code`（要 Node.js 18.20 以上），或者 `brew install Tencent-CodeBuddy/tap/codebuddy-code`。支持 Mac、Linux、Windows。（[安装](https://www.workbuddy.ai/docs/cli/installation)）
2. **钥匙**：`/Users/agentos/.env` 里把 `CLAUDE_CODE_OAUTH_TOKEN` 换成上面两个变量。
3. **启动脚本** `ops/run-claude.sh`：最后一行的 `claude` 换成 `codebuddy`，参数基本照搬。
   ⚠️ 文档里写「非交互模式必须加 `-y`（跳过所有权限检查）」。**这个不能加**：加了就等于拆掉「Claude 只能运行那几条命令」的防线。要先实测只用 `--permission-mode dontAsk` 能不能跑；不行的话，要想别的办法守住权限，不能直接放开。
4. **规则和技能换位置**（内容不用改）：
   - `CLAUDE.md` → `CODEBUDDY.md`（[记忆文件](https://www.workbuddy.ai/docs/cli/memory)）；
   - `.claude/settings.json` → `.codebuddy/settings.json`（[权限](https://www.workbuddy.ai/docs/cli/permissions)）；
   - `.claude/skills/` → `.codebuddy/skills/`（[技能](https://www.workbuddy.ai/docs/cli/skills)）。
5. **核对输出格式**：`ops/bridge/bridge.py` 和 `ops/daily-review.sh` 会读 `result` 消息里的 `result`、`is_error`、`session_id` 字段，字段名要实测是否一样。
6. **拿 10 个真实任务两边各跑一遍**（问聊天、写复盘、出合同、会前材料……），比一比效果再决定。

### 换了有什么好处、要注意什么

- **好处**：国内直连，不用代理；按 API 密钥用，每个用户可以用自己的密钥，比「每人一个 Claude 订阅」更适合给别人用；有 Windows 原生版本。
- **要注意**：
  - 效果要实测，尤其是「每条结论都带出处」的复盘、长任务；
  - 用什么模型可以在 `models.json` 里配（文档里的例子有 DeepSeek），具体有哪些模型、哪个效果好，文档没写全；
  - 网上有一些**非官方项目**（比如把 WorkBuddy 桌面版登录状态转成接口的 workbuddy2api 这类），**不要用**：不是官方的，可能违反服务条款，账号有风险。

### 其他换法（以前聊过的）

- **Claude Code 不动，只把背后的模型换成 DeepSeek**：DeepSeek 提供和 Claude 格式兼容的接口，改几个环境变量就能试，但有兼容问题的报告；
- **自己写一个 agent**：大模型 + 工具 + 循环，替换 `run-claude.sh` 的位置，机器人和采集都不用动；
- **Claude 订阅令牌不能给别人用**：每个人用自己的；面向国内用户，底座基本要用国内能直连的。

---

## 八、怎么让搬移更快

现在按这份文档手动做，第一次大约要 **1～2 小时**（大部分时间在飞书后台点权限、等审批）。还可以再省：

1. **一条命令做干净版**：把第一节的第 2～6 步写成脚本 `ops/make-template.sh`；
2. **一条命令装好**：把第三节串成 `ops/setup.sh`，一步步提示对方填什么、去哪里点；
3. **填空式配置模板**：`bridge.env`、`mail.env`、`wechat.env` 各给一份带注释的样例。

这三样还没做。另外，要支持 Linux、Windows，要先改定时任务和建账号这两块（第六节）。

---

## 九、我们踩过的坑（对方很可能也会遇到）

- **代理没开就装**：Claude Code 装不上、连不上。先开代理，再运行 `bootstrap-mac.sh`。
- **令牌失效**：飞书机器人突然不回话、复盘写不出来 → 多半是 Claude 令牌被撤销了，按第三节第 4 步重新生成一个。复盘脚本现在会直接显示原因（比如 `OAuth token revoked`）。
- **断网**：代理软件的 TUN 模式出问题，会让整台电脑上不了网，所有采集都报错。healthchecks 会发邮件提醒；恢复后采集会自动补齐，但当晚的复盘要手动补：`~/AgentOS/ops/daily-review.sh <日期>`。
- **关机、合盖**：电脑一关，复盘和备份都不跑。要一直开着、插着电。
- **飞书权限审批通过后没反应**：要重新运行 `feishu_auth.py` 授权。
- **改了代码不生效**：
  - 机器人的代码：提交后运行 `deploy.sh`；
  - 规则、技能：提交后运行 `deploy.sh` 或 `push-kb.sh`；
  - 采集、抓取这些脚本：保存就生效。
- **知识库不要在 `~/AgentOS` 里改**：它是 Claude 在运行副本里写的，要改就在飞书里让机器人改，否则同步时会冲突。
- **Claude 跑不了推送、sudo 这类命令**：这是有意的安全限制，这些命令由本人在终端里运行。
