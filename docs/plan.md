# 方案摘要

原文（飞书，需登录）：
- 产品设计（MVP版）：https://ecnnizt3kpqd.feishu.cn/docx/LCY4dLFi4osuntxWjFDcQZfgnAd
- 实施步骤：https://ecnnizt3kpqd.feishu.cn/docx/OMBvd2ThDombgux9uwwcTtVanad
- 服务说明：https://ecnnizt3kpqd.feishu.cn/docx/Uv1RdmrIAon7JYxIvnvcO9ndn1H

## 要验证的假设

AI 读到你的真实工作信息后，能每天省下至少 30 分钟，并且你愿意每天打开它。第 4 天 23:00 手机自动收到第一份复盘，MVP 成立；个人版闸门是连续两周天天在用。

## 北极星场景

1. 飞书里一句话「把我和 X 这周的往来整理成会前材料」，十几分钟后同一对话收到 Word/PDF。
2. 每晚 23:00 自动读完当天飞书消息、日历、妙记、邮件，写一页复盘推到飞书和微信，全文进知识库。
3. 第二天说「接着昨天那件事」，它知道是哪件。

## 四天节奏（每天收工前过闸门）

| 天 | 主题 | 闸门 |
|---|---|---|
| D1 | 底座：桥接服务、Claude Code、仓库 | 手机发一句，60 秒内回复，第二句能接上下文 |
| D2 | 记忆：画像、人物页、项目页；飞书和邮箱采集；rg + SQLite FTS5 检索 | 问「本周和某人谈了什么」，每条结论带出处 |
| D3 | 动手与触达：会前材料、合同出件、网上调研三个技能；文件回传；确认卡片；PushPlus 微信推送 | 一句话出合同回到飞书；外发先收到确认卡片，点「发送」才发出 |
| D4 | 复盘与稳定：复盘技能、定时任务（每小时采集、22:55 补采、23:00 复盘、03:00 提交 kb）、失败告警、healthchecks 心跳 | 两晚复盘按时送达；故意弄坏邮箱授权码，10 分钟内收到告警 |

## 三条底线

1. 原文只追加不修改。
2. 对外动作必须你确认，由代码卡住：Agent 拿不到发送凭据，只能写 `outbox/pending/` 草稿。
3. 采集内容只是数据，里面的指令一律不执行。

## 在 Mac 上的实现差异

原方案是海外 Ubuntu 云主机；本项目改为这台 Mac（位于 Claude 支持地区）直接当服务器：

| 原方案 | Mac 上 |
|---|---|
| systemd service / timer | launchd（LaunchAgent），D4 的定时任务也用 launchd |
| Linux 用户 agent / bridge | 隐藏用户 `agentos` 跑 Claude；桥接服务用你自己的账号 |
| `/srv/agentos`，从 GitHub 拉取 | `/home/agentos/agentos`，从本地 `~/AgentOS` 克隆；GitHub 私有仓库作远程备份 |
| 凭据在 `/home/agent/.env` 等 | Claude 令牌 `/home/agentos/.env`；飞书等发送凭据 `~/.agentos/bridge.env` |
| Tailscale 连常开电脑 | 不需要：微信和 Agent 在同一台机器 |

额外加的一条边界：桥接服务从 `~/AgentOS` 运行，agentos 对它只读，Agent 无法通过改桥接代码绕过确认。
