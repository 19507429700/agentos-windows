---
name: wechat-key
description: 给本机 Windows 微信 4.x 提取数据库密钥（微信 4.1+ 的 Config.Cipher 运行时扫描），一键生成 all_keys.json，让采集脚本能读聊天记录。用户说「取微信密钥」「微信读不了聊天」「要读微信先配钥匙」时用。首次需要微信运行中+管理员权限，之后离线可读。
---

# 微信密钥提取

## 它解决什么问题

微信 4.x 的每个聊天库都是 SQLCipher 加密的，读之前必须先拿到密钥。微信 4.1.12+ 内存里**不再缓存明文密钥**，老方法（内存里搜 `x'...'` 明文）必然 0 命中。本 skill 用 4.1+ 的 `Config.Cipher` 运行时扫描，一步取出全部数据库密钥。

取完密钥后，交给项目里现成的 `ops/collect_wechat.py`（聊天采集）、`ops/wechat_articles.py`（公众号文章）、`ops/extract_text.py`（文件/图片文字）继续干活。

## 首次取密钥（每台电脑一次）

**前提**：微信 4.x 已安装、已登录、正在运行；终端**以管理员身份**打开。

```bash
py .claude/skills/wechat-key/scripts/get_key.py
```

自动定位微信数据目录 → 提取密钥 → 写到 `%USERPROFILE%\.wechat-cli\all_keys.json`。

成功标志：`24/24 salts 找到密钥`（数字 = 数据库个数）。失败按顺序查：

1. 微信没运行 / 没登录 → 启动登录后重跑。
2. 没以管理员身份运行 → 右键终端「以管理员身份运行」。
3. 微信版本不是 4.x → 本 skill 只支持 4.0+（进程名 `Weixin.exe`）。

## 验证

```bash
py ops/collect_wechat.py --check
```

能列出会话即说明钥匙对了。之后读消息**不需要微信运行、不需要重复取密钥**。微信大版本更新后密钥可能失效，重跑一次 `get_key.py` 即可。

## 原理（排障时才需要懂）

内存里没有明文密钥，只有 `com.Tencent.WCDB.Config.Cipher` 对象。工具定位该字符串对象 → 沿对象链取 blob → 用固定掩码 XOR 解码出候选 → 对每个库的第一页算 HMAC-SHA512，和库自己存的 HMAC 比对，对得上才算数（保证不抓错）。

## 规则

- 只读不改、只在本机处理，不上传任何数据。
- 密钥文件含敏感信息，不要提交进 Git、不要发给任何人。
