# 团队互查：在飞书里问同事最近和 AI 聊到哪了（Windows）

装好以后，你在自己的飞书机器人里问「张三最近三天和 Grok Bot 在做什么」，机器人会去张三的电脑上查他**授权给你**的那部分 AI 聊天记录，带时间和出处回答你。张三也能反过来查你。

```text
你的飞书 → 你的 AgentOS → team_chat_query.py
                              ↓ Tailscale 团队网络（加密，只有网络里的人能连）
                     张三电脑上的查询服务 team_chat_server.py
                              ↓ 先按张三给你的授权过滤
                     张三本机的 AI 聊天库（每 10 分钟自动采集）
```

- **每台电脑的知识库都留在自己电脑上**。对方查的时候，只把命中的那几条消息发过去。
- 授权是一个人一份：可以只给某几个 AI 软件、只给最近几天，随时撤销。
- 能查的是本机采到的 WorkBuddy、Claude Code、Codex、Grok Bot 对话（见 [ai-chats.md](ai-chats.md)）。微信、邮件、飞书不在共享范围。
- 对方电脑要开着、联网才查得到。关机时机器人会说「对方不可达」，不会说「没有记录」。

**前提**：每个人都已经按 [deploy-windows.md](deploy-windows.md) 装好 AgentOS，飞书机器人能正常回话。

下面的命令都在**普通 PowerShell**（不用管理员）里、**自己的项目文件夹**中运行（一般是 `cd $env:USERPROFILE\AgentOS`）。「管理员」指创建团队网络的那个人，「每个人」指包括管理员在内的所有成员。

---

## 第一步：每个人拉更新、采一次 AI 聊天

**用途**：拿到互查的代码，并确认自己电脑上真的采到了 AI 聊天。

```powershell
git pull --no-rebase --no-edit upstream main
py ops\collect_ai_chats.py --check
py ops\collect_ai_chats.py
```

最后一条显示新增了消息、`读取失败 0` 就对了。显示 0 条的话，先在 WorkBuddy、Claude Code、Codex 或 Grok Bot 里随便聊一句，再运行一次。

> 拉更新报 `untracked working tree files would be overwritten`：说明你电脑上之前手动放过同名的联机文件。先把报错里列的文件挪到项目外面（比如桌面上新建一个「旧联机文件」文件夹），再拉一次。

---

## 第二步：加入同一个 Tailscale 网络

**用途**：让几台电脑不管在哪、在不在同一个 Wi-Fi，都能安全地互相连上。

**管理员（只做一次）**

