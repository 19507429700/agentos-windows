#!/bin/bash
# 每日复盘（launchd 每天 23:00 经 run-job.sh 调用；也可手动试跑）：ops/daily-review.sh [YYYY-MM-DD]
#   1. 补采一遍邮箱和飞书（对应计划书的 22:55 补采）
#   2. 以 agentos 身份让 Agent 按 daily-review 技能写复盘：全文进 kb/daily/<日期>.md，回复一页摘要
#   3. 摘要推到你的飞书 + 微信
# 以你的账号运行：凭据在你这边，Agent 只拿到原文文件清单。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME=/Users/agentos/agentos
DAY="${1:-$(date +%F)}"
[[ "$DAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "日期格式应为 YYYY-MM-DD" >&2; exit 2; }
YESTERDAY="$(date -j -v-1d -f %F "$DAY" +%F)"
RAW="$RUNTIME/spaces/me/raw"

echo "==> $(date '+%F %T') 每日复盘 ${DAY}"
missing=()
/usr/bin/python3 "$REPO/ops/collect_mail.py" || missing+=("邮箱")
/usr/bin/python3 "$REPO/ops/collect_feishu.py" || missing+=("飞书")
# 微信（本机、个人自用）：用专用 Python（只有它有读微信目录的权限，见 install-collectors.sh）；没建就跳过
WXPY="$HOME/.agentos/wechat-python/bin/python3.14"
[[ -x "$WXPY" ]] && { "$WXPY" "$REPO/ops/collect_wechat.py" || missing+=("微信"); }

all="$(cd "$RAW" 2>/dev/null && find . -path "./*/${DAY}/*" -type f -name '*.md' | sed 's|^\./|spaces/me/raw/|' | sort)"
# 当天往来（聊天、邮件、收件箱）是复盘的主体；当天新存的飞书文档、妙记只是参考，相关时再读
files="$(printf '%s\n' "$all" | grep -Ev '/(文档|妙记)-' | grep . || true)"
docs="$(printf '%s\n' "$all" | grep -E '/(文档|妙记)-' | grep . || true)"
count="$(printf '%s' "$files" | grep -c . || true)"
dcount="$(printf '%s' "$docs" | grep -c . || true)"
echo "    当天往来 ${count} 个文件，新存文档和妙记 ${dcount} 篇"

prompt="按技能 daily-review 写 ${DAY} 的每日复盘。
昨天是 ${YESTERDAY}（有 spaces/me/kb/daily/${YESTERDAY}.md 就读它的「明天三件事」）。
当天往来原文（聊天、邮件、收件箱，共 ${count} 个，这是复盘的主体）：
${files:-（无）}
当天新存或有更新的飞书文档和妙记（共 ${dcount} 篇；它们多半是以前就有的，只是今天第一次读到，别当成今天发生的事，和当天往来相关时再去读）：
${docs:-（无）}
日程（今天起 7 天，排「明天三件事」时参考）：spaces/me/raw/feishu/日程/最近日程.md
没采到的来源：${missing[*]:-无}
全文写进 spaces/me/kb/daily/${DAY}.md，最后只回复那一页摘要。"

# Claude 出错时会以非零退出码结束，错误写在它输出的 result 里；不能让 set -e 直接退出，
# 否则原因被吞掉，手动跑时屏幕上什么都没有，23:00 的告警里也看不到原因。
set +e
out="$(printf '%s' "$prompt" | sudo -n -H -u agentos "$RUNTIME/ops/run-claude.sh" -)"
rc=$?
set -e
summary="$(printf '%s\n' "$out" | RC="$rc" /usr/bin/python3 -c '
import json, os, sys
res, lines = None, []
for line in sys.stdin:
    if line.strip():
        lines.append(line.strip())
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("type") == "result":
        res = ev
rc = os.environ.get("RC", "?")
if res and not res.get("is_error") and (res.get("result") or "").strip():
    print(res["result"].strip())
    sys.exit(0)
if res:
    why = (res.get("result") or "").strip() or json.dumps(res, ensure_ascii=False)
    kind = res.get("subtype") or ""
    sys.exit(f"❌ Agent 没有正常写完复盘（退出码 {rc}，{kind}）：{why[:500]}")
tail = "\n".join(l[:300] for l in lines[-5:]) or "（没有任何输出）"
sys.exit(f"❌ Agent 没有输出结果（退出码 {rc}）。最后几行：\n{tail}")
')"

[[ -f "$RUNTIME/spaces/me/kb/daily/${DAY}.md" ]] || echo "    ⚠️ 没找到 kb/daily/${DAY}.md，Agent 可能没写全文"
/usr/bin/python3 "$REPO/ops/notify.py" "📋 每日复盘 ${DAY}" "$summary"
echo "    已推送"
