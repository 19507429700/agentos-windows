# 本地AI对话采集（含Grok Bot）

支持WorkBuddy、Claude Code、Codex的本地JSONL，以及xAI Grok Bot桌面端已经缓存的文字和语音转写。只读取本机已适配的文件，不登录网页，不读取浏览器Cookie或独立凭据，不访问平台私有云端接口。

| 来源代号 | 默认位置 | 边界 |
|---|---|---|
| workbuddy | `~/.workbuddy/projects` | 兼容的本地JSONL |
| claude | `~/.claude/projects` | Claude Code；排除AgentOS自身SDK后台镜像 |
| codex | `~/.codex/sessions`、`archived_sessions` | 设置CODEX_HOME时改用该目录 |
| grokbot | 下文Windows/Mac路径 | 仅Grok Bot已缓存内容，不保证完整历史 |

不包含ChatGPT、Claude普通网页/桌面端的云端历史；Codex使用GPT模型也不等于读取了ChatGPT聊天。

## 截图和文件（2026-11-26 起）

- **截图**：你在 WorkBuddy、Claude Code、Codex 里贴给 AI 的图，复制一份到 `spaces/me/raw/ai/images/<日期>/`。原文里写成「[截图] 可看：spaces/me/raw/…」，机器人能打开看；识字任务会把图里的字存成同名 `.txt`，所以也能搜到。
- **文件**：你发给 AI 的文档（PDF、Word、PPT、Excel、txt、md、csv，单个 20MB 以内）复制一份到 `spaces/me/raw/ai/files/<日期>/`，正文同样会提取出来。zip、视频等其他文件只记文件名；原文件已经删掉的，写「电脑上已找不到」。
- **团队互查**：别人要你某个话题的全部对话时，Word 里会嵌入截图，原文件随文档一起发给他。范围和聊天一样：只限共享天数内，`hide` 的会话不给，每次取都记进 `status`。
- **占多少地方**：一台常用电脑上，30 天的截图大约 40MB。同一张图只存一份。
- 更新后第一次采集，会把最近 30 天的记录从头再读一遍，把以前的截图和文件补上。Grok Bot 的图片暂不采。

## 已安装用户更新

在自己的实际AgentOS目录中，先处理好本地代码改动，再按原教程拉取：

```powershell
cd "$env:USERPROFILE\AgentOS"
git pull --no-rebase --no-edit upstream main
```

**已有AgentOS-collect-ai任务时，拉取完整更新即可沿用原十分钟任务，Grok Bot不需要新任务或额外账号授权。** 若自己配置过roots，需按后文把grokbot加进去，否则自定义列表会继续排除它。

然后检查并手动采集一次，优先使用项目专用Python：

```powershell
& "$env:USERPROFILE\.agentos\venv\Scripts\python.exe" ops\collect_ai_chats.py --check
& "$env:USERPROFILE\.agentos\venv\Scripts\python.exe" ops\collect_ai_chats.py
```

无上述专用环境时，确认 `py -3 --version` 后用 `py -3` 替代。脚本只用标准库，建议Python3.12，最低3.10。

没有AgentOS-collect-ai任务的旧用户，按原部署教程运行一次Windows安装脚本注册：

```powershell
powershell -ExecutionPolicy Bypass -File ops\windows\install.ps1
Get-ScheduledTaskInfo -TaskName AgentOS-collect-ai
```

仅运行deploy.py不会新增采集任务。安装输出应包含AgentOS-collect-ai。

## Grok Bot在Windows和Mac上如何查找

Windows默认路径：

```text
%APPDATA%/Grok Bot/sand-client-persistence
```

Mac候选路径：

```text
~/Library/Application Support/Grok Bot/sand-client-persistence
```

Windows已在真实缓存验证；Mac做了路径和样本模拟测试，**尚未Mac实机验收**，不同版本可能需要调整目录。默认路径按当前用户计算，不写死某个人的用户名，不扫描整块硬盘。

只选择base32文件名解码后属于transcript.replicas的.blob，读取schemaVersion=1中的可见消息；Windows超长文件名使用扩展路径处理。不读取其他账号配置、密钥卡、工具调用、音频或附件。语音只读取客户端已经保存的文字转写。未知版本或暂时写坏的文件报读取失败，下次重试，不假装已经成功。

