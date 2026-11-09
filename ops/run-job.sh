#!/bin/bash
# 定时任务统一包装（launchd 调用）：ops/run-job.sh <任务名> <超时分钟> -- <命令…>
#   成功：去 healthchecks.io 打卡（~/.agentos/healthchecks.env 里配了这个任务的地址才打）
#   失败/超时：推飞书 + 微信（哪个任务、退出码、日志最后 20 行）；不打卡
#   分工：出错由这里推飞书/微信；Mac 关机/睡着/断网导致没打卡，由 healthchecks 发邮件，两边不重复
#   同一任务连续失败只在第一次告警，恢复时再通知一次，避免每 10 分钟刷屏
# 任务自己的输出照常写进 launchd 日志。
set -uo pipefail

NAME="$1"; TIMEOUT_MIN="$2"; shift 2
[[ "${1:-}" == "--" ]] && shift
REPO="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$HOME/.agentos"
STATUS_FILE="$STATE/jobs/${NAME}.status"
mkdir -p "$STATE/jobs"

OUT="$(mktemp)"
trap 'rm -f "$OUT" "$OUT.timeout"' EXIT

# ---------- 跑任务（带超时）----------
"$@" >"$OUT" 2>&1 &
pid=$!
# 看门狗每秒看一眼：任务结束它就自己退出（不能用一个长 sleep，否则任务结束后它还挂着，launchd 会当任务没跑完）
( end=$((SECONDS + TIMEOUT_MIN * 60))
  while kill -0 "$pid" 2>/dev/null; do
    if (( SECONDS >= end )); then
      touch "$OUT.timeout"; kill -TERM "$pid" 2>/dev/null
      for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -KILL "$pid" 2>/dev/null; break
    fi
    sleep 1
  done ) >/dev/null 2>&1 &
watcher=$!
wait "$pid"; code=$?
wait "$watcher" 2>/dev/null
cat "$OUT"

# ---------- healthchecks 打卡 ----------
HC_URL=""
if [[ -f "$STATE/healthchecks.env" ]]; then
  key="HC_$(echo "$NAME" | tr 'a-z-' 'A-Z_')"
  HC_URL="$(grep -E "^${key}=" "$STATE/healthchecks.env" | head -1 | cut -d= -f2-)"
fi
hc_ping() {  # 成功打卡：只访问地址、不带任何内容（日志里有群名人名，不往国外服务发）；走系统代理
  [[ -n "$HC_URL" ]] || return 0
  local proxy=""
  if [[ "$(scutil --proxy | awk '/HTTPSEnable/ {print $3}')" == "1" ]]; then
    proxy="http://$(scutil --proxy | awk '/HTTPSProxy/ {print $3}'):$(scutil --proxy | awk '/HTTPSPort/ {print $3}')"
  fi
  curl -fsS -m 15 --retry 2 ${proxy:+-x "$proxy"} -o /dev/null "$HC_URL" \
    || echo "（healthchecks 打卡失败，网络或地址有问题）"
}

prev="$(cat "$STATUS_FILE" 2>/dev/null || echo ok)"
if [[ $code -eq 0 ]]; then
  hc_ping
  echo ok > "$STATUS_FILE"
  if [[ "$prev" == "fail" ]]; then
    /usr/bin/python3 "$REPO/ops/notify.py" "✅ AgentOS 已恢复：${NAME}" "$(date '+%F %T') 这个任务又能正常运行了。" >/dev/null
  fi
else
  echo fail > "$STATUS_FILE"
  if [[ "$prev" != "fail" ]]; then
    why="退出码 ${code}"
    [[ -f "$OUT.timeout" ]] && why="超时（超过 ${TIMEOUT_MIN} 分钟被中止）"
    body="$(printf '%s\n时间：%s\n恢复前不再重复提醒。\n\n日志最后 20 行：\n%s' "$why" "$(date '+%F %T')" "$(tail -n 20 "$OUT")")"
    /usr/bin/python3 "$REPO/ops/notify.py" "⚠️ AgentOS 任务失败：${NAME}" "$body" \
      || echo "（告警也没发出去，检查 bridge.env 和网络）"
  fi
fi
exit $code
