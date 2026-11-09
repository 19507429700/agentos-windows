"""运行一轮 Agent。由飞书机器人、每日复盘调用，提示词走 stdin，输出 stream-json（最后一条 type=result）。
用法：python ops/run_agent.py <会话 ID 或 ->

底座在 %USERPROFILE%\\.agentos\\agent.env 里选：
  AGENT_ENGINE=claude     Claude Code（默认）。钥匙二选一：
                            A. CLAUDE_CODE_OAUTH_TOKEN：Claude 订阅的一年期令牌（claude setup-token）
                            B. ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN（+ ANTHROPIC_MODEL）：
                               其他公司提供的 Anthropic 兼容接口（DeepSeek、Kimi、智谱等）
  AGENT_ENGINE=codebuddy  腾讯 WorkBuddy 的 CodeBuddy Code。钥匙：CODEBUDDY_API_KEY + CODEBUDDY_INTERNET_ENVIRONMENT
空着的项不传给 Agent，免得被当成「改了地址」「换了钥匙」。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import IS_WIN, NO_WINDOW, REPO, STATE, child_env, proxy, read_env  # noqa: E402

# 每个底座只拿到自己用得到的钥匙：两种底座的钥匙可以同时填在 agent.env 里，切换时只改 AGENT_ENGINE，
# 不会把 DeepSeek 的地址、密钥交给 CodeBuddy，反过来也一样
ENGINE_KEYS = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
               "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_GIT_BASH_PATH"),
    "codebuddy": ("CODEBUDDY_API_KEY", "CODEBUDDY_INTERNET_ENVIRONMENT"),
}
AGENT_KEYS = tuple(k for keys in ENGINE_KEYS.values() for k in keys)
SID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _git_roots_from_registry() -> list[Path]:
    """Git 官方安装包在注册表里登记的安装目录（HKLM / HKCU 的 SOFTWARE\\GitForWindows\\InstallPath）。"""
    try:
        import winreg
    except ImportError:  # 不是 Windows
        return []
    roots = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows") as k:
                roots.append(Path(winreg.QueryValueEx(k, "InstallPath")[0]))
        except OSError:
            pass
    return roots


def _up(p: Path, levels: int) -> list[Path]:
    out = []
    for _ in range(levels):
        out.append(p)
        p = p.parent
    return out


def find_git_bash(path: str = "") -> str | None:
    """找 Git 自带的 bash.exe（Claude Code 在 Windows 上用它跑命令）。Git 不一定装在 C:\\Program Files\\Git，
    按下面的顺序找，找到第一个存在的 <Git 目录>\\bin\\bash.exe：
      1. 顺着 git 命令的位置：<Git 目录>\\cmd\\git.exe（也可能在 bin\\、mingw64\\bin\\ 下）
      2. 问 Git 自己：git --exec-path 给出 <Git 目录>\\mingw64\\libexec\\git-core（Scoop 这类「转接」的 git 也能问出真实位置）
      3. 注册表里 Git 官方安装包登记的安装目录
      4. 默认安装位置 C:\\Program Files\\Git"""
    roots: list[Path] = []
    git = shutil.which("git", path=path or None)
    if git:
        roots += _up(Path(git).resolve().parent, 3)
        try:
            r = subprocess.run([git, "--exec-path"], capture_output=True, text=True, timeout=10,
                               creationflags=NO_WINDOW)
            if r.returncode == 0 and r.stdout.strip():
                roots += _up(Path(r.stdout.strip()), 5)
        except (OSError, subprocess.SubprocessError):
            pass
    roots += _git_roots_from_registry()
    roots += [Path(r"C:\Program Files\Git"), Path(r"C:\Program Files (x86)\Git")]
    for root in roots:
        c = root / "bin" / "bash.exe"
        if c.is_file():
            return str(c)
    return None


def build_env() -> tuple[str, dict[str, str]]:
    conf = read_env(STATE / "agent.env")
    engine = conf.get("AGENT_ENGINE", "").strip() or "claude"
    env = child_env()
    for k in AGENT_KEYS:          # 先清掉外面带进来的，只认 agent.env 里填了值的
        env.pop(k, None)
    for k, v in conf.items():
        if k in ENGINE_KEYS.get(engine, ()) and v:
            env[k] = v
    px = proxy()
    if px:
        env.setdefault("HTTPS_PROXY", px)
        env.setdefault("HTTP_PROXY", px)
        env.setdefault("NO_PROXY", "localhost,127.0.0.1")
    # Agent 在命令里写 python，要用到的就是装好的这个 Python（venv 里的），不管系统 PATH 怎么样
    extra = [str(Path(sys.executable).parent), str(Path.home() / ".local" / "bin")]
    if IS_WIN:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            extra.append(str(Path(appdata) / "npm"))   # npm 装的 claude / codebuddy 在这里
        if "CLAUDE_CODE_GIT_BASH_PATH" not in env:   # Claude Code 在 Windows 上要用 Git 自带的 bash 跑命令
            bash = find_git_bash(env.get("PATH", ""))
            if bash:
                env["CLAUDE_CODE_GIT_BASH_PATH"] = bash
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return engine, env


def sync_codebuddy_rules() -> None:
    """CodeBuddy 读 CODEBUDDY.md、.codebuddy/settings.json、.codebuddy/skills/：从 .claude 那几份复制过去。
    只有一份真内容（CLAUDE.md、.claude/），改那边就行；这几个复制品不进 Git。"""
    (REPO / ".codebuddy").mkdir(exist_ok=True)
    shutil.copyfile(REPO / "CLAUDE.md", REPO / "CODEBUDDY.md")
    shutil.copyfile(REPO / ".claude" / "settings.json", REPO / ".codebuddy" / "settings.json")
    if (REPO / ".claude" / "skills").is_dir():
        shutil.copytree(REPO / ".claude" / "skills", REPO / ".codebuddy" / "skills", dirs_exist_ok=True)


def trust_project() -> None:
    """无界面运行时没人点「信任此目录」；不标记的话 .claude/settings.json 的权限会被忽略，Agent 读写不了文件。"""
    p = Path.home() / ".claude.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return
    projects = d.setdefault("projects", {})
    keys = {str(REPO), REPO.as_posix()}
    if all(projects.get(k, {}).get("hasTrustDialogAccepted") for k in keys):
        return
    for k in keys:
        projects.setdefault(k, {})["hasTrustDialogAccepted"] = True
    tmp = p.with_suffix(".json.agentos-tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def find_exe(name: str, env: dict[str, str]) -> str | None:
    return shutil.which(name, path=env["PATH"])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    sid = args[0] if args else "-"
    resume: list[str] = []
    if sid != "-":
        if not SID_RE.match(sid):
            print("bad session id", file=sys.stderr)
            return 2
        resume = ["--resume", sid]

    engine, env = build_env()
    common = ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk"]
    if engine == "claude":
        exe = find_exe("claude", env)
        trust_project()
    elif engine == "codebuddy":
        exe = find_exe("codebuddy", env)
        sync_codebuddy_rules()
        # CodeBuddy 文档说无界面模式要加 -y（跳过所有权限检查）。这里故意不加：加了就等于拆掉
        # 「Agent 只能跑那几条命令、读不到钥匙」的防线。只用 dontAsk 跑不通的话，要另想办法，不能直接放开。
    else:
        print(f"AGENT_ENGINE 只能是 claude 或 codebuddy，现在是「{engine}」（在 {STATE / 'agent.env'} 里改）",
              file=sys.stderr)
        return 2
    if not exe:
        print(f"找不到 {engine} 命令：Agent 还没装好，按教程「装底座」那一步装", file=sys.stderr)
        return 127

    # stdin、stdout、stderr 明确交给 Agent（飞书机器人从这里读它的 stream-json）。
    # Windows 上加了 CREATE_NO_WINDOW 时，不明确传的话子进程不一定继承到父进程的管道。
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.call([exe, *common, *resume], cwd=REPO, env=env, creationflags=NO_WINDOW,
                           stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
