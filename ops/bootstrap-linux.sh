#!/bin/bash
# Linux / Windows（WSL2 里的 Ubuntu）初始化，只跑一次，可重复跑：
#   sudo ops/bootstrap-linux.sh
#   要走代理时（国内连 Claude）：sudo AGENTOS_PROXY=http://127.0.0.1:7890 ops/bootstrap-linux.sh
# 做的事和 Mac 版 bootstrap-mac.sh 一样：
#   1. 建标准用户 agentos，专门跑 Agent（Claude Code 或 CodeBuddy Code）
#   2. 从你的 ~/AgentOS 克隆运行副本到 /home/agentos/agentos
#   3. 建 /home/agentos/.env（Agent 的令牌放这里，权限 600）
#   4. sudo 规则：你的账号只能以 agentos 身份跑 run-claude.sh、git pull 和 backup-kb.sh
#   5. 让你的定时任务在没登录时也能跑（loginctl enable-linger）
#   6. 以 agentos 身份安装 Agent（默认 Claude Code；AGENT_ENGINE=codebuddy 时装 CodeBuddy Code）
#
# 权限分组（和 Mac 不同，Ubuntu 的家目录默认 750，别人进不去）：
#   - agentos 加进你的主组：它能读你的 ~/AgentOS（克隆、pull 运行副本要用）
#   - /home/agentos 和 raw/ 的属组设成你的主组：你能进运行副本读产出、往 raw/ 写采集原文
#   - agentos 的 .env（令牌）只有它自己能读
#   sudo 每次切换都会重新读组，所以不用重新登录就生效。
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "请用 sudo 运行：sudo $0" >&2; exit 1; }
OWNER="${SUDO_USER:?请用 sudo 从你自己的账号运行}"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
OWNER_GROUP="$(id -gn "$OWNER")"
SRC_REPO="$OWNER_HOME/AgentOS"
AGENT=agentos
AGENT_HOME="/home/$AGENT"
RUNTIME="$AGENT_HOME/agentos"
PROXY="${AGENTOS_PROXY:-}"
ENGINE="${AGENT_ENGINE:-claude}"

as_agent() { sudo -u "$AGENT" -H "$@"; }

[[ -d "$SRC_REPO/.git" ]] || { echo "没找到 $SRC_REPO（先把代码克隆到 ~/AgentOS）" >&2; exit 1; }
if ! pidof systemd >/dev/null 2>&1; then
  cat >&2 <<'EOF'
这个系统没有启用 systemd，定时任务和飞书机器人都靠它。WSL 里这样开：
  sudo tee /etc/wsl.conf <<'X'
  [boot]
  systemd=true
  X
然后在 Windows 的 PowerShell 里运行  wsl --shutdown ，再重新打开 Ubuntu，重跑本脚本。
EOF
  exit 1
fi

echo "==> 0/6 系统软件（git、Python、LibreOffice、中文字体、识字工具）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates python3 python3-venv \
  libreoffice-writer libreoffice-calc libreoffice-impress fonts-noto-cjk \
  tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra poppler-utils >/dev/null
echo "    已装好"

echo "==> 1/6 用户 $AGENT"
if id "$AGENT" &>/dev/null; then
  echo "    已存在，跳过"
else
  # 不设密码：只通过 sudo 切换，从不用密码登录
  useradd -m -d "$AGENT_HOME" -s /bin/bash "$AGENT"
fi
usermod -aG "$OWNER_GROUP" "$AGENT"
chgrp "$OWNER_GROUP" "$AGENT_HOME"
chmod 750 "$AGENT_HOME"
# 你的家目录要让 agentos 进得来（只读，靠组权限）；Ubuntu 默认就是 750
chmod g+rx "$OWNER_HOME"

echo "==> 2/6 运行副本 $RUNTIME"
as_agent git config --global --add safe.directory "$SRC_REPO"
as_agent git config --global --add safe.directory "$SRC_REPO/.git"
if [[ -d "$RUNTIME/.git" ]]; then
  as_agent git -C "$RUNTIME" pull --ff-only
else
  as_agent git clone "$SRC_REPO" "$RUNTIME"
fi
as_agent mkdir -p "$RUNTIME/spaces/me/raw" "$RUNTIME/spaces/me/outbox/pending"
# raw/ 由你的账号跑采集脚本写入（凭据在你那边），agentos 读：属组设成你的主组、组可写，setgid 让子目录继承
chgrp "$OWNER_GROUP" "$RUNTIME/spaces/me/raw"
chmod 2770 "$RUNTIME/spaces/me/raw"

echo "==> 3/6 $AGENT_HOME/.env"
if [[ ! -f "$AGENT_HOME/.env" ]]; then
  cat > "$AGENT_HOME/.env" <<EOF
# 用哪个底座：claude（Claude Code，默认）或 codebuddy（腾讯 WorkBuddy 的 CodeBuddy Code）
AGENT_ENGINE=$ENGINE

