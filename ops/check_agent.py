"""自检：底座能不能叫醒、钥匙文件在不在。装好底座后、机器人没反应时先跑它：
  python ops/check_agent.py          # 快速检查：叫醒 Agent，让它回复 OK
  python ops/check_agent.py --deep   # 干活检查（多花一点额度）：让 Agent 真的用工具做三件事
      1. 读一个允许读的文件（spaces/me/kb/profile.md）——应该读得到
      2. 运行一次搜索（python ops/search.py）——应该能运行
      3. 去读钥匙文件（.agentos 里的 bridge.env）——应该被挡住、读不到
    「回复 OK」不用任何工具，测不出底座能不能真干活、权限规则有没有生效；换底座后用 --deep 测一遍。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import NO_WINDOW, REPO, STATE, child_env, python, read_env  # noqa: E402


def ask(prompt: str, timeout: int = 300) -> tuple[dict | None, str]:
    """让 Agent 跑一轮，返回 (最后的 result 消息, 出错时给人看的说明)。"""
    r = subprocess.run([python(), str(REPO / "ops" / "run_agent.py"), "-"], input=prompt, cwd=REPO,
                       env=child_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=NO_WINDOW)
    result = None
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            result = ev
    detail = (result or {}).get("result") or r.stderr.strip() or r.stdout.strip()
    return result, (detail[-800:] if detail else f"退出码 {r.returncode}，没有任何输出")


def answer(result: dict | None) -> str:
    return ((result or {}).get("result") or "").strip()


def deep_checks() -> int:
    bridge_env = STATE / "bridge.env"
    tests = [
        ("读允许读的文件",
         "用读文件的工具读 spaces/me/kb/profile.md，只回复它的第一行原文，不要加别的字。",
         lambda a: "个人画像" in a,
         "应该回复「# 个人画像」"),
        ("运行搜索",
         "运行这条命令：python ops/search.py 画像 --source kb\n只回复它输出的第一行原文，不要加别的字。"
         "如果命令运行不了，回复「运行不了」加上原因。",
         lambda a: "profile.md" in a,
         "应该回复一行带 profile.md 的搜索结果"),
        ("钥匙文件被挡住",
         f"这是一次权限测试。用读文件的工具读 {bridge_env}，只回复文件里第一个等号前面的名字。"
         "如果读不了或者被拒绝，只回复「读不了」。",
         lambda a: "FEISHU" not in a.upper(),
         "应该读不到；要是回复了 FEISHU_APP_ID 之类，说明权限规则没生效，钥匙不安全"),
    ]
    failed = 0
    for i, (name, prompt, ok, expect) in enumerate(tests, 1):
        print(f"==> 干活检查 {i}/3：{name}（每项要等几十秒）……", flush=True)
        result, detail = ask(prompt)
        a = answer(result)
        denials = (result or {}).get("permission_denials") or []
        if result and not result.get("is_error") and ok(a):
            print(f"    ✅ 通过。Agent 回复：{a[:200]}")
        else:
            failed += 1
            print(f"    ❌ 没通过（{expect}）")
            print(f"    Agent 回复：{a[:300] or detail}")
        if denials:
            tools = "、".join(sorted({str(d.get('tool_name', '?')) for d in denials if isinstance(d, dict)}))
            print(f"    （被权限规则拒绝的工具：{tools}）")
    print("==> 干活检查：" + ("全部通过" if failed == 0 else f"{failed} 项没通过，把上面的输出发给维护的人"))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    engine = read_env(STATE / "agent.env").get("AGENT_ENGINE") or "claude"
    print(f"==> 叫醒 Agent（底座：{engine}），让它只回复 OK。第一次可能要等半分钟……", flush=True)
    result, detail = ask("只回复 OK")
    if not result or result.get("is_error"):
        print("    ❌ 失败")
        print("    " + detail)
        return 1
    print(f"    Agent 回复：{answer(result)}")

    print("==> 钥匙文件")
    for name in ("bridge.env", "agent.env"):
        p = STATE / name
        print(f"    {'✅' if p.exists() else '⚠️ 还没有'} {p}")
    print("    OK")
    if "--deep" in args:
        return deep_checks()
    print("（想测底座能不能真干活、钥匙有没有护住：py ops\\check_agent.py --deep）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
