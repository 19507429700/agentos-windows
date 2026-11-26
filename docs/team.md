# 团队互查：在飞书里问同事最近和 AI 聊到哪了（Windows）

装好以后，你在自己的飞书机器人里问「张三最近三天和 AI 在做什么」，机器人会去张三的电脑上查他共享的 AI 聊天记录，带时间和出处回答你。团队里每个人都能这样互相查。

```text
你的飞书 → 你的 AgentOS → team_chat_query.py
                              ↓ Tailscale 团队网络（加密，只有网络里的人能连）
                     张三电脑上的查询服务（只读）
                              ↓
                     张三本机的 AI 聊天库（每 10 分钟自动采集）
```

**全团队统一规则**

- 共享的内容：本机采到的**所有** AI 软件聊天，包括 WorkBuddy、Claude Code、Codex、Grok Bot（采集范围见 [ai-chats.md](ai-chats.md)）。微信、邮件、飞书不在共享范围。
- 时间范围：别人最多查到你**最近 15 天**的聊天。
- 谁能查：在同一个 Tailscale 团队网络里的人都能查。要让某人查不了，管理员在 Tailscale 后台把他移出网络。
- 数据存在哪：每个人的聊天都留在自己电脑上。别人查的时候，只把搜到的那几条（每次最多 20 条）传过去。
- 不用发邀请文件，也不用交换地址：Tailscale 会告诉对方电脑「这次是谁来查的」，而且这个身份没法伪造（[官方说明](https://tailscale.com/docs/features/tailscale-serve)）。
- 对方电脑要开着、联网才查得到。关机时机器人会说「不在线」，不会说「没有记录」。

**前提**：每个人都已经按 [deploy-windows.md](deploy-windows.md) 装好 AgentOS，飞书机器人能正常回话。

下面的命令都在**普通 PowerShell**（不用管理员）、**自己的项目文件夹**里运行（`cd $env:USERPROFILE\AgentOS`）。

---

## 第一步（管理员做一次）：建团队网络

**用途**：让大家的电脑不管在哪、在不在同一个 Wi-Fi，都能安全地互相连上。

1. 安装 [Tailscale](https://tailscale.com/download/windows) 并登录，这会建好你的团队网络。
2. 网页管理台 → **DNS**：打开 **MagicDNS**，再在下面的 **HTTPS Certificates** 点 **Enable HTTPS**。
3. 网页管理台 → **Users** → **Invite users**，把邀请发给成员。

> **要不要花钱**：个人免费版一个网络最多 6 个人，只能非商业使用；超过 6 人或用于公司业务，要换付费版。以[官方价格页](https://tailscale.com/pricing)为准。

## 第二步（每人一次）：加入网络

**用途**：把自己的电脑接进团队网络。

1. 安装 Tailscale，用**自己的账号**登录（不要几个人共用一个账号），接受管理员的邀请。
2. 托盘里的 Tailscale 图标 → **Preferences** → 勾上 **Run unattended**。这样开机就自动连上，不用登录、不用管理员身份打开。

**看到什么算成功**：运行 `tailscale status`，列表里能看到其他人的电脑。

## 第三步（每人一次）：拉更新、采集、打开共享

```powershell
git pull --no-rebase --no-edit upstream main
```

```powershell
py ops\collect_ai_chats.py
```

```powershell
py ops\team_chat_server.py on --name "你的名字"
```

- 第一条：拿到互查功能。
- 第二条：把你和 AI 的聊天采进本机，先有数据才查得到。显示「读取失败 0」就行。
- 第三条：**打开共享，只需这一条**。`--name` 是别人查你时用的名字。它会自动做三件事：
  - 按统一规则打开共享；
  - 给你的电脑挂上团队网络地址；
  - 装成开机自动运行。

**看到什么算成功**：显示 `✅ 已打开共享`，下面有你的地址 `https://你的电脑名.xxxx.ts.net`。

- 报「Access denied」：用管理员身份打开 PowerShell，运行一次 `tailscale serve --bg http://127.0.0.1:8765`，再回普通 PowerShell 重新运行第三条。
- 拉更新时报 `would be overwritten`：说明以前手动拷过别的联机文件。先运行 `git stash push --include-untracked -m "旧改动"`，再拉一次。

## 第四步：在飞书里问

1. 给自己的机器人发「**新对话**」，让它读到新规则。
2. 问它：

> 团队里现在谁能查？

> 查一下张三最近三天和 AI 聊了什么，整理主要话题，带时间和出处，分清他的计划和 AI 说已完成的。

> 张三最近关于「消息去重」做到哪了？卡在哪？我能接着做什么？

**看到什么算成功**：回复里有成员名字、软件、消息时间和 `evidence_id`。

不确定是不是机器人的问题，可以先在终端里手动查：

```powershell
py ops\team_chat_query.py --members
```

```powershell
py ops\team_chat_query.py 张三 --recent --days 3
```

---

## 自己怎么控制

| 想做什么 | 命令 |
|---|---|
| 看现在共享了什么、**最近谁查过我** | `py ops\team_chat_server.py status` |
| 列出我的会话（编号 + 我说的第一句话） | `py ops\team_chat_server.py sessions` |
| 某个会话不给别人看（立刻生效） | `py ops\team_chat_server.py hide 会话编号` |
| 恢复共享这个会话 | `py ops\team_chat_server.py unhide 会话编号` |
| 改成只让别人查最近 7 天 | `py ops\team_chat_server.py on --days 7` |
| **关掉共享，谁都查不了我** | `py ops\team_chat_server.py off` |
| 重新打开 | `py ops\team_chat_server.py on` |

已经被别人查到、看过的内容收不回来。

## 排错

| 现象 | 先查什么 |
|---|---|
| 采集显示 0 个文件 | 本机没装这些 AI 软件，或者从没聊过；见 [ai-chats.md](ai-chats.md) |
| `--members` 里没有某人 | 他还没运行第三步的 `on`，或者电脑没开、没连 Tailscale |
| 「某某的电脑现在不在线」 | 对方关机、睡眠，或者没勾 Run unattended |
| 「某某关掉了共享」 | 对方运行了 `off` |
| 自己 `off` 了，在飞书里查自己还能查到 | 正常：机器人查你本人时会直接读你本机的记录，`off` 只挡别人。想自己验证 `off`，用终端 `py ops\team_chat_query.py 自己的名字 --recent`，应提示「关掉了共享」 |
| 「聊天库暂时读不了」 | 对方还没采集过，让他运行 `py ops\collect_ai_chats.py` |
| `status` 显示本机服务没在运行 | 等一分钟再看；还不行，看 `.agentos\logs\team-chat.log`，或重新运行 `on` |
| 终端能查，飞书说没权限 | 先在飞书发「新对话」；还不行，确认拉到了最新代码 |
| 校园网、公司网连不上 | 换手机热点试一次，确认是不是网络本身限制了 |

## 现在的边界

- Mac 还没接：Mac 版机器人在单独的 agentos 账号下运行，采集要在本人账号下运行，需要另外适配。
- 只能查聊天，不能远程执行命令，也不能同步整个知识库。对方关机期间查不到。
- 两台电脑之间实际验证过的，写进下面的测试记录；没验证过的不算「已实现」。

## 测试记录

| 日期 | 谁查谁 | 终端查询 | 飞书查询 | 关闭共享 / 离线 / 新消息 / 重启 | 备注 |
|---|---|---|---|---|---|
| 2026-11-25 | 黄佳旭查自己（旧版两两授权） | 通过 | 通过 | — | 新版统一规则待两台实测 |
| 2026-11-25 | 黄佳旭（Windows）查艾依佳（Windows），新版统一规则 | — | 通过：WorkBuddy 11-21～11-25，带时间和 evidence_id | 待测 | 两人各运行一次 on 即通；反方向待测 |