# ---- 底座 A：Claude 订阅（claude setup-token 打印的一年期令牌）----
CLAUDE_CODE_OAUTH_TOKEN=

# ---- 底座 B：Claude Code + 其他公司的接口（DeepSeek、Kimi、智谱等提供 Anthropic 兼容接口的）----
# 用它时上面的 CLAUDE_CODE_OAUTH_TOKEN 留空。地址和模型名以厂商文档为准。
ANTHROPIC_BASE_URL=
ANTHROPIC_AUTH_TOKEN=
ANTHROPIC_MODEL=
# 后台小任务用的模型（不填就和上面一样）
ANTHROPIC_SMALL_FAST_MODEL=

# Claude Code 不往 Anthropic 发统计、不自动检查更新（用 B 时国内没代理也不会卡在这上面）
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# ---- 底座 C：WorkBuddy（CodeBuddy Code）----
# 中国版密钥在 https://copilot.tencent.com/profile/ 生成；中国版必须同时设 internal
CODEBUDDY_API_KEY=
CODEBUDDY_INTERNET_ENVIRONMENT=internal
EOF
  if [[ -n "$PROXY" ]]; then
    printf '\n# 连外网走的代理；代理软件要一直开着，换了端口就改这里\nHTTPS_PROXY=%s\nHTTP_PROXY=%s\nNO_PROXY=localhost,127.0.0.1\n' \
      "$PROXY" "$PROXY" >> "$AGENT_HOME/.env"
  fi
fi
chown "$AGENT:$AGENT" "$AGENT_HOME/.env"
chmod 600 "$AGENT_HOME/.env"
# 你这边的定时任务（healthchecks 打卡、推 GitHub）也走同一个代理
if [[ -n "$PROXY" ]]; then
  install -d -m 700 -o "$OWNER" -g "$OWNER_GROUP" "$OWNER_HOME/.agentos"
  printf '%s\n' "$PROXY" > "$OWNER_HOME/.agentos/proxy"
  chown "$OWNER:$OWNER_GROUP" "$OWNER_HOME/.agentos/proxy"
  chmod 600 "$OWNER_HOME/.agentos/proxy"
fi

echo "==> 4/6 sudo 规则"
TMP="$(mktemp)"
cat > "$TMP" <<EOF
# AgentOS：桥接服务以 $AGENT 身份调用 Agent；部署时更新运行副本；每天备份时提交知识库
$OWNER ALL=($AGENT) NOPASSWD: $RUNTIME/ops/run-claude.sh, /usr/bin/git -C $RUNTIME pull --ff-only, $RUNTIME/ops/backup-kb.sh
EOF
visudo -cf "$TMP" >/dev/null
install -m 440 -o root -g root "$TMP" /etc/sudoers.d/agentos
rm -f "$TMP"

echo "==> 5/6 没登录时也跑定时任务"
loginctl enable-linger "$OWNER"

echo "==> 6/6 Agent（$AGENT 用户，底座：$ENGINE）"
proxy_env=(env HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY")
# 和 run-claude.sh 用同一个 PATH 找：官方脚本装在 ~/.local/bin，npm 装在 ~/.npm-global/bin
AGENT_PATH="$AGENT_HOME/.local/bin:$AGENT_HOME/.codebuddy/bin:$AGENT_HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
has_cmd() { as_agent env PATH="$AGENT_PATH" bash -c "command -v $1" &>/dev/null; }
npm_hint() {  # 官方安装脚本要连外网；连不上时改用国内镜像经 npm 装
  cat <<EOF
    ⚠️ 没装上（多半是连不上外网）。改用国内镜像装，在你的账号下依次运行：
      sudo apt install -y nodejs npm
      sudo -u $AGENT -H bash -c 'npm config set prefix ~/.npm-global && npm config set registry https://registry.npmmirror.com && npm install -g $1'
    装好后重跑本脚本（会跳过已完成的步骤）。
EOF
}
if [[ "$ENGINE" == "codebuddy" ]]; then
  if has_cmd codebuddy; then
    echo "    已安装 CodeBuddy Code"
  elif ! as_agent "${proxy_env[@]}" bash -c 'cd ~ && curl -fsSL https://www.codebuddy.cn/cli/install.sh | bash'; then
    npm_hint @tencent-ai/codebuddy-code
  fi
else
  if has_cmd claude; then
    echo "    已安装 Claude Code"
  elif ! as_agent "${proxy_env[@]}" bash -c 'cd ~ && curl -fsSL https://claude.ai/install.sh | bash'; then
    npm_hint @anthropic-ai/claude-code
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
fi

cat <<EOF

完成。接下来按 docs/deploy-windows.md 的「填底座的钥匙」一步：
  按文档「第五步」选底座、填钥匙（用 ops/set-key.sh，不用编辑器）
  ~/AgentOS/ops/check-agent.sh   # 验证 Agent 能以 agentos 身份跑通
EOF
