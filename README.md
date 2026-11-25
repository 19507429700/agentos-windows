# AgentOS（Windows 版）

个人 AI 助理：在手机飞书里一句话派活；它自动读你的飞书聊天、文档、邮件，每晚写复盘推到手机；出合同、出文件、起草邮件，对外发送前要你点头。方案见 [docs/plan.md](docs/plan.md)。

**怎么装：照着 [docs/deploy-windows.md](docs/deploy-windows.md) 一步步做。**

本地AI对话（WorkBuddy、Claude Code、Codex、Grok Bot）采集与更新见 [docs/ai-chats.md](docs/ai-chats.md)。Grok Bot仅读取已缓存到本机的文字和语音转写。

团队成员之间互查 AI 聊天进度（经 Tailscale，按人授权、可撤销）见 [docs/team.md](docs/team.md)。

## 它在 Windows 上怎么跑

- 直接跑在 Windows 上，不用装 Linux。手机上装飞书当遥控器。
- 代码、AI 干活的地方、采集的原文，都在项目文件夹 `C:\Users\你的用户名\AgentOS`；原文 `spaces\me\raw\` 不上传 GitHub。
- 钥匙都在 `C:\Users\你的用户名\.agentos`，权限规则禁止 AI 读它（`.claude\settings.json`）。
- 飞书机器人和定时任务在「任务计划程序」里，名字以 `AgentOS-` 开头；日志在 `.agentos\logs\`。

## 底座三选一

在 `.agentos\agent.env` 里选（详见部署文档第四步）：
- **Claude 订阅**（Claude Code + 一年期令牌，国内要代理）
- **Claude Code + 其他公司的接口**（DeepSeek 等，国内直连）
- **WorkBuddy**（腾讯的 CodeBuddy Code + API 密钥，要先实测）

## 和 Mac 版的区别

- 脚本都是 Python 和 PowerShell（Mac 版是 bash）；定时任务用「任务计划程序」（Mac 是 launchd）。
- 没有单独的 agentos 账号，AI 和你用同一个 Windows 账号，靠权限规则护住钥匙。
- 没有「运行副本」：只有一份代码，知识库直接在这里提交、备份。
- 图片识字、读 PDF 用 Windows 自带的识字功能和 pypdf（Mac 用 Vision、PDFKit）。
- **暂时没有微信聊天采集和公众号抓取。**

## 目录

```
CLAUDE.md             AI 的总规则（用 CodeBuddy 时会自动复制一份 CODEBUDDY.md）
.claude\              AI 的权限 settings.json、技能 skills\
spaces\me\kb\         知识库：profile.md、people\、projects\、daily\、decisions.md
spaces\me\raw\        采集原文，不进 Git
ops\                  飞书机器人、采集、复盘、备份、检索、出文件
ops\windows\          安装脚本 install.ps1、识字脚本 ocr.ps1
```

## 日常

- 填或改钥匙：`py ops\set_key.py <bridge|mail|agent|healthchecks> <名字> [值]`
- 检查底座：`py ops\check_agent.py`
- 重启飞书机器人：`py ops\deploy.py`
- 拉更新：`git pull --no-rebase --no-edit upstream main`，再 `py ops\deploy.py`
- 手动补写某天的复盘：`py ops\daily_review.py 2026-11-09`
- 手动备份知识库：`py ops\push_kb.py`
- 离线测试：`py -m unittest ops\test_windows.py ops\test_extract_text.py ops\test_d2.py`
