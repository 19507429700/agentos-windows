#!/bin/bash
# 往配置文件里填一项，不用编辑器：
#   ops/set-key.sh <配置文件> <名字>          粘贴值后回车（屏幕上不显示，适合令牌、密码）
#   ops/set-key.sh <配置文件> <名字> <值>     直接写（适合不保密的，比如 AGENT_ENGINE codebuddy）
# 这一行已经有了就替换，没有就加在最后。最后显示「已填好」或「没填上」，不会把值打印出来。
set -euo pipefail

file="${1:?用法：set-key.sh <配置文件> <名字> [值]}"
name="${2:?用法：set-key.sh <配置文件> <名字> [值]}"
[[ "$name" =~ ^[A-Z_][A-Z0-9_]*$ ]] || { echo "名字只能是大写字母、数字、下划线：$name" >&2; exit 2; }
[[ -f "$file" ]] || { echo "没有这个文件：$file（先运行生成它的那一步）" >&2; exit 1; }

if [[ $# -ge 3 ]]; then
  value="$3"
else
  read -rs -p "粘贴 $name 的值，然后按回车（屏幕上不显示，是正常的）：" value
  echo
fi
value="${value//$'\r'/}"   # 从 Windows 复制过来可能带回车符
[[ "$value" != *$'\n'* ]] || { echo "值里不能有换行" >&2; exit 2; }

tmp="$(mktemp "$(dirname "$file")/.set-key.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
NAME="$name" VALUE="$value" awk '
  BEGIN { n = ENVIRON["NAME"]; v = ENVIRON["VALUE"]; done = 0 }
  index($0, n "=") == 1 { if (!done) print n "=" v; done = 1; next }
  { print }
  END { if (!done) print n "=" v }
' "$file" > "$tmp"
chmod --reference="$file" "$tmp" 2>/dev/null || chmod 600 "$tmp"
mv "$tmp" "$file"
trap - EXIT
unset value

if grep -qE "^${name}=.+" "$file"; then echo "✅ $name 已填好"; else echo "⚠️ $name 没填上（值是空的）"; fi
