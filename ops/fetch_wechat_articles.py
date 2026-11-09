#!/usr/bin/env python3
"""微信公众号文章自动抓取归档（定时任务入口）。

把「列出待抓链接 → 抓正文 → 归档进知识库」这条链路串起来，供 launchd 定时调用。

分工与边界（和 wechat_articles.py 一致）：
- 列待抓链接、去重归档由 ops/wechat_articles.py 负责，这里只调它；
- 抓正文由 ops/fetch_wx.py 负责（第三方 OzzyLennon/wechat-article-extract，MIT 协议，
  微信客户端 UA 伪装过风控，抓不到就明确失败、绝不假装成功）；
- 本脚本只做编排：pending 链接 -> fetch_wx.py 抓成 .md -> 放进收件夹 -> ingest 归档。

抓取失败（被风控 / 网络）不打乱整体：单篇失败记日志继续下一篇，下一轮重试；
每篇先抓进临时目录，成功才放进收件夹（失败留下的空壳不会被当成文章归档）。
文章本身没了（删除 / 下架 / 迁移）：记为失效，以后不再抓，也不算失败。
有失败且全部失败退出码 2（供 run-job.sh 告警），部分成功退出码 1，没有失败 0。

配置（环境变量，或 ~/.agentos/wechat.env）：
  AGENTOS_RAW    原文根目录，默认 /Users/agentos/agentos/spaces/me/raw
  WECHAT_ARTICLES_INBOX  抓好的 .md 放哪，默认 ~/.agentos/wechat-articles-inbox
  WECHAT_FETCH_PY   fetch_wx.py 路径，默认本脚本同目录 fetch_wx.py
  WECHAT_FETCH_PYTHON  运行 fetch_wx.py 的 Python（带 requests+bs4），默认 ~/.agentos/wxfetch-python/bin/python

用法：
  python3 ops/fetch_wechat_articles.py           # 抓 pending 链接并归档
  python3 ops/fetch_wechat_articles.py --dry-run # 只列待抓链接，不真抓
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path(os.environ.get("AGENTOS_STATE_DIR", Path.home() / ".agentos"))
REPO = Path(__file__).resolve().parent.parent
# fetch_wx.py 判定「文章本身没了」（删除/下架/迁移）时报错里的字样；IP 被墙、网络错误不含它
GONE_MARK = "文章不可用"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def inbox_dir() -> Path:
    return Path(os.environ.get("WECHAT_ARTICLES_INBOX", STATE_DIR / "wechat-articles-inbox"))


def wechat_articles_py() -> Path:
    return REPO / "ops" / "wechat_articles.py"


def fetch_wx_py() -> Path:
    p = os.environ.get("WECHAT_FETCH_PY", "")
    if p:
        return Path(p)
    return REPO / "ops" / "fetch_wx.py"


def fetch_python() -> Path:
    p = os.environ.get("WECHAT_FETCH_PYTHON", "")
    if p:
        return Path(p)
    return Path.home() / ".agentos" / "wxfetch-python" / "bin" / "python"


def pending_urls() -> list[str]:
    """调 wechat_articles.py urls，拿还没归档的公众号链接（一行一个）。"""
    r = subprocess.run(
        [sys.executable, str(wechat_articles_py()), "urls"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    urls = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("http")]
    return urls


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    load_env(STATE_DIR / "wechat.env")
    args = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in args

    urls = pending_urls()
    if not urls:
        print("没有待抓取的公众号链接（都归档过了，或还没采到）。")
        return 0

    if dry_run:
        print(f"待抓取 {len(urls)} 篇：")
        for u in urls:
            print(u)
        return 0

    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    ok, fail, gone = 0, 0, 0
    for u in urls:
        # 先抓进临时目录：fetch_wx.py 失败时也会写一个只有元信息的空壳 .md，
        # 直接写进收件夹会被当成文章归档、链接记成「已归档」，以后再也不重抓。
        with tempfile.TemporaryDirectory(prefix=".fetch-", dir=inbox) as tmp:
            r = run([str(fetch_python()), str(fetch_wx_py()), "--url", u,
                     "--format", "md", "--out_dir", tmp])
            if r.returncode == 0:
                for md in Path(tmp).glob("*.md"):
                    shutil.move(str(md), str(inbox / md.name))
                ok += 1
                continue
        # 抓失败的详情（fetch_wx.py 打在 stderr）记进本脚本日志
        err = (r.stderr or "").strip()
        tail = err.splitlines()
        reason = tail[-1].strip() if tail else f"退出码 {r.returncode}"
        if GONE_MARK in err:
            # 文章已删除/下架/迁移：重抓也没用，记为失效，不算失败，免得任务一直处在失败状态
            gone += 1
            why = reason.split("FAIL:", 1)[-1].strip()
            run([sys.executable, str(wechat_articles_py()), "skip", u, why])
            print(f"[失效] {u} -> {why}（以后不再抓）")
        else:
            fail += 1
            print(f"[失败] {u} -> {reason}")

    # 把成功抓进收件夹的归档进知识库（wechat_articles.py ingest 自带去重）
    r = run([sys.executable, str(wechat_articles_py()), "ingest"])
    print(r.stdout.strip())

    gone_note = f"，失效 {gone}（文章已删除，以后不再抓）" if gone else ""
    print(f"\n抓取完成：成功 {ok}，失败 {fail}（失败会在下一轮重试）{gone_note}。")
    if fail == 0:
        return 0
    if ok == 0:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
