# 定时任务和常驻服务（Linux / WSL 用 systemd 的「用户服务」，对应 Mac 版的 launchd）。
# 注意：路径里不要有空格（systemd 的命令行按空格切分）。
# 被 install-bridge.sh、install-collectors.sh、deploy.sh 引用：source "$REPO/ops/systemd.sh"
# 装在 ~/.config/systemd/user/，以你的账号运行；bootstrap-linux.sh 开了 linger，没登录也会跑。
# 看状态：systemctl --user list-timers 'agentos-*'   看日志：~/.agentos/logs/<任务名>.log

UNIT_DIR="$HOME/.config/systemd/user"

_unit_common() {  # 每个服务都要的环境：PATH（Agent、Python 在哪）、日志位置
  local name="$1"
  cat <<EOF
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
StandardOutput=append:$HOME/.agentos/logs/$name.log
StandardError=append:$HOME/.agentos/logs/$name.log
EOF
}

# 定时任务：install_timer <名字> <超时分钟> <何时跑> -- <命令…>
#   何时跑：every10 = 每 10 分钟（开机 2 分钟后第一次）；或 systemd 日历写法，如 "*-*-* 23:00:00"
#   命令经 run-job.sh 包一层：失败/超时推飞书和微信，成功去 healthchecks 打卡（和 Mac 版一样）
install_timer() {
  local name="$1" timeout="$2" when="$3"; shift 3
  [[ "${1:-}" == "--" ]] && shift
  local unit="agentos-$name"
  mkdir -p "$UNIT_DIR" "$HOME/.agentos/logs"
  {
    printf '[Unit]\nDescription=AgentOS %s\n\n[Service]\nType=oneshot\n' "$name"
    _unit_common "$name"
    printf 'ExecStart=/bin/bash %s/ops/run-job.sh %s %s --' "$REPO" "$name" "$timeout"
    printf ' %s' "$@"
    printf '\n'
  } > "$UNIT_DIR/$unit.service"
  {
    printf '[Unit]\nDescription=AgentOS %s 定时\n\n[Timer]\n' "$name"
    if [[ "$when" == "every10" ]]; then
      printf 'OnBootSec=2min\nOnUnitActiveSec=10min\n'
    else
      # Persistent：电脑关着或睡着错过了，开机后补跑一次（和 launchd 一样）
      printf 'OnCalendar=%s\nPersistent=true\n' "$when"
    fi
    printf '\n[Install]\nWantedBy=timers.target\n'
  } > "$UNIT_DIR/$unit.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now "$unit.timer" >/dev/null
}

# 常驻服务（飞书机器人）：install_service <名字> -- <命令…>；崩溃了 10 秒后自动重启
install_service() {
  local name="$1"; shift
  [[ "${1:-}" == "--" ]] && shift
  local unit="agentos-$name"
  mkdir -p "$UNIT_DIR" "$HOME/.agentos/logs"
  {
    printf '[Unit]\nDescription=AgentOS %s\nAfter=network-online.target\n\n[Service]\nType=simple\n' "$name"
    _unit_common "$name"
    printf 'ExecStart='
    printf '%s ' "$@"
    printf '\nRestart=always\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n'
  } > "$UNIT_DIR/$unit.service"
  systemctl --user daemon-reload
  systemctl --user enable "$unit.service" >/dev/null
  systemctl --user restart "$unit.service"
}
