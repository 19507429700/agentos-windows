"""每日复盘（任务计划程序每天 23:00 经 run_job.py 调用；也可手动补写某天）：
  python ops/daily_review.py [YYYY-MM-DD]
  1. 补采一遍邮箱和飞书
  2. 让 Agent 按 daily-review 技能写复盘：全文进 kb/daily/<日期>.md，回复一页摘要
  3. 摘要推到你的飞书 + 微信
Agent 出错时把原因打出来（比如令牌失效），不会静默退出。只用标准库。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import NO_WINDOW, RAW, REPO, SPACE, STATE, child_env, python, read_env  # noqa: E402

DOC_RE = re.compile(r"/(文档|妙记)-")


def day_files(day: str) -> tuple[list[str], list[str]]:
    """当天原文：往来（聊天、邮件、收件箱）是复盘的主体；当天新存的飞书文档、妙记只是参考。"""
    found = sorted("spaces/me/raw/" + p.relative_to(RAW).as_posix()
                   for p in RAW.glob(f"*/{day}/**/*.md") if p.is_file())
    files = [f for f in found if not DOC_RE.search(f)]
    docs = [f for f in found if DOC_RE.search(f)]
    return files, docs


def build_prompt(day: str, files: list[str], docs: list[str], missing: list[str]) -> str:
    yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    return (f"按技能 daily-review 写 {day} 的每日复盘。\n"
            f"昨天是 {yesterday}（有 spaces/me/kb/daily/{yesterday}.md 就读它的「明天三件事」）。\n"
            f"当天往来原文（聊天、邮件、收件箱，共 {len(files)} 个，这是复盘的主体）：\n"
            f"{chr(10).join(files) or '（无）'}\n"
            f"当天新存或有更新的飞书文档和妙记（共 {len(docs)} 篇；它们多半是以前就有的，只是今天第一次读到，"
            f"别当成今天发生的事，和当天往来相关时再去读）：\n"
            f"{chr(10).join(docs) or '（无）'}\n"
            "日程（今天起 7 天，排「明天三件事」时参考）：spaces/me/raw/feishu/日程/最近日程.md\n"
            f"没采到的来源：{' '.join(missing) or '无'}\n"
            f"全文写进 spaces/me/kb/daily/{day}.md，最后只回复那一页摘要。")


def parse_result(out: str, rc: int) -> tuple[bool, str]:
    """从 stream-json 里取最后的 result。返回 (成功?, 摘要或出错原因)。"""
    res, lines = None, []
    for line in out.splitlines():
        if line.strip():
            lines.append(line.strip())
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            res = ev
    if res and not res.get("is_error") and (res.get("result") or "").strip():
        return True, res["result"].strip()
    if res:
        why = (res.get("result") or "").strip() or json.dumps(res, ensure_ascii=False)
        return False, f"❌ Agent 没有正常写完复盘（退出码 {rc}，{res.get('subtype') or ''}）：{why[:500]}"
    tail = "\n".join(line[:300] for line in lines[-5:]) or "（没有任何输出）"
    return False, f"❌ Agent 没有输出结果（退出码 {rc}）。最后几行：\n{tail}"


def run_py(script: str, *args: str, stdin: str | None = None, timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run([python(), str(REPO / "ops" / script), *args], cwd=REPO, env=child_env(),
                          input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    day = args[0] if args else date.today().isoformat()
    try:
        date.fromisoformat(day)
    except ValueError:
        print("日期格式应为 YYYY-MM-DD", file=sys.stderr)
        return 2

    print(f"==> {datetime.now():%Y-%m-%d %H:%M:%S} 每日复盘 {day}", flush=True)
    missing = []
    # 没接的来源不补采（不然没配邮箱也会报错、复盘里写「邮箱没采到」）；微信没取钥匙时 collect_wechat.py 安静退出 0
    sources = []
    if read_env(STATE / "mail.env").get("MAIL_PASSWORD"):
        sources.append(("collect_mail.py", "邮箱"))
    if (STATE / "feishu_user_token.json").exists():
        sources.append(("collect_feishu.py", "飞书"))
    sources.append(("collect_wechat.py", "微信"))
    for script, label in sources:
        r = run_py(script, timeout=600)
        print((r.stdout + r.stderr).strip(), flush=True)
        if r.returncode != 0:
            missing.append(label)

    files, docs = day_files(day)
    print(f"    当天往来 {len(files)} 个文件，新存文档和妙记 {len(docs)} 篇", flush=True)
    r = run_py("run_agent.py", "-", stdin=build_prompt(day, files, docs, missing), timeout=2400)
    ok, summary = parse_result(r.stdout, r.returncode)
    if not ok:
        print(summary, file=sys.stderr)
        if r.stderr.strip():
            print(r.stderr.strip()[-1000:], file=sys.stderr)
        return 1

    if not (SPACE / "kb" / "daily" / f"{day}.md").exists():
        print(f"    ⚠️ 没找到 kb/daily/{day}.md，Agent 可能没写全文")
    n = run_py("notify.py", f"📋 每日复盘 {day}", summary, timeout=120)
    print(n.stdout.strip())
    print("    已推送")
    return 0


if __name__ == "__main__":
    sys.exit(main())
