#!/bin/bash
# Mac 初始化（只跑一次，可重复跑）：sudo ops/bootstrap-mac.sh
#   1. 建隐藏的标准用户 agentos，专门跑 Claude
#   2. 从你的 ~/AgentOS 克隆运行副本到 /Users/agentos/agentos
#   3. 建 /Users/agentos/.env（Claude 令牌放这里，权限 600）
#   4. sudo 规则：你的账号只能以 agentos 身份跑 run-claude.sh、git pull 和 backup-kb.sh
#   5. 以 agentos 身份安装 Claude Code（官方脚本）
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "请用 sudo 运行：sudo $0" >&2; exit 1; }
OWNER="${SUDO_USER:?请用 sudo 从你自己的账号运行}"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
SRC_REPO="$OWNER_HOME/AgentOS"
AGENT=agentos
AGENT_HOME="/Users/$AGENT"
RUNTIME="$AGENT_HOME/agentos"

as_agent() { sudo -u "$AGENT" -H "$@"; }

# 系统设置里的 HTTPS 代理（国内连 Claude 要用）；命令行程序不会自动走系统代理，要显式传
PROXY=""
if [[ "$(scutil --proxy | awk '/HTTPSEnable/ {print $3}')" == "1" ]]; then
  PROXY="http://$(scutil --proxy | awk '/HTTPSProxy/ {print $3}'):$(scutil --proxy | awk '/HTTPSPort/ {print $3}')"
  echo "==> 检测到系统代理 $PROXY"
fi

echo "==> 1/5 用户 $AGENT"
if id "$AGENT" &>/dev/null; then
  echo "    已存在，跳过"
else
  # 随机密码，不保存：只通过 sudo 切换，从不用密码登录
  sysadminctl -addUser "$AGENT" -fullName "AgentOS Agent" \
    -password "$(openssl rand -base64 24)" -home "$AGENT_HOME"
  createhomedir -c -u "$AGENT" >/dev/null
  dscl . -create "/Users/$AGENT" IsHidden 1
fi
# 家目录 750：你的账号（staff 组）能读 outbox 等产出，其他人进不来
chmod 750 "$AGENT_HOME"

echo "==> 2/5 运行副本 $RUNTIME"
git -C "$SRC_REPO" rev-parse HEAD &>/dev/null || { echo "    $SRC_REPO 还没有提交，先 git commit" >&2; exit 1; }
as_agent git config --global --add safe.directory "$SRC_REPO"
as_agent git config --global --add safe.directory "$SRC_REPO/.git"
if [[ -d "$RUNTIME/.git" ]]; then
  # 运行副本跟着你的仓库当前所在分支走（比如从 d2-memory 换到 main）；只快进，不丢运行副本里未提交的知识库
  BRANCH="$(git -C "$SRC_REPO" rev-parse --abbrev-ref HEAD)"
  as_agent git -C "$RUNTIME" fetch -q origin
  if [[ "$(as_agent git -C "$RUNTIME" rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]]; then
    as_agent git -C "$RUNTIME" merge-base --is-ancestor HEAD "origin/$BRANCH" \
      || { echo "    运行副本有 $BRANCH 之外的提交，不能直接切换，请先把它们拉回 $SRC_REPO" >&2; exit 1; }
    as_agent git -C "$RUNTIME" checkout -q -B "$BRANCH" --track "origin/$BRANCH"
    echo "    运行副本已切到分支 $BRANCH"
  fi
  as_agent git -C "$RUNTIME" pull --ff-only
else
  as_agent git clone "$SRC_REPO" "$RUNTIME"
fi
as_agent mkdir -p "$RUNTIME/spaces/me/raw" "$RUNTIME/spaces/me/outbox/pending"
# raw/ 由你的账号跑采集脚本写入（凭据在你那边），agentos 读：组 staff 可写，setgid 让子目录继承组
chgrp staff "$RUNTIME/spaces/me/raw"
chmod 2770 "$RUNTIME/spaces/me/raw"

echo "==> 3/5 $AGENT_HOME/.env"
if [[ ! -f "$AGENT_HOME/.env" ]]; then
  {
    printf '# claude setup-token 打印的令牌\nCLAUDE_CODE_OAUTH_TOKEN=\n'
    if [[ -n "$PROXY" ]]; then
      printf '\n# 连 Claude 走的代理（来自系统设置）；代理软件要一直开着，换了端口就改这里\n'
      printf 'HTTPS_PROXY=%s\nHTTP_PROXY=%s\nNO_PROXY=localhost,127.0.0.1\n' "$PROXY" "$PROXY"
    fi
  } > "$AGENT_HOME/.env"
fi
chown "$AGENT":staff "$AGENT_HOME/.env"
chmod 600 "$AGENT_HOME/.env"

echo "==> 4/5 sudo 规则"
TMP="$(mktemp)"
cat > "$TMP" <<EOF
# AgentOS：桥接服务以 $AGENT 身份调用 Claude；部署时更新运行副本；每天备份时提交知识库
$OWNER ALL=($AGENT) NOPASSWD: $RUNTIME/ops/run-claude.sh, /usr/bin/git -C $RUNTIME pull --ff-only, $RUNTIME/ops/backup-kb.sh
EOF
visudo -cf "$TMP" >/dev/null
install -m 440 -o root -g wheel "$TMP" /etc/sudoers.d/agentos
rm -f "$TMP"

echo "==> 5/5 Claude Code（$AGENT 用户）"
if [[ -x "$AGENT_HOME/.local/bin/claude" ]]; then
  echo "    已安装：$(as_agent "$AGENT_HOME/.local/bin/claude" --version)"
else
  as_agent env HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" bash -c 'cd ~ && curl -fsSL https://claude.ai/install.sh | bash'
fi

# 无界面模式没人点「信任此目录」，不标记的话 .claude/settings.json 的权限会被忽略，Agent 读写不了文件
as_agent /usr/bin/python3 - "$RUNTIME" <<'PY'
import json, os, sys
p = os.path.expanduser("~/.claude.json")
d = json.load(open(p)) if os.path.exists(p) else {}
d.setdefault("projects", {}).setdefault(sys.argv[1], {})["hasTrustDialogAccepted"] = True
json.dump(d, open(p, "w"), indent=2)
PY
echo "    已信任工作目录 $RUNTIME"

if grep -qE '^CLAUDE_CODE_OAUTH_TOKEN=.+' "$AGENT_HOME/.env"; then
  echo; echo "完成。Claude 令牌已填好，不用再生成。"
else
  cat <<EOF

完成。接下来（详见 README 的 D1 清单）：
  1. sudo -u $AGENT -i            # 切到 agentos
     ~/.local/bin/claude setup-token   # 浏览器授权，打印一年期令牌
     nano ~/.env                    # 填 CLAUDE_CODE_OAUTH_TOKEN=...
     exit
  2. ops/check-agent.sh            # 验证 Claude 能以 agentos 身份跑通
EOF
fi
