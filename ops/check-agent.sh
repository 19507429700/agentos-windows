#!/bin/bash
# 以你的账号运行：验证 sudo 规则和 Claude 令牌都通了（D1 第 2 步的验收）
set -euo pipefail
RUNTIME=/Users/agentos/agentos

echo "==> 以 agentos 身份调用 Claude（不要求输入密码）"
out="$(echo "只回复 OK" | sudo -n -H -u agentos "$RUNTIME/ops/run-claude.sh" - | tail -1)"
result="$(printf '%s' "$out" | /usr/bin/python3 -c 'import json,sys; e=json.load(sys.stdin); print(e.get("result","")); sys.exit(1 if e.get("is_error") else 0)')" \
  || { echo "失败：$out" >&2; exit 1; }
echo "    Claude 回复：$result"

echo "==> 桥接凭据只有你能读"
for p in "$HOME/.agentos:700" "$HOME/.agentos/bridge.env:600"; do
  path="${p%%:*}" want="${p##*:}"
  [[ -e "$path" ]] || { echo "    $path 不存在，先运行 ops/install-bridge.sh" >&2; exit 1; }
  got="$(stat -f %Lp "$path")" owner="$(stat -f %Su "$path")"
  [[ "$got" == "$want" && "$owner" == "$(id -un)" ]] \
    || { echo "    ⚠️ $path 是 ${owner}/${got}，应为 $(id -un)/${want}" >&2; exit 1; }
done
echo "    OK"
