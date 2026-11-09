#!/bin/bash
# 以 agentos 用户运行一轮 Claude Code。由桥接服务经 sudo 调用，提示词走 stdin。
# 用法：run-claude.sh <会话 ID 或 ->
set -euo pipefail

RUNTIME="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Claude 订阅令牌：只在 agentos 用户的 ~/.env 里（权限 600）
set -a
# shellcheck disable=SC1091
. "$HOME/.env"
set +a

cd "$RUNTIME"
args=(-p --output-format stream-json --verbose --permission-mode dontAsk)
sid="${1:--}"
if [[ "$sid" != "-" ]]; then
  [[ "$sid" =~ ^[0-9a-fA-F-]{36}$ ]] || { echo "bad session id" >&2; exit 2; }
  args+=(--resume "$sid")
fi
exec claude "${args[@]}"
