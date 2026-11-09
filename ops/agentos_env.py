"""公用的小工具：路径、配置文件、代理、Windows 上不弹黑窗口。其他脚本都从这里取。

Windows 版只有一个账号：代码、Agent 干活的地方、采集原文都在同一个文件夹 REPO（默认 %USERPROFILE%\\AgentOS）。
钥匙放在 STATE（%USERPROFILE%\\.agentos），Agent 的权限规则禁止它读这个文件夹（见 .claude/settings.json）。

配置文件（都在 STATE 下，一行一个 名字=值，# 开头是注释）：
  bridge.env       飞书应用、本人 open_id、PushPlus
  mail.env         邮箱 IMAP
  agent.env        底座：AGENT_ENGINE（claude / codebuddy）和对应的令牌、密钥、接口地址
  healthchecks.env 各定时任务的打卡地址（可选）
  proxy            一行代理地址（可选，如 http://127.0.0.1:7890）
只用标准库。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WIN = os.name == "nt"
REPO = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("AGENTOS_STATE_DIR", Path.home() / ".agentos"))
SPACE = REPO / "spaces" / "me"
RAW = SPACE / "raw"
LOGS = STATE / "logs"
# 子进程不弹黑窗口（定时任务和飞书机器人都在后台跑）
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0


def read_env(path: Path) -> dict[str, str]:
    """读一个配置文件，返回 {名字: 值}；空值也返回（空字符串）。文件不存在返回空字典。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")  # 记事本存的文件开头可能有 BOM
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_env(path: Path) -> None:
    """把配置文件里的值放进环境变量（已经有的不覆盖）。"""
    for k, v in read_env(path).items():
        os.environ.setdefault(k, v)


def proxy() -> str:
    """~/.agentos/proxy 里写的代理地址；没有就返回空（直连，或者用 Windows 系统代理）。"""
    try:
        return (STATE / "proxy").read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def child_env(**extra: str) -> dict[str, str]:
    """给子进程的环境：强制 UTF-8（中文 Windows 默认是 GBK，读写中文文件会乱码或报错）。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def python() -> str:
    """跑子脚本用的 Python：就是当前这个（安装时建的 venv 里的那个）。"""
    exe = Path(sys.executable)
    # pythonw.exe 没有控制台，跑子脚本要用同目录的 python.exe 才拿得到输出
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def kill_tree(proc: subprocess.Popen, wait: float = 10) -> None:
    """结束一个进程和它启动的所有子进程。Windows 上 terminate() 只结束最外层，Agent 会变成孤儿一直挂着。"""
    if proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.terminate()
    try:
        proc.wait(wait)
    except subprocess.TimeoutExpired:
        proc.kill()
