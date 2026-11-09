# AgentOS

个人 AI 助理 MVP：打通「信息进来 → AI 记住 → AI 动手 → 结果回到手机」。方案见 [docs/plan.md](docs/plan.md)。

本机（你的 Mac）就是服务器：

| 角色 | 谁 | 在哪 |
|---|---|---|
| 开发仓库 | 你的账号 | `~/AgentOS`（本仓库） |
| 桥接服务 | 你的账号，launchd 托管 | 代码 `~/AgentOS/ops/bridge/`，状态 `~/.agentos/`（凭据、队列库、日志） |
| Agent（Claude Code） | 隐藏用户 `agentos` | 运行副本 `/Users/agentos/agentos`，令牌 `/Users/agentos/.env` |

## 目录

```
CLAUDE.md            Agent 总规则
.claude/settings.json Agent 权限（第二道防线）
.claude/skills/      技能（D3 起）
shared/              共享项目库（团队版启用）
spaces/me/kb/        知识库：profile.md、people/、projects/、daily/、decisions.md
spaces/me/raw/       采集原文，只追加，不进 Git
spaces/me/outbox/    待回传文件与待确认草稿，不进 Git
ops/                 桥接服务、初始化与部署脚本、launchd 配置
```

## D1 清单：手机发一句，60 秒内收到回复

所有令牌、密钥只写进下面指定的文件，不要发在任何聊天里。

**0. Mac 常开**
- 系统设置 → 电池 → 选项：打开「插电时防止自动睡眠」。
- MacBook 合盖会睡眠：保持开盖插电，或接外接显示器。
- 桥接服务是登录项级别的服务，Mac 重启后需要你登录一次。

**1. 提交代码**（运行副本从本仓库克隆，需要至少一次提交）

**2. 初始化 Mac**（要输一次管理员密码）
```bash
sudo ~/AgentOS/ops/bootstrap-mac.sh
```
建 `agentos` 用户、克隆运行副本、配 sudo 规则、以 agentos 身份装 Claude Code。
如果系统设置里开着代理（国内连 Claude 必需），脚本会自动把它写进 agentos 的 `~/.env`，安装和之后调用 Claude 都走代理。**先打开代理软件再运行。**

**3. 生成 Claude 令牌**
```bash
sudo -u agentos -i
```
在 agentos 的终端里（第一行让这个终端也走代理）：
```bash
set -a; . ~/.env; set +a
~/.local/bin/claude setup-token
```
浏览器授权后终端打印一年期令牌，写进 agentos 的 `~/.env`：
```bash
nano ~/.env
```
填 `CLAUDE_CODE_OAUTH_TOKEN=...`，保存后 `exit` 回到你的账号。

**4. 飞书应用（第一部分）**
飞书开放平台 → 创建「企业自建应用」：
- 添加「机器人」能力。
- 权限管理里开：`im:message.p2p_msg:readonly`（读取用户发给机器人的单聊消息）、`im:message:send_as_bot`（以应用身份发消息）、`im:resource`（读取和上传消息中的文件，D3 用）。
- 记下「凭证与基础信息」里的 App ID 和 App Secret。

**5. 装桥接服务**
```bash
~/AgentOS/ops/install-bridge.sh
```
第一次运行会生成 `~/.agentos/bridge.env`，填好 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`：
```bash
nano ~/.agentos/bridge.env
```
再运行一次 `install-bridge.sh`，服务启动并连上飞书长连接。看日志：
```bash
tail -f ~/.agentos/logs/bridge.log
```

**6. 验证 Agent**
```bash
~/AgentOS/ops/check-agent.sh
```
应输出 `Claude 回复：OK` 和两个 OK。

**7. 飞书应用（第二部分）**——顺序不能反，必须在第 5 步服务已连上之后：
- 事件与回调 → 订阅方式选「使用长连接接收事件」→ 保存。
- 添加事件 `im.message.receive_v1`（接收消息）。
- 创建版本并发布。

**8. 绑定你自己**
在飞书里搜到机器人，随便发一句。它会回你的 `open_id`，填进 `~/.agentos/bridge.env` 的 `OWNER_OPEN_ID`，然后：
```bash
~/AgentOS/ops/install-bridge.sh
```
之后只响应你本人，其他人发来的一律忽略。

**收工闸门**：手机发「介绍一下你能做什么」，60 秒内收到回复；紧接着发第二条，它能接上第一条的上下文。

## D2 清单：问「这周我和某某谈了什么」，每条结论都带出处

**1. 同步代码**：`~/AgentOS/ops/deploy.sh`。如果 D1 的 `bootstrap-mac.sh` 是在本次更新前跑的，再跑一次（可重复跑），让 `raw/` 对你的账号可写。

**2. 邮箱采集**
- 在邮箱网页版设置里开启 IMAP，生成**授权码**（不是登录密码）。
- 运行一次生成配置文件，填好后再运行一次：
```bash
~/AgentOS/ops/install-collectors.sh
nano ~/.agentos/mail.env
~/AgentOS/ops/install-collectors.sh
```
成功后先拉最近 7 天的邮件，之后每 10 分钟自动拉一次。广告、订阅、系统通知会被过滤；邮件保持未读状态。原文存在 `/Users/agentos/agentos/spaces/me/raw/mail/<日期>/`。

**2b. 飞书采集**（以你本人身份读群聊和私聊）
- 开放平台给应用开通以下权限（都选「用户身份」）：`im:message:readonly`、`im:message.p2p_msg:get_as_user`、`im:message.group_msg:get_as_user`、`im:chat:readonly`、`search:message`、`contact:user.base:readonly`、`contact:contact.base:readonly`、`offline_access`。
- 安全设置 → 重定向 URL 添加 `http://localhost:18080/feishu/callback`。
- 授权（浏览器里点同意），再装定时任务：
```bash
python3 ~/AgentOS/ops/feishu_auth.py
~/AgentOS/ops/install-collectors.sh
```
- 想先看能读到哪些会话、往回能读多久（只统计不保存）：`python3 ~/AgentOS/ops/feishu_probe.py`
- 范围：你所在的全部群聊 + 搜索找到的私聊（私聊靠「搜索消息」接口发现，群列表接口不含私聊）。第一次拉最近 30 天，之后每 10 分钟增量。
- 原文：`raw/feishu/<日期>/<会话名>-<会话ID后6位>.md`，每条一行「- 时:分 发送人：内容」，只追加。
- 授权凭证 `~/.agentos/feishu_user_token.json` 自动续期；超过约 7 天没跑、或改了权限，重新运行 `feishu_auth.py`。

