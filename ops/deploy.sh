#!/bin/bash
# 改完 ~/AgentOS 并提交后运行：把提交同步到 agentos 的运行副本，并重启桥接服务。
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -n "$(git -C "$REPO" status --porcelain)" ]]; then
  echo "有未提交的改动，运行副本只拉取已提交的内容：" >&2
  git -C "$REPO" status --short >&2
fi

sudo -n -H -u agentos /usr/bin/git -C /Users/agentos/agentos pull --ff-only
launchctl kickstart -k "gui/$(id -u)/com.agentos.bridge" && echo "桥接服务已重启"
