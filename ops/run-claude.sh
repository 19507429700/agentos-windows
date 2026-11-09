#!/bin/bash
# 以 agentos 用户运行一轮 Agent。由桥接服务、每日复盘经 sudo 调用，提示词走 stdin。
# 用法：run-claude.sh <会话 ID 或 ->
#
# 底座在 agentos 的 ~/.env 里选（权限 600，只有 agentos 能读）：
#   AGENT_ENGINE=claude     Claude Code（默认）。钥匙二选一：
#                             A. CLAUDE_CODE_OAUTH_TOKEN：Claude 订阅的一年期令牌
#                             B. ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN（+ ANTHROPIC_MODEL）：
#                                其他公司提供的 Anthropic 兼容接口（DeepSeek、Kimi、智谱等）
#   AGENT_ENGINE=codebuddy  腾讯 WorkBuddy 的 CodeBuddy Code。钥匙：CODEBUDDY_API_KEY + CODEBUDDY_INTERNET_ENVIRONMENT
# 不管哪个底座，输出都是 stream-json，最后一条 type=result；桥接和复盘按同一种格式读。
set -euo pipefail

RUNTIME="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:$HOME/.codebuddy/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

set -a
# shellcheck disable=SC1091
. "$HOME/.env"
set +a
# .env 模板里三种底座的变量都列着，没填的是空值；空值不传给 Agent，免得被当成「改了地址」「换了钥匙」
for v in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL \
         CODEBUDDY_API_KEY CODEBUDDY_INTERNET_ENVIRONMENT HTTPS_PROXY HTTP_PROXY NO_PROXY; do
  [[ -n "${!v:-}" ]] || unset "$v"
done

cd "$RUNTIME"
resume=()
sid="${1:--}"
if [[ "$sid" != "-" ]]; then
  [[ "$sid" =~ ^[0-9a-fA-F-]{36}$ ]] || { echo "bad session id" >&2; exit 2; }
  resume=(--resume "$sid")
fi

case "${AGENT_ENGINE:-claude}" in
  claude)
    # 规则 CLAUDE.md、权限 .claude/settings.json、技能 .claude/skills/
    exec claude -p --output-format stream-json --verbose --permission-mode dontAsk "${resume[@]}"
    ;;
  codebuddy)
    # 规则 CODEBUDDY.md、权限 .codebuddy/settings.json、技能 .codebuddy/skills/（都是指向 .claude 那几份的链接，内容同一份）
    # 注意：CodeBuddy 文档说无界面模式要加 -y（跳过所有权限检查）。这里故意不加：加了就等于拆掉
    # 「Agent 只能跑那几条命令、读不到令牌」的防线。只用 dontAsk 跑不通的话，要另想办法，不能直接放开。
    exec codebuddy -p --output-format stream-json --verbose --permission-mode dontAsk "${resume[@]}"
    ;;
  *)
    echo "AGENT_ENGINE 只能是 claude 或 codebuddy，现在是「${AGENT_ENGINE}」（在 agentos 的 ~/.env 里改）" >&2
    exit 2
    ;;
esac
