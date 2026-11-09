"""知识库备份（任务计划程序每天 03:00 经 run_job.py 调用；也可手动）：python ops/push_kb.py
  1. 提交 spaces/me/kb/ 的改动（复盘、画像、人物页……）；原文 raw/、草稿 outbox/ 被 .gitignore 排除，不上传
  2. 推到 origin（你自己的私有仓库）；有 ~/.agentos/proxy 就走代理
  3. 配了 gitee 远程就再推一份（失败只提示，不影响 GitHub 备份）
origin 还是模板仓库（刚 git clone 下来、没按教程第二步换）时拒绝备份，免得把你的知识库推进给别人用的模板。
只检查不备份：python ops/push_kb.py --check（安装脚本用它决定装不装每天的备份任务）
Windows 版只有一份代码（没有 Mac 版那种运行副本），所以直接在这里提交。只用标准库。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import NO_WINDOW, REPO, is_template, proxy  # noqa: E402


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    px = proxy()
    cmd = ["git", "-C", str(REPO)] + (["-c", f"http.proxy={px}"] if px else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{(r.stderr or r.stdout).strip()[-500:]}")
    return r


def origin_problem() -> str:
    """origin 能不能用来备份知识库；能就返回空字符串，不能就返回原因。"""
    origin = git("remote", "get-url", "origin", check=False).stdout.strip()
    if not origin:
        return "没有设置备份仓库（origin）。不想备份可以不管；想备份就按教程第二步设好"
    if is_template(origin):
        return (f"origin 还是模板仓库（{origin}），不能把你的知识库推进去。"
                "按教程第二步：git remote rename origin upstream，再 git remote add origin 你自己的仓库地址")
    return ""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    problem = origin_problem()
    if "--check" in args:
        print(problem or "备份仓库：" + git("remote", "get-url", "origin").stdout.strip())
        return 1 if problem else 0
    print(f"==> {datetime.now():%Y-%m-%d %H:%M:%S} 备份知识库")
    if problem:
        print(problem, file=sys.stderr)
        return 1
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    origin = git("remote", "get-url", "origin").stdout.strip()
    git("add", "-A", "--", "spaces/me/kb")
    if git("diff", "--cached", "--quiet", "--", "spaces/me/kb", check=False).returncode == 0:
        print("    知识库没有变化")
    else:
        git("-c", "user.name=AgentOS", "-c", "user.email=agentos@localhost", "commit", "-q",
            "-m", f"知识库备份 {datetime.now():%Y-%m-%d %H:%M}", "--", "spaces/me/kb")
        print("    已提交：" + git("log", "--oneline", "-1").stdout.strip())
    git("push", "-q", "origin", branch)
    print("    已推到 origin：" + origin)
    if git("remote", "get-url", "gitee", check=False).returncode == 0:
        r = git("push", "-q", "gitee", branch, check=False)
        print("    已推到 Gitee" if r.returncode == 0 else f"    !! 推送 Gitee 失败：{r.stderr.strip()[-200:]}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