**3. 画像**：在飞书里把 D0 准备的材料发给机器人（自我介绍、公司、常往来的 10 个人、正在跟的 5 个项目、文字偏好），说「整理进我的画像、人物页和项目页」。缺的它会一次问你 3 个问题。

**4. 检索自测**（在你的账号下）：
```bash
sudo -u agentos -H /usr/bin/python3 /Users/agentos/agentos/ops/search.py 关键词
```

**收工闸门**：飞书问「这周我和某某谈了什么」，每条结论后面都有出处，打开文件能对上原文。

> 飞书云文档、妙记、日历也一起采：聊天和邮件里出现过链接的文档存正文（改过另存新版本），妙记存文字记录，日程每 10 分钟覆盖写未来 7 天。

## 知识库备份到 GitHub（计划书 D4 的一项，提前做）

- 每天 03:00 自动把 `spaces/me/kb/`（画像、人物页、项目页、复盘、拍板记录）备份到 GitHub；`raw/` 原文和 `outbox/` 草稿不上传（`.gitignore` 排除）。
- 方向：运行副本里提交 → 拉回 `~/AgentOS` → 推到 GitHub，三处是同一串提交。
- 第一次要重跑 `sudo ~/AgentOS/ops/bootstrap-mac.sh`（把 `backup-kb.sh` 加进免密规则），再运行 `~/AgentOS/ops/install-collectors.sh` 装定时任务。
- 手动立刻备份：`~/AgentOS/ops/push-kb.sh`；日志 `~/.agentos/logs/push-kb.log`。
- 注意：仓库协作者能看到知识库内容。

## 推送与告警（计划书 D3 微信推送、D4 告警，提前做）

- `ops/notify.py`：给你本人推一条通知，飞书（agentos 机器人私聊）+ 微信（PushPlus）。测试：`python3 ~/AgentOS/ops/notify.py --test`。
- `ops/run-job.sh`：所有定时任务都经它启动。失败或超时 → 推飞书和微信（任务名、退出码、日志最后 20 行）；同一任务连续失败只提醒第一次，恢复时再通知一次。成功 → 去 healthchecks.io 打卡（只访问地址，不发送任何日志内容）；失败不打卡（Mac 停摆导致长时间没打卡时，由 healthchecks 发邮件）。
- 配置都在你的账号下，Agent 读不到：
  - `~/.agentos/bridge.env` 加一行 `PUSHPLUS_TOKEN=...`（没有就只推飞书）
  - `~/.agentos/healthchecks.env`：每个任务一行打卡地址，`HC_COLLECT_MAIL=`、`HC_COLLECT_FEISHU=`、`HC_PUSH_KB=`（没有就不打卡）
- 飞书接口遇到内部错误或网络抖动会自动重试一次，偶发错误不告警。

## 日常

- 改了代码并提交后同步到运行副本、重启桥接服务：`~/AgentOS/ops/deploy.sh`
- 飞书里发「帮助」看能做什么；「新对话」从头开始；「停止」中止当前任务并清掉排队；「状态」看有没有在干活。
- 桥接服务测试（离线，不碰飞书和 sudo）：`~/.agentos/venv/bin/python -m unittest ~/AgentOS/ops/bridge/test_bridge.py`
- 检索、邮箱和飞书采集测试（离线，假 IMAP、假飞书）：`/usr/bin/python3 -m unittest ~/AgentOS/ops/test_d2.py`

## 排错

| 现象 | 看哪里 |
|---|---|
| 发消息没反应 | `tail ~/.agentos/logs/bridge.log`；`launchctl print gui/$(id -u)/com.agentos.bridge` |
| 回「Agent 异常退出」 | 运行 `ops/check-agent.sh`；多半是令牌没填或过期 |
| `sudo: a password is required` | `/etc/sudoers.d/agentos` 不在，重跑 `sudo ops/bootstrap-mac.sh` |
| 飞书后台保存长连接失败 | 桥接服务还没连上，先完成第 5 步 |
