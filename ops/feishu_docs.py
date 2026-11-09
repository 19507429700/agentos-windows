#!/usr/bin/env python3
"""飞书云文档读取（以你本人身份，只读），供 collect_feishu.py 调用：
  - 文档（docx）：按「块」转成 Markdown，标题、列表、代码、表格都保留；图片下载成文件，在原位置写 ![图N](路径)
  - 电子表格（sheet）：每个工作表转成 Markdown 表格
  - 多维表格（bitable）：每张数据表转成 Markdown 表格；附件里的图片下载成文件
  - 列出「我的云盘」和「我的文档库」里的全部文档
只用标准库，兼容 /usr/bin/python3（3.9）。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Callable

import feishu_user as fu

MAX_SHEET_ROWS = 2000      # 每个工作表最多读多少行
MAX_SHEET_COLS = 60        # 最多读多少列
MAX_BITABLE_RECORDS = 2000  # 每张数据表最多读多少条记录
MAX_IMAGES = 200           # 每篇最多下载多少张图
MAX_IMAGE_BYTES = 20 * 1024 * 1024
LIST_MAX_FILES = 2000      # 列云盘、文档库时最多看多少个

IMG_EXT_BY_TYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif",
                   "image/webp": ".webp", "image/bmp": ".bmp", "image/svg+xml": ".svg"}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")

# 文档块类型 → 内容字段名（飞书 docx 块的 block_type 编号）
HEADING_LEVEL = {3 + i: i + 1 for i in range(9)}  # 3..11 → 一级到九级标题
TYPE_KEY = {1: "page", 2: "text", 12: "bullet", 13: "ordered", 14: "code", 15: "quote", 17: "todo",
            18: "bitable", 19: "callout", 22: "divider", 23: "file", 24: "grid", 25: "grid_column",
            27: "image", 30: "sheet", 31: "table", 32: "table_cell", 34: "quote_container"}
TYPE_KEY.update({t: f"heading{n}" for t, n in HEADING_LEVEL.items()})


# ---------- 小工具 ----------

def sniff_image_ext(data: bytes, content_type: str = "") -> str:
    """看文件头判断图片格式，判断不了再看 Content-Type。"""
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
    if data.lstrip()[:5] in (b"<?xml", b"<svg ") or b"<svg" in data[:200]:
        return ".svg"
    return IMG_EXT_BY_TYPE.get(content_type.split(";")[0].strip().lower(), "")


def md_cell(s: str) -> str:
    """表格格子里的文字：去掉换行和竖线，免得把 Markdown 表格撑坏。"""
    return str(s).replace("|", "\\|").replace("\r", "").replace("\n", "<br>").strip()


def md_table(rows: list[list[str]]) -> str:
    rows = [r for r in rows if r is not None]
    if not rows:
        return "（空）"
    width = max(len(r) for r in rows) or 1
    rows = [[md_cell(c) for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s or "A"


def trim_grid(values: list[list]) -> list[list[str]]:
    """去掉表格末尾的空行和右边的空列。"""
    rows = [[cell_text(c) for c in (r or [])] for r in values]
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    width = 0
    for r in rows:
        for i, c in enumerate(r):
            if c.strip():
                width = max(width, i + 1)
    return [r[:width] for r in rows]


def cell_text(v) -> str:
    """电子表格 / 多维表格里各种形状的值 → 文字。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, list):
        return "、".join(t for t in (cell_text(x) for x in v) if t) if not all(
            isinstance(x, dict) and x.get("type") == "text" for x in v) else "".join(cell_text(x) for x in v)
    if isinstance(v, dict):
        for k in ("text", "name", "full_address", "link", "value", "en_name"):
            if v.get(k):
                return str(v[k])
        return ""
    return str(v)


# ---------- 文档：块 → Markdown ----------

