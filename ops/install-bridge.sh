#!/bin/bash
# 以你的账号运行（不用 sudo）：ops/install-bridge.sh
# 建 ~/.agentos（凭据、队列库、日志、venv），装 launchd 服务。可重复运行，会重启服务。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$HOME/.agentos"
ENV_FILE="$STATE/bridge.env"
PLIST="$HOME/Library/LaunchAgents/com.agentos.bridge.plist"
LABEL="gui/$(id -u)/com.agentos.bridge"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"

mkdir -p "$STATE/logs"
chmod 700 "$STATE"

if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'EOF'
# 飞书开放平台 → 企业自建应用 → 凭证与基础信息
FEISHU_APP_ID=
FEISHU_APP_SECRET=
# 第一次给机器人发消息，它会回你的 open_id，填到这里后重新运行 install-bridge.sh
OWNER_OPEN_ID=
# 以下一般不用改
AGENTOS_RUNTIME=/Users/agentos/agentos
MAX_CONCURRENCY=2
STUCK_NOTICE_MIN=15
HARD_TIMEOUT_MIN=45
EOF
  echo "已生成 ${ENV_FILE}，先填好 FEISHU_APP_ID / FEISHU_APP_SECRET"
fi
chmod 600 "$ENV_FILE"

echo "==> Python 环境"
[[ -x "$STATE/venv/bin/python" ]] || "$UV" venv --python 3.12 "$STATE/venv"
VIRTUAL_ENV="$STATE/venv" "$UV" pip install -q -r "$REPO/ops/bridge/requirements.txt"

echo "==> launchd"
mkdir -p "$(dirname "$PLIST")"
sed -e "s#__HOME__#$HOME#g" -e "s#__REPO__#$REPO#g" "$REPO/ops/launchd/com.agentos.bridge.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null

if ! grep -qE '^FEISHU_APP_ID=.+' "$ENV_FILE" || ! grep -qE '^FEISHU_APP_SECRET=.+' "$ENV_FILE"; then
  echo "飞书凭据还没填，服务已装好但未启动。填完 $ENV_FILE 后重新运行本脚本。"
  exit 0
fi

launchctl bootout "$LABEL" 2>/dev/null || true
# bootout 是异步的，旧服务没退干净就 bootstrap 会报 Input/output error
for _ in {1..20}; do launchctl print "$LABEL" &>/dev/null || break; sleep 0.5; done
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "$LABEL"
sleep 3
launchctl print "$LABEL" | grep -E '^\s+(state|pid) =' || true
echo "日志：tail -f $STATE/logs/bridge.log"