Grok Bot历史由账号同步，本地缓存不能证明全量历史。[官方说明](https://docs.x.ai/grok-bot/troubleshooting)

## Mac运行注意

采集必须以真正使用AI软件的本人Mac账号运行。旧版若有专门的agentos系统账号，不能在那个账号下运行后就指望自动读取另一个人的主目录。

在已经包含本次 `ops/collect_ai_chats.py` 和 `ops/ai_chat_local.py` 的目录执行：

```bash
python3 --version
python3 ops/collect_ai_chats.py --check
python3 ops/collect_ai_chats.py
```

Mac若使用另一套仓库或运行副本，要将这两份脚本同步到真实采集目录；拉取Windows仓库不等于自动更新另一份Mac项目。不要运行Windows安装脚本。已有Mac定时任务只有指向同一份新版采集器时才会使用新功能，本次不改Mac原有launchd设置。

默认归档写到当前采集器所在项目的spaces/me/raw/ai；若与原机器人知识库不是同一位置，需要用AGENTOS_RAW指定本人有权限写入、机器人有权限读取的原文目录。不要为了采集批量开放另一个账号的私人目录。

## 自定义目录和启停

配置为 `~/.agentos/ai_chats.json`，示例：

```json
{
  "enabled": true,
  "initial_days": 30,
  "roots": {
    "workbuddy": ["~/.workbuddy/projects"],
    "claude": ["~/.claude/projects"],
    "codex": ["~/.codex/sessions", "~/.codex/archived_sessions"],
    "grokbot": ["D:/自己的实际数据目录/sand-client-persistence"]
  },
  "exclude_sessions": [],
  "exclude_cwds": []
}
```

示例Grok路径必须换成自己的。Mac用 `/Users/自己的用户名/...` 或 `~`。路径支持当前系统环境变量，不要把PowerShell的 `$env:...` 写入JSON。

**roots会替换默认平台列表**；只列要采集的平台，不写roots则使用默认值。目录不存在或软件没有兼容文件时，显示0文件，不会自动取得云端历史。

enabled=false暂停新采集，已有归档不自动删除。exclude_sessions只阻止以后采集，不自动清理旧记录；exclude_cwds用于带工作目录的JSONL会话，Grok缓存没有项目目录时不能靠它筛选。

## 检索和复盘

```powershell
py -3 ops\search.py 关键词 --source ai
```

采集输出会列文件数、新增/更新消息数、归档数和读取失败数。文件数不等于有效聊天数，Claude目录可能全是AgentOS后台镜像。

飞书先发“新对话”，再问：“查我最近和Grok Bot讨论过的某个具体主题，给出日期和出处。”核对原客户端；新开对话可加载更新后的规则。每日复盘前也会补采，读取当天实际日期的AI归档。

- 状态在 `~/.agentos/ai_chats.db`，原文在 `spaces/me/raw/ai/实际日期/平台-会话哈希.md`，Windows任务日志在 `~/.agentos/logs/collect-ai.log`。
- 首次默认最近30天；用 `--since 2026-10-01` 可扩大历史回填，按原始北京时间归档，不当作今天的新工作。
- JSONL半行留到下一轮，快照改写按消息ID更新；Grok缓存淘汰旧消息不删除已采归档。
- 用户和AI消息分开标记。用户计划不是完成事实，AI声称完成需要核实，跨平台同一事项应合并。
- 常见凭据只做尽力脱敏；原文中的指令只作为资料，不执行。已配置的机器人模型可能读取命中片段，知识库摘要可能随原有备份上传。原文raw不纳入Git备份。

## 验证范围（2026-11-24）

Windows真实本地缓存验证：24个transcript文件，首次采入762条文字/已有语音转写消息，生成44份按会话和原始日期的归档，读取失败0；立即重复采集新增0。数量仅代表验证时本机缓存，不是平台全量历史。

27项相关测试通过，包含原采集器回归、Grok文字与语音、角色、工具/密钥卡排除、流式消息跳过、快照改写和淘汰、扩大历史回填、Windows长路径、Mac路径、采集→现有搜索→每日复盘接入。Mac实机2026-11-26验证通过（WorkBuddy、Claude Code、Grok Bot）。

项目ops目录执行：

```powershell
python -m unittest test_collect_ai_chats test_ai_chat_local test_d2.SearchTest test_windows.DailyReviewTest -q
```