def _elements(block: dict, key: str) -> str:
    """把一个块里的文字元素拼成一行 Markdown（加粗、行内代码、链接、@文档）。"""
    out = []
    for el in (block.get(key) or {}).get("elements") or []:
        if "text_run" in el:
            tr = el["text_run"]
            s = tr.get("content", "")
            style = tr.get("text_element_style") or {}
            link = (style.get("link") or {}).get("url")
            if link and s.strip():
                s = f"[{s}]({urllib.parse.unquote(link)})"
            elif style.get("inline_code") and s.strip():
                s = f"`{s}`"
            elif style.get("bold") and s.strip():
                s = f"**{s}**"
            out.append(s)
        elif "mention_doc" in el:
            md = el["mention_doc"]
            out.append(f"[{md.get('title') or '文档'}]({urllib.parse.unquote(md.get('url', ''))})")
        elif "mention_user" in el:
            out.append("@某人")
        elif "equation" in el:
            out.append((el["equation"].get("content") or "").strip())
        elif "reminder" in el:
            out.append("[提醒]")
        elif "file" in el:
            out.append("[文件]")
    return "".join(out)


class DocRenderer:
    """飞书文档的块是一棵树（page 是根）。按顺序走一遍，输出 Markdown。
    save_image(file_token) 负责下载图片，返回放在原位置的一行文字（![图N](路径) 或失败说明）。"""

    def __init__(self, blocks: list[dict], save_image: Callable[[str], str]):
        self.by_id = {b.get("block_id"): b for b in blocks}
        self.save_image = save_image
        self.lines: list[str] = []
        self.embedded: list[tuple[str, str]] = []  # 文档里内嵌的电子表格 / 多维表格（类型, token）

    def render(self) -> str:
        root = next((b for b in self.by_id.values() if b.get("block_type") == 1), None)
        if root is None:
            return ""
        self._children(root, 0)
        text = "\n".join(self.lines)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def _children(self, block: dict, depth: int) -> None:
        num = 0
        for cid in block.get("children") or []:
            child = self.by_id.get(cid)
            if not child:
                continue
            num = num + 1 if child.get("block_type") == 13 else 0
            self._block(child, depth, num)

    def _para(self, text: str, depth: int) -> None:
        if depth:
            self.lines.append("  " * depth + text)
        else:
            self.lines += [text, ""]

    def _block(self, b: dict, depth: int, num: int) -> None:
        t = b.get("block_type")
        key = TYPE_KEY.get(t, "")
        ind = "  " * depth
        if t in HEADING_LEVEL:  # 文档标题占 #，正文一级标题从 ## 开始
            self.lines += ["", "#" * min(HEADING_LEVEL[t] + 1, 6) + " " + _elements(b, key), ""]
            self._children(b, depth)
        elif t == 2:
            self._para(_elements(b, key), depth)
            self._children(b, depth + 1)
        elif t in (12, 13, 17):
            if t == 12:
                mark = "-"
            elif t == 13:
                mark = f"{num}."
            else:
                mark = "- [x]" if ((b.get("todo") or {}).get("style") or {}).get("done") else "- [ ]"
            self.lines.append(f"{ind}{mark} {_elements(b, key)}")
            self._children(b, depth + 1)
            if not depth:
                self.lines.append("") if self._next_is_not_list(b) else None
        elif t == 14:
            self.lines += ["", ind + "```", *[ind + ln for ln in _elements(b, key).split("\n")], ind + "```", ""]
        elif t == 15:
            self.lines += [ind + "> " + ln for ln in _elements(b, key).split("\n")] + [""]
        elif t == 22:
            self.lines += ["", "---", ""]
        elif t == 27:
            token = (b.get("image") or {}).get("token", "")
            self._para(self.save_image(token) if token else "[图片：没有文件编号]", depth)
        elif t == 23:
            self._para(f"[附件] {(b.get('file') or {}).get('name') or ''}".strip(), depth)
        elif t == 30:
            token = (b.get("sheet") or {}).get("token", "")
            self.embedded.append(("sheet", token.split("_")[0]))
            self._para(f"[内嵌电子表格 {token.split('_')[0]}，内容另存为「表格-」文件]", depth)
        elif t == 18:
            token = (b.get("bitable") or {}).get("token", "")
            self.embedded.append(("bitable", token.split("_")[0]))
            self._para(f"[内嵌多维表格 {token.split('_')[0]}，内容另存为「多维表格-」文件]", depth)
        elif t == 31:
            self._table(b)
        elif b.get("children"):  # 高亮块、分栏、引用容器等：只是外壳，照常输出里面的内容
            self._children(b, depth)

    def _next_is_not_list(self, b: dict) -> bool:
        parent = self.by_id.get(b.get("parent_id")) or {}
        sibs = parent.get("children") or []
        try:
            nxt = self.by_id.get(sibs[sibs.index(b.get("block_id")) + 1]) or {}
        except (ValueError, IndexError):
            return True
        return nxt.get("block_type") not in (12, 13, 17)

    def _cell_text(self, cell_id: str) -> str:
        parts = []

        def walk(bid: str):
            b = self.by_id.get(bid) or {}
            t = b.get("block_type")
            if t == 27:
                token = (b.get("image") or {}).get("token", "")
                parts.append(self.save_image(token) if token else "[图片]")
            elif t in TYPE_KEY and TYPE_KEY[t] in b and (b.get(TYPE_KEY[t]) or {}).get("elements") is not None:
                parts.append(_elements(b, TYPE_KEY[t]))
            for c in b.get("children") or []:
                walk(c)
        for c in (self.by_id.get(cell_id) or {}).get("children") or []:
            walk(c)
        return "\n".join(p for p in parts if p)

    def _table(self, b: dict) -> None:
        tb = b.get("table") or {}
        prop = tb.get("property") or {}
        cols = int(prop.get("column_size") or 0)
        cells = tb.get("cells") or b.get("children") or []
        if not cols:
            return
        rows = [[self._cell_text(c) for c in cells[i:i + cols]] for i in range(0, len(cells), cols)]
        self.lines += ["", md_table(rows), ""]


