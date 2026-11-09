# AgentOS（Windows 版）

个人 AI 助理：在手机飞书里一句话派活；它自动读你的飞书聊天、文档、邮件，每晚写复盘推到手机；出合同、出文件、起草邮件，对外发送前要你点头。方案见 [docs/plan.md](docs/plan.md)。

**怎么装：照着 [docs/deploy-windows.md](docs/deploy-windows.md) 一步步做。**

## 它在 Windows 上怎么跑

Windows 自带的 Linux 子系统（WSL2）里装一个 Ubuntu，整套系统跑在 Ubuntu 里；手机上装飞书当遥控器。

- **你的账号**（Ubuntu 里的普通用户）：代码 `~/AgentOS`；飞书机器人、采集、复盘、备份都以你的身份跑，钥匙在 `~/.agentos/`（只有你能读）。
- **agentos 账号**（专门跑 AI 的隔离账号）：运行副本 `/home/agentos/agentos`；底座的令牌在 `/home/agentos/.env`。AI 只能在这里干活，读不到你的钥匙。
- **Windows 这一头**：`ops/windows/setup-windows.ps1` 让电脑插电时不睡眠，登录后自动在后台启动 WSL。

## 底座可选

在 agentos 的 `~/.env` 里选（详见部署文档「第五步」）：
- **Claude 订阅**（Claude Code + 一年期令牌）
- **Claude Code + 其他公司的接口**（DeepSeek 等提供 Anthropic 兼容接口的，国内直连）
- **WorkBuddy**（腾讯的 CodeBuddy Code + API 密钥，要先实测）

## 和 Mac 版的区别

- 定时任务用 systemd（Mac 是 launchd）；隔离账号用 `useradd`（Mac 是 `dscl`）。
- 图片识字、读 PDF 用 tesseract 和 poppler（Mac 用系统自带的 Vision）。
- **暂时没有微信聊天采集和公众号抓取**：Windows 版微信的解密方法和 Mac 不同，还没做。

## 目录

```
CLAUDE.md             AI 的总规则（CODEBUDDY.md 是指向它的链接）
.claude/              AI 的权限 settings.json、技能 skills/（.codebuddy/ 里是指向它们的链接）
spaces/me/kb/         知识库：profile.md、people/、projects/、daily/、decisions.md
spaces/me/raw/        采集原文，只在运行副本里，不进 Git
ops/                  飞书机器人、采集、复盘、备份、安装脚本
ops/windows/          Windows 那一头的设置脚本
```

## 日常

- 看所有定时任务：`systemctl --user list-timers 'agentos-*'`
- 看飞书机器人：`systemctl --user status agentos-bridge`，日志 `tail -f ~/.agentos/logs/bridge.log`
- 改了代码并提交后，同步到运行副本、重启飞书机器人：`~/AgentOS/ops/deploy.sh`
- 手动补写某天的复盘：`~/AgentOS/ops/daily-review.sh 2026-11-09`
- 手动备份知识库到 GitHub：`~/AgentOS/ops/push-kb.sh`
- 离线测试：`python3 -m unittest ops/test_d2.py ops/test_extract_text.py`
