#!/usr/bin/env python3
"""微信公众号文章归档（半自动）。

分工：抓正文那一步 AgentOS 不碰（第三方脚本靠伪装微信客户端过风控，属于绕验证）；
本脚本只做干净活——列出还没归档的公众号链接、把你自己抓好的正文归档进知识库、去重，
归档后进检索、可被复盘引用。

用法（以你本人账号跑）：
  python3 ops/wechat_articles.py pending            # 列出待抓链接（标题+链接），并提示怎么抓
  python3 ops/wechat_articles.py urls               # 只打印待抓链接，一行一个
  python3 ops/wechat_articles.py ingest [目录]       # 把你抓好的 .md 归档进知识库（默认读收件夹）
    目录里每个 .md 开头若有 `source_url:`（抓取工具会写），用它去重；否则取正文里第一个公众号链接。
    正文是空的（抓取失败留下的空壳）不归档，链接留着下次再抓。
    文章里的图片下载到同名「-图片」文件夹（01.jpg、02.png…），正文原位置改成 ![图N](spaces/me/raw/…)。
  python3 ops/wechat_articles.py images             # 补图片：已归档文章里还是网址的图片，下载下来并改写正文
  python3 ops/wechat_articles.py skip <链接> [原因]  # 文章已删除/下架：记为失效，以后不再列出、不再抓

配置（环境变量，或 ~/.agentos/wechat.env）：
  AGENTOS_RAW   原文根目录，默认 /Users/agentos/agentos/spaces/me/raw
  WECHAT_ARTICLES_INBOX  你抓好的 .md 放哪，默认 ~/.agentos/wechat-articles-inbox
状态：~/.agentos/wechat_articles.json（done：已归档；gone：已失效。去重用）
只用标准库，兼容 /usr/bin/python3。
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(os.environ.get("AGENTOS_STATE_DIR", Path.home() / ".agentos"))
LINK_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[^\s　）)\"'<>]*")
TITLE_RE = re.compile(r"\[公众号\]\s*(.+)")

# ---------- 文章里的图片：下载到 <文章名>-图片/NN.ext，正文原位置改成 ![图N](spaces/me/raw/…)，和飞书文档一样 ----------
IMG_RE = re.compile(r'!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
LOCAL_PREFIX = "spaces/me/raw/"  # 已经是本地路径的图片引用以它开头
MAX_IMAGES = 200
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_DELAY_SEC = 0.3  # 两张图之间歇一下，别一口气把 CDN 打满
IMG_HEADERS = {
    # 微信图床有防盗链：不带来自 mp.weixin.qq.com 的 Referer 会给一张「此图片来自微信公众平台」的占位图
    "Referer": "https://mp.weixin.qq.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
IMG_EXT_BY_TYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif",
                   "image/webp": ".webp", "image/bmp": ".bmp", "image/svg+xml": ".svg"}


def sniff_image_ext(data: bytes, content_type: str = "") -> str:
    """看文件头判断图片格式，判断不了再看 Content-Type（和 feishu_docs.py 同一套）。"""
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if b"<svg" in data[:200]:
        return ".svg"
    return IMG_EXT_BY_TYPE.get(content_type.split(";")[0].strip().lower(), "")


def fetch_image(url: str, timeout: int = 20) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers=IMG_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_IMAGE_BYTES + 1), r.headers.get("Content-Type", "")


def rel_path(p: Path, base: Path) -> str:
    """图片在正文里的写法：从运行副本根目录算起（spaces/me/raw/…），Claude 用 Read、make_doc 都认。"""
    if base.parent.parent.name == "spaces":
        return p.relative_to(base.parents[2]).as_posix()
    return str(p)


def localize_images(text: str, src_dir: Path, img_dir: Path, rel: str, fetch=None):
    """把正文里的图片存成本地文件，引用改成 ![图N](rel/NN.ext)。

    网址图片下载；相对路径的本地图片（别的抓取工具下好的）复制过来；已经是本地路径的不动。
    下载失败的保留原网址，下次跑 images 子命令还会再试。返回 (新正文, 存下几张, 失败几张)。
    """
    fetch = fetch or fetch_image
    existing = [int(m.group(1)) for p in (img_dir.glob("*") if img_dir.is_dir() else [])
                if (m := re.match(r"(\d+)\.", p.name))]
    n = max(existing, default=0)
    done: dict[str, str] = {}  # 同一张图出现多次只存一份
    saved = failed = 0
    first_fetch = True

    def repl(m: re.Match) -> str:
        nonlocal n, saved, failed, first_fetch
        src = html.unescape(m.group(2))
        if src.startswith(LOCAL_PREFIX) or src.startswith("data:"):
            return m.group(0)
        if src in done:
            return done[src]
        if n >= MAX_IMAGES:
            return m.group(0)
        try:
            if src.startswith(("http://", "https://")):
                if not first_fetch:
                    time.sleep(IMAGE_DELAY_SEC)
                first_fetch = False
                data, ctype = fetch(src)
            else:
                local = (src_dir / src).resolve()
                if not local.is_file():
                    return m.group(0)
                data, ctype = local.read_bytes(), ""
        except Exception:  # 网络、防盗链、404……保留原网址
            failed += 1
            return m.group(0)
        ext = sniff_image_ext(data, ctype)
        if not data or len(data) > MAX_IMAGE_BYTES or not ext:
            failed += 1
            return m.group(0)
        n += 1
        img_dir.mkdir(parents=True, exist_ok=True)
        name = f"{n:02d}{ext}"
        (img_dir / name).write_bytes(data)
        saved += 1
        done[src] = f"![图{n}]({rel}/{name})"
        return done[src]

    return IMG_RE.sub(repl, text), saved, failed


def image_line(saved: int, failed: int) -> str:
    s = f"- 图片：已下载 {saved} 张，在同名「-图片」文件夹"
    return s + (f"；{failed} 张没下下来，保留原网址" if failed else "")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def raw_dir() -> Path:
    return Path(os.environ.get("AGENTOS_RAW", "/Users/agentos/agentos/spaces/me/raw"))


def inbox_dir() -> Path:
    return Path(os.environ.get("WECHAT_ARTICLES_INBOX", STATE_DIR / "wechat-articles-inbox"))


def state_path() -> Path:
    return STATE_DIR / "wechat_articles.json"


def load_state() -> dict:
    try:
        st = json.loads(state_path().read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("done", {})  # canon_key -> 归档后的相对路径
    st.setdefault("gone", {})  # canon_key -> {"url", "reason", "at"}：文章已删除/下架，不再抓
    return st


def handled(st: dict) -> set:
    """已归档或已失效的链接：都不再列为待抓。"""
    return set(st["done"]) | set(st["gone"])


MIN_BODY_CHARS = 50  # 去掉开头的 --- 元信息块后，正文少于这么多字就当没抓到


def body_text(text: str) -> str:
    """去掉开头的 YAML 元信息块（--- … ---）和空白，剩下的正文。"""
    m = re.match(r"\s*---\n.*?\n---[ \t]*\n?", text, re.S)
    return (text[m.end():] if m else text).strip()


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, state_path())
    except BaseException:
        os.unlink(tmp)
        raise


def canon(url: str) -> str:
    """把链接归一成一个稳定的去重键：优先取 sn 参数，其次 /s/<hash>，否则整条。"""
    url = html.unescape(url)
    m = re.search(r"[?&]sn=([0-9a-fA-F]+)", url)
    if m:
        return "sn:" + m.group(1)
    m = re.search(r"/s/([A-Za-z0-9_-]{8,})", url)
    if m:
        return "s:" + m.group(1)
    return url.split("#")[0]


def safe_name(name: str) -> str:
    name = unicodedata.normalize("NFKC", name)
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_")[:50] or "未命名"


def scan_links(base: Path):
    """扫采集到的微信聊天 .md，产出 (canon_key, url, 标题)。跳过 articles 归档目录自身。"""
    wechat = base / "wechat"
    if not wechat.exists():
        return
    seen = set()
    for path in sorted(wechat.rglob("*.md")):
        if "articles" in path.parts:
            continue
        last_title = ""
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            t = TITLE_RE.search(line)
            if t:
                last_title = t.group(1).split(" — ")[0].strip()
            for url in LINK_RE.findall(line):
                key = canon(url)
                if key in seen:
                    continue
                seen.add(key)
                yield key, html.unescape(url), last_title


def cmd_pending() -> int:
    base = raw_dir()
    st = load_state()
    done, skip = st["done"], handled(st)
    items = [(k, u, t) for k, u, t in scan_links(base) if k not in skip]
    if not items:
        print("没有待归档的公众号链接（都归档过了，或还没采到）。")
        return 0
    inbox = inbox_dir()
    gone = f"，已失效 {len(st['gone'])} 篇" if st["gone"] else ""
    print(f"待归档公众号 {len(items)} 篇（已归档 {len(done)} 篇{gone}，跳过）：\n")
    for i, (_k, url, title) in enumerate(items, 1):
        print(f"{i:2d}. {title or '(无标题)'}")
        print(f"    {url}")
    print("\n—— 抓正文这步你自己做（AgentOS 不碰）——")
    print(f"用你选的工具把上面的链接抓成 Markdown（.md），放进收件夹：{inbox}")
    print("每个 .md 开头最好带一行  source_url: <链接>  （多数抓取工具会自动写），用来去重。")
    print("\n抓完后归档进知识库：")
    print("  python3 ops/wechat_articles.py ingest")
    return 0


def cmd_urls() -> int:
    """只打印待抓链接，一行一个，方便直接管道喂给你的抓取工具。"""
    skip = handled(load_state())
    for key, url, _title in scan_links(raw_dir()):
        if key not in skip:
            print(url)
    return 0


def cmd_skip(url: str, reason: str) -> int:
    """把一个链接记为失效（文章已删除/下架），以后不再列出、不再抓。"""
    st = load_state()
    key = canon(url)
    if key in st["done"]:
        print(f"已归档过，不用记失效：{url}")
        return 0
    st["gone"][key] = {"url": html.unescape(url), "reason": reason or "文章不可用",
                       "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    save_state(st)
    print(f"已记为失效，以后不再抓：{url}（{reason or '文章不可用'}）")
    return 0


def _front(text: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(.+)$", text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'") or None


def cmd_ingest(folder: Path | None) -> int:
    base = raw_dir()
    src = folder or inbox_dir()
    if not src.exists():
        print(f"收件夹不存在：{src}（先抓几篇放进去）")
        return 0
    st = load_state()
    done = st["done"]
    out_root = base / "wechat" / "articles"
    n = 0
    for md in sorted(src.glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        url = _front(text, "source_url") or _front(text, "url")
        if not url:
            m = LINK_RE.search(text)
            url = m.group(0) if m else None
        if not url:
            print(f"跳过（找不到来源链接）：{md.name}")
            continue
        key = canon(url)
        if key in done:
            continue  # 已归档，去重
        if len(body_text(text)) < MIN_BODY_CHARS:
            # 抓取失败也会留下只有元信息的空壳：不归档、不记已归档，链接下次照样会被列出来重抓
            print(f"跳过（正文是空的，可能没抓到）：{md.name}")
            continue
        title = _front(text, "title") or md.stem
        pub = _front(text, "publish_time") or ""
        day = (re.search(r"\d{4}-\d{2}-\d{2}", pub) or [None])
        day = day.group(0) if hasattr(day, "group") else datetime.now().strftime("%Y-%m-%d")
        dest = out_root / f"{day}-{safe_name(title)}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        img_dir = dest.with_name(dest.stem + "-图片")
        text, saved, failed = localize_images(text, md.parent, img_dir, rel_path(img_dir, base))
        header = (f"# {title}\n\n- 来源：微信公众号文章（你本人抓取并归档）\n"
                  f"- 链接：{html.unescape(url)}\n- 归档：{datetime.now():%Y-%m-%d %H:%M}\n"
                  f"{image_line(saved, failed)}\n\n---\n\n")
        write_atomic(dest, header + text)
        done[key] = str(dest.relative_to(base.parent.parent)) if base.name == "raw" else str(dest)
        n += 1
        print(f"已归档：{dest.name}（图片 {saved} 张" + (f"，{failed} 张没下下来）" if failed else "）"))
    save_state(st)
    print(f"\n完成：新归档 {n} 篇，累计 {len(done)} 篇。进了知识库 {out_root}，检索和复盘能用。")
    return 0


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)  # Claude（agentos 账号）要能读
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def cmd_images() -> int:
    """补图片：已归档的文章里还是网址的图片，下载到本地并改写正文（上次没下下来的也会再试）。"""
    base = raw_dir()
    out_root = base / "wechat" / "articles"
    total_saved = total_failed = files = 0
    for md in sorted(out_root.glob("*.md")) if out_root.is_dir() else []:
        text = md.read_text(encoding="utf-8", errors="replace")
        if not any(m.group(2).startswith(("http://", "https://")) for m in IMG_RE.finditer(text)):
            continue
        img_dir = md.with_name(md.stem + "-图片")
        new, saved, failed = localize_images(text, md.parent, img_dir, rel_path(img_dir, base))
        if saved == 0 and failed == 0:
            continue
        # 头部的「- 图片：」一行按这篇现在的总数重写（本地已有的 + 这次没下下来的）
        have = len(set(re.findall(r"!\[图\d+\]\((" + re.escape(LOCAL_PREFIX) + r"[^)]+)\)", new)))
        line = image_line(have, failed)
        if re.search(r"^- 图片：.*$", new, re.M):
            new = re.sub(r"^- 图片：.*$", lambda _m: line, new, count=1, flags=re.M)
        else:
            new = re.sub(r"^(- 归档：.*)$", lambda m: m.group(1) + "\n" + line, new, count=1, flags=re.M)
        write_atomic(md, new)
        files += 1
        total_saved += saved
        total_failed += failed
        print(f"{md.name}：新下载 {saved} 张" + (f"，{failed} 张没下下来" if failed else ""))
    print(f"\n补图片完成：{files} 篇，新下载 {total_saved} 张，没下下来 {total_failed} 张。")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env(STATE_DIR / "wechat.env")
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "pending"
    if cmd == "pending":
        return cmd_pending()
    if cmd == "urls":
        return cmd_urls()
    if cmd == "ingest":
        folder = Path(args[1]).expanduser() if len(args) > 1 else None
        return cmd_ingest(folder)
    if cmd == "images":
        return cmd_images()
    if cmd == "skip" and len(args) > 1:
        return cmd_skip(args[1], " ".join(args[2:]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