def read_docx(token: str, save_image: Callable[[str], str], http=fu.http_json) -> tuple[str, list]:
    """读一篇文档的全部块，返回 (Markdown 正文, 内嵌的表格列表)。"""
    blocks = list(fu.paginate(f"/docx/v1/documents/{token}/blocks",
                              {"page_size": 500, "document_revision_id": -1}, http))
    r = DocRenderer(blocks, save_image)
    return r.render(), r.embedded


# ---------- 电子表格 ----------

def read_sheet(token: str, http=fu.http_json) -> tuple[str, str]:
    """返回 (标题, Markdown)。每个工作表一节。"""
    title = (fu.api_get(f"/sheets/v3/spreadsheets/{token}", None, http).get("spreadsheet") or {}).get("title") \
        or "未命名表格"
    parts = []
    for sh in fu.api_get(f"/sheets/v3/spreadsheets/{token}/sheets/query", None, http).get("sheets") or []:
        name = sh.get("title") or sh.get("sheet_id", "")
        if sh.get("resource_type") not in (None, "", "sheet"):
            parts.append(f"## {name}\n\n（{sh.get('resource_type')} 类型的工作表，暂不读取）")
            continue
        g = sh.get("grid_properties") or {}
        nrow, ncol = int(g.get("row_count") or 0), int(g.get("column_count") or 0)
        if not nrow or not ncol:
            parts.append(f"## {name}\n\n（空）")
            continue
        rows, cols = min(nrow, MAX_SHEET_ROWS), min(ncol, MAX_SHEET_COLS)
        rng = urllib.parse.quote(f"{sh['sheet_id']}!A1:{col_letter(cols)}{rows}", safe="")
        vr = fu.api_get(f"/sheets/v2/spreadsheets/{token}/values/{rng}",
                        {"valueRenderOption": "ToString", "dateTimeRenderOption": "FormattedString"}, http)
        grid = trim_grid((vr.get("valueRange") or {}).get("values") or [])
        note = ""
        if nrow > rows or ncol > cols:
            note = f"\n\n（这个工作表有 {nrow} 行 {ncol} 列，只读了前 {rows} 行、前 {cols} 列）"
        parts.append(f"## {name}\n\n" + (md_table(grid) if grid else "（空）") + note)
    return title, "\n\n".join(parts) or "（没有工作表）"


# ---------- 多维表格 ----------

DATE_FIELD_TYPES = {5, 1001, 1002}  # 日期、创建时间、修改时间（值是毫秒时间戳）


def _bitable_value(v, ftype: int, save_attachment: Callable[[dict], str]) -> str:
    from datetime import datetime
    if v is None:
        return ""
    if ftype in DATE_FIELD_TYPES and isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d %H:%M")
    if isinstance(v, list) and v and isinstance(v[0], dict) and "file_token" in v[0]:
        return " ".join(save_attachment(a) for a in v)
    return cell_text(v)


