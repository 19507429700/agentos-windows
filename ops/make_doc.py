#!/usr/bin/env python3
"""文档转换（Agent 用）：Markdown / HTML / Word 之间互转，以及转 PDF。底层是 LibreOffice。

  python3 ops/make_doc.py 输入.md   输出.docx      # Markdown → Word
  python3 ops/make_doc.py 输入.html 输出.pdf       # HTML → PDF
  python3 ops/make_doc.py 模板.docx 模板.html      # Word 模板 → HTML（读结构、照着填）
  python3 ops/make_doc.py 输入.docx 输出.txt       # Word → 纯文本（读用户发来的 Word）
  python3 ops/make_doc.py 图.png    输出.png       # 复制一张图（如飞书文档里下载的图），放进 outbox 就作为图片消息发出
  python3 ops/make_doc.py 画的.svg  输出.png       # SVG（自己画的示意图、图表）→ PNG

Markdown 里的 ![说明](spaces/me/...png) 会把图片嵌进 Word / PDF（路径从运行副本根目录算起）。
输出路径只能在 spaces/me/ 下（成品放 outbox/<任务号>/）。只用标准库，兼容 /usr/bin/python3（3.9）。
"""

from __future__ import annotations

import base64
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = {".docx": "docx:MS Word 2007 XML", ".pdf": "pdf", ".html": "html:XHTML Writer File:UTF8",
           ".txt": "txt:Text (encoded):UTF8", ".xlsx": "xlsx", ".pptx": "pptx", ".png": "png", ".jpg": "jpg"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMG_WIDTH = 560  # 嵌进 Word / PDF 的图最宽多少像素（大约一页宽）
CSS = ("body{font-family:'Hiragino Sans GB','PingFang SC','Microsoft YaHei',sans-serif;font-size:11pt;line-height:1.6}"
       "table{border-collapse:collapse}td,th{border:1px solid #999;padding:4px 8px}h1{font-size:18pt}")


# 无界面模式下 LibreOffice 用自带的 fontconfig 找字体，默认只看得到它自带的西文字体，中文转 PDF 会变方框；
# 每次转换给它一份配置，指向系统字体目录。
FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig><dir>/System/Library/Fonts</dir><dir>/Library/Fonts</dir><cachedir>{cache}</cachedir></fontconfig>
"""


def find_soffice() -> str:
    for c in (shutil.which("soffice"), "/opt/homebrew/bin/soffice",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if c and Path(c).exists():
            return c
    sys.exit("没装 LibreOffice（soffice），无法转换。请让用户运行：brew install --cask libreoffice")


def image_size(path: Path) -> tuple[int, int] | None:
    """读 PNG / JPEG 文件头里的宽高；读不出返回 None。"""
    try:
        data = path.read_bytes()[:256 * 1024]
    except OSError:
        return None
    if data.startswith(b"\x89PNG") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker, seg = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
            i += 2 + seg
    return None


def local_image(src: str) -> Path | None:
    """图片路径（从运行副本根目录算起、绝对路径或 file://）→ 运行副本里存在的文件；不是就返回 None。"""
    src = html.unescape(src)
    if src.startswith("file://"):
        src = urllib.parse.unquote(src[len("file://"):])
    p = Path(src)
    p = (p if p.is_absolute() else ROOT / p).resolve()
    return p if p.is_file() and p.is_relative_to(ROOT.resolve()) else None


def data_uri(p: Path) -> str:
    """把图片内容直接写进 HTML（data:）。LibreOffice 遇到 file:// 只存一个外部链接，换台设备就看不到图。"""
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".webp": "image/webp", ".bmp": "image/bmp", ".svg": "image/svg+xml"}.get(p.suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def img_tag(alt: str, src: str) -> str:
    """Markdown 图片 → <img>，图片内容嵌进去；太宽的按页宽缩小。"""
    p = local_image(src)
    if not p:
        return f"[图片找不到：{alt or src}]"
    size = image_size(p)
    attrs = ""
    if size and size[0] > 0:
        w = min(size[0], MAX_IMG_WIDTH)
        attrs = f' width="{w}" height="{round(size[1] * w / size[0])}"'
    return f'<img src="{data_uri(p)}" alt="{alt}"{attrs}>'


def embed_html_images(text: str) -> str:
    """HTML 里 <img src="本地路径或 file://"> 换成 data:，图片真正嵌进 Word / PDF。"""
    def repl(m: re.Match) -> str:
        p = local_image(m.group(2))
        return f'{m.group(1)}src="{data_uri(p)}"' if p else m.group(0)
    return re.sub(r'(<img\b[^>]*?)\bsrc="([^"]+)"', repl, text, flags=re.I)


def inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", lambda m: img_tag(m.group(1), m.group(2)), s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)


def md_to_html(md: str) -> str:
    """够用的 Markdown：标题、段落、无序/有序列表、表格、加粗、代码、链接、分隔线。"""
    out, lst, table = [], None, []

    def flush():
        nonlocal lst, table
        if lst:
            out.append(f"</{lst}>")
            lst = None
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|?\s*:?-{2,}.*", r)]
            out.append('<table border="1" cellpadding="4" cellspacing="0">' + "".join(
                "<tr>" + "".join(f"<{'th' if i == 0 else 'td'}>{inline(c.strip())}</{'th' if i == 0 else 'td'}>"
                                 for c in r.strip().strip("|").split("|")) + "</tr>"
                for i, r in enumerate(rows)) + "</table>")
            table = []
    for line in md.splitlines():
        s = line.rstrip()
        if s.startswith("|"):
            if lst:
                out.append(f"</{lst}>")
                lst = None
            table.append(s)
            continue
        if table:
            flush()
        m = re.match(r"(#{1,6})\s+(.*)", s)
        if m:
            flush()
            out.append(f"<h{len(m.group(1))}>{inline(m.group(2))}</h{len(m.group(1))}>")
        elif re.match(r"\s*[-*]\s+", s) or re.match(r"\s*\d+[.)]\s+", s):
            kind = "ol" if re.match(r"\s*\d", s) else "ul"
            if lst != kind:
                flush()
                out.append(f"<{kind}>")
                lst = kind
            item = re.sub(r"^\s*([-*]|\d+[.)])\s+", "", s)
            out.append(f"<li>{inline(item)}</li>")
        elif re.fullmatch(r"-{3,}|\*{3,}", s.strip()):
            flush()
            out.append("<hr/>")
        elif not s.strip():
            flush()
        else:
            if lst:
                flush()
            out.append(f"<p>{inline(s)}</p>")
    flush()
    return f'<html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{"".join(out)}</body></html>'


def convert(src: Path, dst: Path) -> None:
    fmt = TARGETS.get(dst.suffix.lower())
    if not fmt:
        sys.exit(f"不支持的输出格式：{dst.suffix}（支持 {' '.join(TARGETS)}）")
    s_ext, d_ext = src.suffix.lower().replace(".jpeg", ".jpg"), dst.suffix.lower().replace(".jpeg", ".jpg")
    if s_ext in IMAGE_SUFFIXES and s_ext == d_ext:  # 同格式的图片：直接复制
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return
    if d_ext in (".png", ".jpg") and s_ext not in (".svg", ".html", ".htm", ".md", ".markdown"):
        sys.exit("转成图片只支持 SVG、HTML、Markdown；图片之间换格式不支持，请保持原格式（如 .png → .png）")
    with tempfile.TemporaryDirectory() as tmp:
        work = src
        if src.suffix.lower() in (".html", ".htm"):
            text = src.read_text("utf-8", errors="replace")
            embedded = embed_html_images(text)
            if embedded != text:
                work = Path(tmp) / src.name
                work.write_text(embedded, "utf-8")
        if src.suffix.lower() in (".md", ".markdown"):
            work = Path(tmp) / (src.stem + ".html")
            work.write_text(md_to_html(src.read_text("utf-8")), "utf-8")
            if dst.suffix.lower() == ".html":
                shutil.copy(work, dst)
                return
        profile = Path(tmp) / "profile"  # 独立配置目录，几个任务同时转换也不互相锁住
        cmd = [find_soffice(), f"-env:UserInstallation=file://{profile}", "--headless",
               "--convert-to", fmt, "--outdir", tmp, str(work)]
        conf = Path(tmp) / "fonts.conf"
        conf.write_text(FONTS_CONF.format(cache=Path(tmp) / "fccache"))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env={**os.environ, "FONTCONFIG_FILE": str(conf)})
        produced = Path(tmp) / (work.stem + "." + fmt.split(":")[0])
        if r.returncode != 0 or not produced.exists():
            sys.exit(f"转换失败：{(r.stderr or r.stdout).strip()[-300:]}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), dst)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    src, dst = Path(argv[0]).resolve(), Path(argv[1]).resolve()
    if not src.is_file():
        sys.exit(f"找不到输入文件：{argv[0]}")
    if not dst.is_relative_to((ROOT / "spaces/me").resolve()):
        sys.exit("输出只能写在 spaces/me/ 下（成品放 spaces/me/outbox/<任务号>/）")
    convert(src, dst)
    print(f"已生成 {dst.relative_to(ROOT)}（{dst.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
