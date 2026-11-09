"""定时任务统一包装（Windows 任务计划程序调用）：python ops/run_job.py <任务名> <超时分钟> -- <命令…>

- 任务的输出追加写进 %USERPROFILE%\\.agentos\\logs\\<任务名>.log（任务计划程序不会替你记日志）
- 超时：连同子进程一起结束
- 成功：去 healthchecks.io 打卡（healthchecks.env 里配了 HC_<任务名大写> 才打；只访问地址，不带任何内容）
- 失败或超时：推飞书 + 微信（哪个任务、退出码、日志最后 20 行）；连续失败只在第一次告警，恢复时再通知一次
- 分工：出错由这里推；电脑关机、睡着、断网导致没打卡，由 healthchecks 发邮件，两边不重复
命令里的 python 用当前这个 Python（venv 里的）。只用标准库。
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import LOGS, NO_WINDOW, REPO, STATE, child_env, kill_tree, proxy, python, read_env  # noqa: E402

TAIL_LINES = 20


def hc_url(name: str) -> str:
    key = "HC_" + name.upper().replace("-", "_")
    return read_env(STATE / "healthchecks.env").get(key, "")


def hc_ping(url: str) -> str:
    px = proxy()
    handlers = [urllib.request.ProxyHandler({"http": px, "https": px})] if px else []
    opener = urllib.request.build_opener(*handlers)
    for _ in range(3):
        try:
            with opener.open(url, timeout=15):
                return ""
        except OSError as e:
            err = str(e)
            time.sleep(2)
    return f"（healthchecks 打卡失败：{err}）"


def notify(title: str, body: str) -> None:
    subprocess.run([python(), str(REPO / "ops" / "notify.py"), title, body], env=child_env(),
                   capture_output=True, timeout=120, creationflags=NO_WINDOW)


def run(name: str, timeout_min: float, cmd: list[str]) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{name}.log"
    cmd = [python() if c in ("python", "python3") else c for c in cmd]
    started = datetime.now()
    timed_out = False
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"==> {started:%Y-%m-%d %H:%M:%S} {name}\n")
        f.flush()
        proc = subprocess.Popen(cmd, cwd=REPO, env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        try:
            out, _ = proc.communicate(timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
            out, _ = proc.communicate()
        text = (out or b"").decode("utf-8", "replace")
        f.write(text)
        code = proc.returncode if not timed_out else 124

        status = STATE / "jobs" / f"{name}.status"
        status.parent.mkdir(parents=True, exist_ok=True)
        prev = status.read_text(encoding="utf-8").strip() if status.exists() else "ok"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if code == 0:
            url = hc_url(name)
            if url:
                msg = hc_ping(url)
                if msg:
                    f.write(msg + "\n")
            status.write_text("ok", encoding="utf-8")
            if prev == "fail":
                notify(f"✅ AgentOS 已恢复：{name}", f"{now} 这个任务又能正常运行了。")
        else:
            status.write_text("fail", encoding="utf-8")
            if prev != "fail":
                why = f"超时（超过 {timeout_min:g} 分钟被中止）" if timed_out else f"退出码 {code}"
                tail = "\n".join(text.strip().splitlines()[-TAIL_LINES:])
                notify(f"⚠️ AgentOS 任务失败：{name}",
                       f"{why}\n时间：{now}\n恢复前不再重复提醒。\n\n日志最后 {TAIL_LINES} 行：\n{tail}")
        f.write(f"<== 退出码 {code}，用时 {(datetime.now() - started).seconds} 秒\n")
    return code


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 4 or args[2] != "--":
        print(__doc__)
        return 2
    return run(args[0], float(args[1]), args[3:])


if __name__ == "__main__":
    sys.exit(main())