def read_bitable(app_token: str, save_attachment: Callable[[dict], str], http=fu.http_json) -> tuple[str, str, str]:
    """返回 (标题, 版本号, Markdown)。每张数据表一节；附件里的图片交给 save_attachment 下载。"""
    app = fu.api_get(f"/bitable/v1/apps/{app_token}", None, http).get("app") or {}
    title, rev = app.get("name") or "未命名多维表格", str(app.get("revision", ""))
    parts = []
    for tb in fu.paginate(f"/bitable/v1/apps/{app_token}/tables", {"page_size": 100}, http):
        tid, name = tb.get("table_id", ""), tb.get("name") or tb.get("table_id", "")
        fields = [(f.get("field_name", ""), int(f.get("type") or 0))
                  for f in fu.paginate(f"/bitable/v1/apps/{app_token}/tables/{tid}/fields", {"page_size": 100}, http)]
        rows = [[f for f, _ in fields]]
        n = 0
        for rec in fu.paginate(f"/bitable/v1/apps/{app_token}/tables/{tid}/records", {"page_size": 500}, http,
                               limit=MAX_BITABLE_RECORDS):
            fv = rec.get("fields") or {}
            rows.append([_bitable_value(fv.get(f), t, save_attachment) for f, t in fields])
            n += 1
        note = f"\n\n（只读了前 {MAX_BITABLE_RECORDS} 条）" if n >= MAX_BITABLE_RECORDS else ""
        parts.append(f"## {name}（{n} 条）\n\n" + md_table(rows) + note)
    return title, rev, "\n\n".join(parts) or "（没有数据表）"


# ---------- 图片下载 ----------

def media_download(file_token_or_url: str, http=fu.http_json, fetch=None) -> tuple[bytes, str]:
    """下载云文档里的素材（图片、附件）。可以传 file_token，也可以传多维表格附件自带的下载地址。"""
    path = file_token_or_url if file_token_or_url.startswith("http") \
        else f"/drive/v1/medias/{file_token_or_url}/download"
    return fu.api_get_bytes(path, None, http, fetch or fu.http_bytes)


# ---------- 列出我的云盘、我的文档库 ----------

def list_drive(http=fu.http_json) -> list[dict]:
    """「我的空间」（云盘）里的全部文件，递归进文件夹；快捷方式换成它指向的文件。"""
    out, stack, seen = [], [""], set()
    while stack and len(out) < LIST_MAX_FILES:
        folder = stack.pop()
        params = {"page_size": 200}
        if folder:
            params["folder_token"] = folder
        while True:
            data = fu.api_get("/drive/v1/files", params, http)
            for f in data.get("files") or []:
                typ, tok = f.get("type"), f.get("token")
                if typ == "folder":
                    if tok and tok not in seen:
                        seen.add(tok)
                        stack.append(tok)
                    continue
                if typ == "shortcut":
                    si = f.get("shortcut_info") or {}
                    typ, tok = si.get("target_type"), si.get("target_token")
                if tok:
                    out.append({"kind": typ, "token": tok, "title": f.get("name", ""), "where": "云盘"})
            if not data.get("has_more") or not data.get("next_page_token"):
                break
            params["page_token"] = data["next_page_token"]
    return out


def list_my_library(http=fu.http_json) -> list[dict]:
    """「我的文档库」（个人知识库）里的全部节点，递归子节点。"""
    space = fu.api_get("/wiki/v2/spaces/my_library", None, http).get("space") or {}
    sid = space.get("space_id")
    if not sid:
        return []
    out, stack = [], [""]
    while stack and len(out) < LIST_MAX_FILES:
        parent = stack.pop()
        params = {"page_size": 50}
        if parent:
            params["parent_node_token"] = parent
        for n in fu.paginate(f"/wiki/v2/spaces/{sid}/nodes", params, http):
            out.append({"kind": "wiki", "token": n.get("node_token", ""), "title": n.get("title", ""),
                        "obj_type": n.get("obj_type", ""), "obj_token": n.get("obj_token", ""), "where": "文档库"})
            if n.get("has_child"):
                stack.append(n.get("node_token", ""))
    return out
