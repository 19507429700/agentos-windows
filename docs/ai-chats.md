# 本机 AI 对话接入

支持 WorkBuddy（~/.workbuddy/projects）、Claude Code（~/.claude/projects）、Codex（~/.codex/sessions 与 archived_sessions）的本机 JSONL。
不包含 Claude 网页版、ChatGPT 网页版等云端历史；需用户从对应平台导出后另加适配器，不能仅凭安装了客户端就读取所有云端对话。

## 已安装用户升级（此步骤在功能发布后使用）

在自己的 AgentOS 目录中，先确认本地没有未处理的代码改动，然后拉取更新：

```powershell
cd "$env:USERPROFILE\AgentOS"
git pull --no-rebase --no-edit upstream main
powershell -ExecutionPolicy Bypass -File ops\windows\install.ps1
```

安装输出应包含 AgentOS-collect-ai。仅运行 deploy.py 只会重启机器人，不会新增定时任务。
然后检查和手动采集一次：

```powershell
py ops\collect_ai_chats.py --check
py ops\collect_ai_chats.py
```

检查输出会列出查找目录、目录是否存在及 JSONL 文件数。文件数不是有效对话数，Claude 目录可能全是 AgentOS 后台记录，实际会排除。
最后在飞书问“查一下我最近与 WorkBuddy 讨论过的某个具体主题，给出日期和出处”，核对结果与原始对话。

## 其他电脑与自定义数据目录

默认目录根据当前 Windows 用户主目录计算，不依赖软件安装在 C 盘还是其他盘。
Codex 设置了 CODEX_HOME 时，默认使用该目录下的 sessions 和 archived_sessions。
程序只检查已适配的目录，不搜索整块硬盘，也不会登录云端账户；软件变更存储格式时仍需新的解析适配。

记录迁移过时，在 ~/.agentos/ai_chats.json 中填自己的实际路径，例如：

```json
{
  "enabled": true,
  "initial_days": 30,
  "roots": {
    "workbuddy": ["D:/MyAIData/.workbuddy/projects"],
    "codex": ["%USERPROFILE%/.codex/sessions", "%USERPROFILE%/.codex/archived_sessions"]
  }
}
```

上例仅为路径写法示范，应替换为自己的实际数据目录。路径支持 ~ 和环境变量；不要把 PowerShell 的 $env:... 语法写进 JSON。
roots 会替代默认平台列表，没列出的平台不采集。目录不存在或没有兼容的会话文件时，不会凭空取得聊天。
这里只在一台 Windows 电脑真实记录及自动化测试中验证过，尚不能保证每个客户端版本、每台电脑都能自动识别。

## 使用与暂停

`py ops/collect_ai_chats.py --check`：检查能找到的平台及文件数，不写入对话。
`py ops/collect_ai_chats.py`：采集，首次最近30天；历史按消息真实北京时间归档。
`py ops/search.py 关键词 --source ai`：检索。

默认每10分钟运行 AgentOS-collect-ai；每日复盘前也补采。
可在 ~/.agentos/ai_chats.json 设置 enabled=false 暂停新采集；已有归档仍可检索。
可设置 initial_days、roots、exclude_sessions、exclude_cwds。例如：
```json
{"enabled":true,"initial_days":30,"exclude_sessions":[],"exclude_cwds":[]}
```
关闭某个平台时，在 roots 中只列要保留的平台（值为目录列表）。排除项只阻止今后采集，不自动删除历史。

## 数据边界与复盘

仅提取用户和 AI 的可见文字，不读凭据文件、系统提示、工具结果、内部推理；只对常见凭据模式进行尽力脱敏，重要秘密不应粘贴进任何聊天。
导入 raw/ai 后会像其他原文一样，可能被已配置的 Agent 模型读取处理；原始对话不纳入 Git 备份，但复盘/知识库里的摘要可能随既有备份上传。
原文留在 raw/ai；采集状态在 ~/.agentos/ai_chats.db；日志在 ~/.agentos/logs/collect-ai.log。
用户消息与 AI 回复分开标注；计划不等于完成，AI声称完成要核实。同一事项跨平台讨论要合并，历史补采不算当天工作。
活跃 JSONL 的半行留待下轮；相同消息 ID 更新不重复追加；自动每日复盘、AgentOS 自己通过 SDK 调用 Claude 产生的后台镜像、子代理会话不作为新的用户活动。
