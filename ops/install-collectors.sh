#!/bin/bash
# 以你的账号运行（不用 sudo）：ops/install-collectors.sh
# 装定时任务（systemd 用户定时器）：邮箱采集、飞书采集、提取文字（每 10 分钟），
# 每日复盘（每天 23:00），知识库备份到 GitHub（每天 03:00）。
# 各自满足前提才装（填好 mail.env / 运行过 feishu_auth.py / 绑定了本人 / 免密规则里有 backup-kb.sh）。可重复运行。
# Windows 版暂不含微信采集和公众号抓取（Windows 版微信的解密方法不同，还没做）。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$HOME/.agentos"
ENV_FILE="$STATE/mail.env"
PY=/usr/bin/python3
# shellcheck source=ops/systemd.sh
source "$REPO/ops/systemd.sh"

mkdir -p "$STATE/logs"
chmod 700 "$STATE"

# ---------- 邮箱 ----------
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'EOF'
# 邮箱 IMAP 只读采集。授权码在邮箱网页版「设置」里开启 IMAP 后生成，不是登录密码
# QQ 邮箱 imap.qq.com ｜ 163 邮箱 imap.163.com ｜ 腾讯企业邮 imap.exmail.qq.com
MAIL_IMAP_HOST=
MAIL_USER=
MAIL_PASSWORD=
# 多个文件夹用逗号隔开；第一次拉最近几天
MAIL_FOLDERS=INBOX
MAIL_INITIAL_DAYS=7
EOF
  echo "已生成 ${ENV_FILE}，填好 MAIL_IMAP_HOST / MAIL_USER / MAIL_PASSWORD 后重新运行本脚本"
fi
chmod 600 "$ENV_FILE"

if grep -qE '^MAIL_PASSWORD=.+' "$ENV_FILE"; then
  echo "==> 邮箱：测试登录"
  $PY "$REPO/ops/collect_mail.py" --check
  install_timer collect-mail 8 every10 -- $PY "$REPO/ops/collect_mail.py"
  echo "==> 邮箱：先拉一次"
  $PY "$REPO/ops/collect_mail.py" || echo "    （这次拉取有报错，见上面；定时任务已装好，下次会重试）"
  echo "    之后每 10 分钟自动拉。日志：$STATE/logs/collect-mail.log"
else
  echo "==> 邮箱还没填，跳过"
fi

# ---------- 飞书 ----------
if [[ -f "$STATE/feishu_user_token.json" ]]; then
  install_timer collect-feishu 8 every10 -- $PY "$REPO/ops/collect_feishu.py"
  echo "==> 飞书：先拉一次"
  $PY "$REPO/ops/collect_feishu.py" || echo "    （这次拉取有报错，见上面；定时任务已装好，下次会重试）"
  echo "    之后每 10 分钟自动拉。日志：$STATE/logs/collect-feishu.log"
else
  echo "==> 飞书还没以你本人身份授权，跳过（先运行：python3 $REPO/ops/feishu_auth.py）"
fi

# ---------- 提取文字（图片识字、PDF / Word / PPT 正文）----------
# Linux 上用 tesseract（识字）、poppler（读 PDF）、LibreOffice（老 Word），bootstrap-linux.sh 已装好。
install_timer extract-text 8 every10 -- $PY "$REPO/ops/extract_text.py"
echo "==> 提取文字：每 10 分钟一轮（每轮最多 4 分钟），结果存成原文件旁边的 .txt。日志：$STATE/logs/extract-text.log"

# ---------- 每日复盘 ----------
if grep -qE '^OWNER_OPEN_ID=.+' "$STATE/bridge.env" 2>/dev/null; then
  install_timer daily-review 40 "*-*-* 23:00:00" -- /bin/bash "$REPO/ops/daily-review.sh"
  echo "==> 每日复盘：每天 23:00 写复盘并推到飞书。日志：$STATE/logs/daily-review.log（手动试跑：$REPO/ops/daily-review.sh）"
else
  echo "==> 每日复盘跳过：bridge.env 里还没有 OWNER_OPEN_ID（先完成绑定本人）"
fi

# ---------- 知识库备份 ----------
if sudo -n -l 2>/dev/null | grep -q "ops/backup-kb.sh"; then
  install_timer push-kb 15 "*-*-* 03:00:00" -- /bin/bash "$REPO/ops/push-kb.sh"
  echo "==> 知识库：每天 03:00 备份到 GitHub。日志：$STATE/logs/push-kb.log（手动备份：$REPO/ops/push-kb.sh）"
else
  echo "==> 知识库备份跳过：免密规则里还没有 backup-kb.sh，先重跑一次 sudo $REPO/ops/bootstrap-linux.sh"
fi

echo
echo "全部定时任务：systemctl --user list-timers 'agentos-*'"
