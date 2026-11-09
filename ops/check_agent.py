"""自检：底座能不能叫醒、钥匙文件在不在。装好底座后、机器人没反应时先跑它：python ops/check_agent.py"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import NO_WINDOW, REPO, STATE, child_env, python, read_env  # noqa: E402


def main() -> int:
    engine = read_env(STATE / "agent.env").get("AGENT_ENGINE") or "claude"
    print(f"==> 叫醒 Agent（底座：{engine}），让它只回复 OK。第一次可能要等半分钟……")
    r = subprocess.run([python(), str(REPO / "ops" / "run_agent.py"), "-"], input="只回复 OK", cwd=REPO,
                       env=child_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300, creationflags=NO_WINDOW)
    result = None
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            result = ev
    if not result or result.get("is_error"):
        print("    ❌ 失败")
        detail = (result or {}).get("result") or r.stderr.strip() or r.stdout.strip()
        print("    " + (detail[-800:] if detail else f"退出码 {r.returncode}，没有任何输出"))
        return 1
    print(f"    Agent 回复：{result.get('result', '').strip()}")

    print("==> 钥匙文件")
    for name in ("bridge.env", "agent.env"):
        p = STATE / name
        print(f"    {'✅' if p.exists() else '⚠️ 还没有'} {p}")
    print("    OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
