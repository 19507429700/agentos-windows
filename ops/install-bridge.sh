#!/bin/bash
# 以你的账号运行（不用 sudo）：ops/install-bridge.sh
# 建 ~/.agentos（凭据、队列库、日志、venv），装 systemd 用户服务 agentos-bridge。可重复运行，会重启服务。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$HOME/.agentos"
ENV_FILE="$STATE/bridge.env"
# shellcheck source=ops/systemd.sh
source "$REPO/ops/systemd.sh"

mkdir -p "$STATE/logs"
chmod 700 "$STATE"

if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'EOF'
# 飞书开放平台 → 企业自建应用 → 凭证与基础信息
FEISHU_APP_ID=
FEISHU_APP_SECRET=
# 第一次给机器人发消息，它会回你的 open_id，填到这里后重新运行 install-bridge.sh
OWNER_OPEN_ID=
# 可选：告警和复盘同时推到微信（PushPlus 的令牌）
PUSHPLUS_TOKEN=
# 以下一般不用改
AGENTOS_RUNTIME=/home/agentos/agentos
MAX_CONCURRENCY=2
STUCK_NOTICE_MIN=15
HARD_TIMEOUT_MIN=45
EOF
  echo "已生成 ${ENV_FILE}，先填好 FEISHU_APP_ID / FEISHU_APP_SECRET"
fi
chmod 600 "$ENV_FILE"

echo "==> Python 环境"
[[ -x "$STATE/venv/bin/python" ]] || /usr/bin/python3 -m venv "$STATE/venv"
"$STATE/venv/bin/python" -m pip install -q --upgrade pip
"$STATE/venv/bin/python" -m pip install -q -r "$REPO/ops/bridge/requirements.txt"

if ! grep -qE '^FEISHU_APP_ID=.+' "$ENV_FILE" || ! grep -qE '^FEISHU_APP_SECRET=.+' "$ENV_FILE"; then
  echo "飞书凭据还没填，服务没有启动。填完 $ENV_FILE 后重新运行本脚本。"
  exit 0
fi

echo "==> 飞书机器人服务（systemd）"
install_service bridge -- "$STATE/venv/bin/python" "$REPO/ops/bridge/bridge.py"
sleep 3
systemctl --user --no-pager --lines=0 status agentos-bridge.service | sed -n '1,4p' || true
echo "日志：tail -f $STATE/logs/bridge.log"
