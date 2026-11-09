"""改了代码（或拉了更新）后运行：重启飞书机器人，让新代码生效。python ops/deploy.py
采集、复盘、备份这些定时任务每次都重新启动，保存代码就生效，不用重启。
Windows 版只有一份代码，没有 Mac 版那种运行副本，所以不用同步。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import IS_WIN, NO_WINDOW  # noqa: E402

TASK = "AgentOS-Bridge"


def main() -> int:
    if not IS_WIN:
        print("这个脚本是给 Windows 版用的")
        return 2
    subprocess.run(["schtasks", "/End", "/TN", TASK], capture_output=True, creationflags=NO_WINDOW)
    r = subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True, text=True,
                       encoding="gbk", errors="replace", creationflags=NO_WINDOW)
    if r.returncode != 0:
        print(f"重启失败：{(r.stderr or r.stdout).strip()}（飞书机器人装好了吗？见教程「装飞书机器人」）")
        return 1
    print("飞书机器人已重启")
    return 0


if __name__ == "__main__":
    sys.exit(main())
