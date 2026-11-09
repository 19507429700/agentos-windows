#!/bin/bash
# 以你的账号运行（systemd 定时器每天 03:00；也可手动）：ops/push-kb.sh
# 知识库备份到 GitHub：知识库是 Agent 在运行副本里写的，所以按 副本 → 本仓库 → GitHub 的方向走，
# 三处始终是同一串提交，deploy.sh 不会冲突。
#   1. 运行副本先快进到本仓库的最新代码（免密规则里已有的 git pull）
#   2. 以 agentos 身份提交 spaces/me/kb/（免密规则里的 backup-kb.sh）
#   3. 本仓库从运行副本快进拉回这次提交
#   4. 推到 GitHub（有 ~/.agentos/proxy 就走代理）
#   5. 推到 Gitee（配了 gitee 远程才推）
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME=/home/agentos/agentos
BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"

echo "==> $(date '+%F %T') 备份知识库（分支 ${BRANCH}）"
sudo -n -H -u agentos /usr/bin/git -C "$RUNTIME" pull --ff-only
sudo -n -H -u agentos "$RUNTIME/ops/backup-kb.sh"
git -C "$REPO" -c safe.directory="$RUNTIME" pull --ff-only -q "$RUNTIME" "$BRANCH"

PROXY=""
[[ -s "$HOME/.agentos/proxy" ]] && PROXY="$(head -1 "$HOME/.agentos/proxy")"  # bootstrap-linux.sh 写的；没有就直连
git -C "$REPO" ${PROXY:+-c http.proxy="$PROXY"} push -q origin "$BRANCH"
echo "    已推到 GitHub：$(git -C "$REPO" log --oneline -1)"

# 5. 同步到 Gitee（国内直连，不走代理；失败只提示，不影响 GitHub 备份）
if git -C "$REPO" remote get-url gitee >/dev/null 2>&1; then
  if GIT_TERMINAL_PROMPT=0 git -C "$REPO" push -q gitee "$BRANCH"; then
    echo "    已推到 Gitee"
  else
    echo "    !! 推送 Gitee 失败（凭据失效？手动跑一次 git push gitee ${BRANCH} 重新登录）" >&2
  fi
fi
