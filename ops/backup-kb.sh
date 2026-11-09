#!/bin/bash
# 以 agentos 身份运行（ops/push-kb.sh 经 sudo 免密调用，规则见 bootstrap-mac.sh）：
# 把知识库 spaces/me/kb/ 的改动提交到运行副本。只提交 kb/；原文 raw/、草稿 outbox/ 被 .gitignore 排除。
set -euo pipefail
RUNTIME="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RUNTIME"

git add -A -- spaces/me/kb
if git diff --cached --quiet -- spaces/me/kb; then
  echo "知识库没有变化"
  exit 0
fi
git -c user.name=AgentOS -c user.email=agentos@localhost \
  commit -q -m "知识库备份 $(date '+%Y-%m-%d %H:%M')" -- spaces/me/kb
git log --oneline -1