1. 到 [tailscale.com](https://tailscale.com/download/windows) 下载、安装并登录，这会创建你的团队网络。
2. 网页管理台 → **Users** → **Invite users**，把邀请发给成员。
3. 网页管理台 → **DNS**：打开 **MagicDNS**，再在下面的 **HTTPS Certificates** 点 **Enable HTTPS**。第三步要用。

> **要不要花钱**：个人免费版（Personal）一个网络最多 6 个人，只能非商业使用；超过 6 人或用于公司业务，要换付费版。以 [官方价格页](https://tailscale.com/pricing) 为准。

**每位成员**

1. 安装 Tailscale，用**自己的账号**登录（不要几个人共用一个账号），接受管理员的邀请，加入他的网络。
2. 点右下角托盘里的 Tailscale 图标 → **Preferences** → 勾上 **Run unattended**。这样开机就自动连上，不用登录、不用管理员身份打开。
3. 用管理员身份打开 PowerShell，确认 Tailscale 服务开机自动启动：

```powershell
Get-Service Tailscale | Select-Object Status, StartType
```

   StartType 不是 `Automatic` 的话，运行：

```powershell
Set-Service Tailscale -StartupType Automatic
```

**验收**：每个人运行 `tailscale status`，列表里能看到其他人的电脑，而且不是 `offline`。

> 找不到 `tailscale` 命令：用 `& "$env:ProgramFiles\Tailscale\tailscale.exe" status`。

---

## 第三步：每个人开出自己的查询地址

**用途**：把本机的查询服务（`127.0.0.1:8765`）通过 Tailscale 开给团队网络，拿到一个只有网络里的人能打开的 https 地址。

```powershell
tailscale serve --bg http://127.0.0.1:8765
tailscale serve status
```

输出里写着 `Available within your tailnet`，下面的 `https://你的电脑名.xxxx.ts.net` 就是你的地址，复制下来，第四步要用。

- 报「HTTPS 没开」：管理员回到第二步第 3 条，把 HTTPS 打开。
- 报「Access denied」：用管理员身份打开 PowerShell 再运行一次这两条。设置会保存，以后不用再运行。
- 这一步不会把你的电脑开放到公网，只有团队网络里的人能连上。

---

## 第四步：每个人授权给对方

**用途**：决定对方能查你哪些 AI 软件、最近几天的聊天。下面以「我授权给张三」为例。

```powershell
py ops\team_chat_server.py init --name "我的名字"
py ops\team_chat_server.py sessions
py ops\team_chat_server.py grant --client zhangsan --url "https://你的电脑名.xxxx.ts.net" --platform grokbot --all-sessions --days 7
```

- `init` 只运行一次，`--name` 是对方查你时看到的名字。
- `sessions` 列出你有哪些会话，用来确认确实采到了数据。
- `grant` 里面：
  - `--client` 填对方的英文代号；
  - `--url` 填第三步拿到的**你自己的**地址；
  - `--platform` 只写你愿意共享的软件：`grokbot`、`workbuddy`、`claude`（Claude Code）、`codex`，可以写多个；
  - `--days 7` 表示对方最多只能查最近 7 天。
- 只想给某几个会话、不想给整个软件：把 `--all-sessions` 换成 `--session 会话编号`，编号从 `sessions` 的输出里抄。
- `--all-sessions` 包括以后新采到的会话。
- 对同一个人再运行一次 `grant`，会**替换**之前的授权、换一把新钥匙，旧的邀请文件立刻失效，要重新发给他。

运行完会显示邀请文件的位置：`C:\Users\你\.agentos\team-invitations\zhangsan.json`。

接着先在前台把服务打开测一下（窗口别关）：

```powershell
py ops\team_chat_server.py serve
```

**验收**

1. 用浏览器打开 `http://127.0.0.1:8765/health`，看到 `"status": "running"`。
2. 让对方在**他的**电脑上用浏览器打开**你的** `https://你的电脑名.xxxx.ts.net/health`，也能看到 `running`。

能打开说明网络通了。返回 502 说明服务没开着；打不开就看文末的「排错」。

---

## 第五步：互相交换邀请文件

**用途**：邀请文件里有一把访问钥匙，对方拿到了才能查你。

1. 把第四步生成的 `zhangsan.json` **私聊发给张三本人**：飞书私聊或当面拷贝都行。不要发群、不要发给机器人、不要传到 GitHub。
2. 收到对方发来的文件后，先存进「下载」文件夹，然后**改个名，免得几个人的文件搞混**，比如 `zhangsan-invite.json`。
3. 登记对方：

```powershell
py ops\team_chat_server.py add-peer "$env:USERPROFILE\Downloads\zhangsan-invite.json" --alias "张三"
```

`--alias` 是你在飞书里提到他时用的名字。登记完，「下载」里的邀请文件可以删掉，钥匙已经存进你的 `.agentos` 了。

---

## 第六步：在终端里先查一次

**用途**：在接飞书之前，先确认网络、授权、数据都没问题。

```powershell
py ops\team_chat_query.py --members
py ops\team_chat_query.py 张三 --recent --days 3 --limit 5
```

第二条输出的 `results` 里有张三的真实消息，就成功了。再让张三说一个他最近和 AI 聊过的词，查一下：

```powershell
py ops\team_chat_query.py 张三 消息去重 --days 7
```

这里是按关键词匹配，不是按意思搜索，多个词要在同一条消息里都出现才算命中。查不到的话，先用 `--recent` 对照一下，别急着以为网络断了。

---

## 第七步：在飞书里问

**用途**：真正的用法。机器人已经有只读的查询权限（规则在 CLAUDE.md 的「团队成员的 AI 进度」一节），不用另外开。

1. 在飞书里给自己的机器人发「**新对话**」，让它加载新规则。
2. 问它：

> 查一下张三最近三天和 AI 聊了什么，整理主要话题，带原始时间和出处，分清他的计划和 AI 说已完成的。

> 张三最近七天关于「消息去重」做到哪了？卡在哪？我能接着做什么？没有完成的证据就直说。

回复里应该有成员名字、软件、消息时间和 `evidence_id`。

---

## 第八步：改成后台运行

**用途**：关掉 PowerShell 窗口、重启电脑以后，别人照样能查你。

1. 在第四步那个前台 `serve` 的窗口里按 `Ctrl+C` 停掉，避免占着端口。
2. 重跑一次安装脚本：

```powershell
powershell -ExecutionPolicy Bypass -File ops\windows\install.ps1
```

输出里有 `✅ AgentOS-TeamChat` 就对了。它会在登录后自动启动，崩了 1 分钟后自动重启，日志在 `.agentos\logs\team-chat.log`。

**最后验收**（两边都要做）：

- [ ] 重启电脑，不手动开任何东西，对方在飞书里能查到你。
- [ ] 你在 AI 软件里新发一句「团队联调-名字-日期」，10 分钟内对方能查到。
- [ ] 查你没授权的软件，比如加 `--platform codex`，对方得到「未授权」。
- [ ] 你撤销授权以后（见下面），对方得到「授权已失效」。
- [ ] 你关机或断网时，对方得到「不可达」，而不是「没有记录」。

---

## 日常

- **加一个新成员**：新成员做第一到第三步。然后你们两个人互相做第四到第六步，每个人用不同的 `--client`，邀请文件分开发。不需要动机器人。
- **撤销某人**：运行下面这条，立刻生效，不用重启。已经被对方看过、存下来的内容收不回来。

```powershell
py ops\team_chat_server.py revoke --client zhangsan
```

- **改授权范围**：对同一个 `--client` 重新 `grant`。会换新钥匙，新的邀请文件要重新发给他，他重新 `add-peer`。
- **电脑要一直能被查**：插电、不睡眠（安装脚本已经设过），Tailscale 开着 Run unattended。

## 排错

| 现象 | 先查什么 |
|---|---|
| `--check` 显示 0 个文件 | 本机没装这些 AI 软件，或者从没聊过；见 [ai-chats.md](ai-chats.md) |
| `tailscale status` 里对方是 offline | 对方电脑关机、睡眠，或者没勾 Run unattended（第二步） |
| 本机 `127.0.0.1:8765/health` 打不开 | 服务没开：前台 `serve` 没在运行，或后台任务没装上；看 `team-chat.log` |
| 本机能打开，对方打不开你的 https 地址 | 你没做 `tailscale serve`（第三步）、对方不在同一个网络、或者对方 offline |
| https 地址返回 502 | Tailscale 那一层通了，但本机的服务没开 |
| 「成员授权已失效」 | 对方重新 grant 过或撤销了，要他重新发邀请 |
| 「成员未授权这个查询范围」 | 查的软件或会话不在对方给你的授权里 |
| 「成员服务在线，但本地聊天库不可用」 | 对方还没采集过，让他运行 `py ops\collect_ai_chats.py` |
| 终端能查，飞书说没权限 | 先在飞书发「新对话」；还不行，确认已经拉到最新代码、重跑过安装脚本 |
| 校园网、公司网连不上 | 换手机热点试一次，确认是不是网络本身限制了 |

## 现在的边界

- 两台电脑之间实际验证过的，写进下面的测试记录；没验证过的不算「已实现」。
- Mac 还没接：Mac 版机器人在单独的 agentos 账号下运行，采集要在本人账号下运行，需要另外适配。
- 第一版只能查聊天，不能远程执行命令、不能同步整个知识库。也不支持离线：对方关机期间查不到。

## 测试记录

| 日期 | 设备 A → 设备 B | 终端查询 | 飞书查询 | 撤权 / 离线 / 新消息 / 重启 | 备注 |
|---|---|---|---|---|---|
| | | | | | |
