"""把原文里图片、PDF、Word、PPT 中的文字提取出来，存成旁边的同名 .txt，让 search.py 也能搜到。

为什么要它：search.py 只索引文字文件（.md、.txt…），图片里的字、PDF 和 Word 的正文原来都搜不到。
做法：在 raw/ 下每个这类文件旁边写一个「原文件名 + .txt」（如 01.png.txt、课件.pdf.txt），
开头写明原文件和所在文档，后面是提取出来的文字。原文件不动，raw/ 依旧只追加。

Windows 上：
- 图片（png/jpg/gif/webp/bmp/tif）：Windows 自带的识字功能（Windows.Media.Ocr，中文版 Windows 自带中文），见 ops/windows/ocr.ps1
- PDF：pypdf 取文字层；某页字很少（扫描版）就用 Windows 自带的 PDF 功能把这一页画成图片再识字，取字多的
- Word（.docx）、PPT（.pptx）：直接读文件里的文字；.doc / .rtf 用 LibreOffice 转成文字
（Linux 上用 tesseract、poppler；Mac 版用 macOS 自带的 Vision 和 PDFKit。对外的结果格式都一样。）
每轮最多跑 DEFAULT_BUDGET_SEC 秒，做不完的下一轮接着做（任务计划程序每 10 分钟）。

用法：
  python ops/extract_text.py            # 做一轮
  python ops/extract_text.py --status   # 只统计：还有多少没做
配置（环境变量）：AGENTOS_RAW 原文根目录（默认是项目文件夹下的 spaces/me/raw）
状态：~/.agentos/extract_text.json（每个文件做过没有、结果如何，用来跳过和重试）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from html import unescape
from pathlib import Path

STATE_DIR = Path(os.environ.get("AGENTOS_STATE_DIR", Path.home() / ".agentos"))
IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
OCR_PS1 = Path(__file__).resolve().parent / "windows" / "ocr.ps1"
CJK_GAP = re.compile(r"(?<=[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef])[ \t]+(?=[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef])")
CJK_PUNCT_GAP = re.compile(r"[ \t]+(?=[\u3000-\u303f\uff00-\uffef])|(?<=[\u3000-\u303f\uff00-\uffef])[ \t]+")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}  # tesseract 读不了 heic
PDF_EXTS = {".pdf"}
ZIP_EXTS = {".docx", ".pptx"}
TEXTUTIL_EXTS = {".doc", ".rtf"}
ALL_EXTS = IMAGE_EXTS | PDF_EXTS | ZIP_EXTS | TEXTUTIL_EXTS

MAX_FILE_BYTES = 50 * 1024 * 1024
MIN_IMAGE_BYTES = 1024           # 太小的多半是图标、分割线，不识
DEFAULT_BUDGET_SEC = 240
IMAGE_BATCH = 20                 # 一批识多少张图（每批做完检查一次时间预算）
OCR_TIMEOUT_SEC = 120             # 一张图、一页 PDF 最多识多久
TESS_LANG = "chi_sim+chi_tra+eng"
MIN_PAGE_TEXT = 200              # PDF 某页文字层少于这么多字，就再识一遍字，取字多的（扫描版常常只有页眉页码是文字）
MAX_PDF_PAGES = 300
RENDER_DPI = 200
FAIL_RETRY_SEC = 86400           # 失败的一天后再试
SIDECAR_SUFFIX = ".txt"


def raw_dir() -> Path:
    return Path(os.environ.get("AGENTOS_RAW", "") or Path(__file__).resolve().parent.parent / "spaces" / "me" / "raw")


def state_path() -> Path:
    return STATE_DIR / "extract_text.json"


def load_state() -> dict:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("files", {})  # 相对 raw/ 的路径 -> {"sig": [大小, 修改时间], "status": ok|empty|fail, "at", "error"}
    return st


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, ensure_ascii=False, indent=0)
        os.chmod(tmp, 0o600)
        os.replace(tmp, state_path())
    except BaseException:
        os.unlink(tmp)
        raise


def sidecar(p: Path) -> Path:
    # 去掉开头的点：微信偶尔存成 .archivetemp…pdf 这种隐藏文件名，search.py 会跳过点开头的文件
    return p.with_name(p.name.lstrip(".") + SIDECAR_SUFFIX)


def rel_from_root(p: Path, base: Path) -> str:
    """出处写法：从项目文件夹根目录算起（spaces/me/raw/…），和其他原文一致。"""
    if base.parent.parent.name == "spaces":
        return p.relative_to(base.parents[2]).as_posix()
    return str(p)


def owner_doc(p: Path) -> Path | None:
    """图片在「<文档名>-图片/」文件夹里时，找到它所在的文档（飞书文档、公众号文章）。"""
    d = p.parent
    if d.name.endswith("-图片"):
        doc = d.with_name(d.name[: -len("-图片")] + ".md")
        if doc.exists():
            return doc
    return None


def signature(p: Path) -> list:
    st = p.stat()
    return [st.st_size, int(st.st_mtime)]


def candidates(base: Path, st: dict, now: float) -> list[Path]:
    """还没提取、改过、或失败满一天的文件。收件箱和聊天里的文件排前面，然后按新到旧。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            ext = p.suffix.lower()
            if ext not in ALL_EXTS or name == ".DS_Store":
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES or (ext in IMAGE_EXTS and size < MIN_IMAGE_BYTES):
                continue
            rec = st["files"].get(p.relative_to(base).as_posix())
            if rec and rec.get("sig") == signature(p):
                if rec["status"] == "ok" and sidecar(p).exists():
                    continue
                if rec["status"] == "empty":
                    continue
                if rec["status"] == "fail" and now - rec.get("at", 0) < FAIL_RETRY_SEC:
                    continue
            out.append(p)

    def rank(p: Path):
        parts = p.relative_to(base).parts
        first = 0 if parts[0] == "inbox" or parts[:2] == ("wechat", "files") else 1
        kind = 0 if p.suffix.lower() not in IMAGE_EXTS else 1
        return (first, kind, -p.stat().st_mtime)

    return sorted(out, key=rank)


# ---------- 各种格式怎么取字 ----------

def _xml_text(xml: str, para_tag: str, text_tag: str) -> str:
    """从 Office 的 XML 里按段落取文字：<w:p>…<w:t>字</w:t>…</w:p>。"""
    paras = []
    for para in re.findall(rf"<{para_tag}[ >].*?</{para_tag}>", xml, re.S):
        s = "".join(re.findall(rf"<{text_tag}(?: [^>]*)?>(.*?)</{text_tag}>", para, re.S))
        s = unescape(s).strip()
        if s:
            paras.append(s)
    return "\n".join(paras)


def read_docx(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        parts = ["word/document.xml"] + sorted(n for n in z.namelist()
                                               if re.match(r"word/(header|footer|footnotes)\d*\.xml$", n))
        texts = [_xml_text(z.read(n).decode("utf-8", "replace"), "w:p", "w:t") for n in parts if n in z.namelist()]
    return "\n".join(t for t in texts if t)


def read_pptx(p: Path) -> list[tuple[int, str]]:
    with zipfile.ZipFile(p) as z:
        slides = sorted((int(m.group(1)), n) for n in z.namelist()
                        if (m := re.match(r"ppt/slides/slide(\d+)\.xml$", n)))
        out = []
        for i, name in slides:
            text = _xml_text(z.read(name).decode("utf-8", "replace"), "a:p", "a:t")
            notes = f"ppt/notesSlides/notesSlide{i}.xml"
            if notes in z.namelist():
                nt = _xml_text(z.read(notes).decode("utf-8", "replace"), "a:p", "a:t")
                if nt:
                    text += "\n（备注）" + nt
            out.append((i, text))
    return out


def _run(cmd: list[str], timeout: int = OCR_TIMEOUT_SEC) -> str:
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"{Path(cmd[0]).name} 失败：{err[:300] or r.returncode}")
    return r.stdout.decode("utf-8", "replace")


def read_office_text(p: Path) -> str:
    """.doc / .rtf：LibreOffice 转成纯文字。用临时的配置目录，免得和 Agent 出文件时抢同一个。"""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            from make_doc import find_soffice
            soffice = find_soffice()
        except SystemExit as e:  # 没装 LibreOffice
            raise RuntimeError(str(e)) from None
        _run([soffice, f"-env:UserInstallation={(Path(tmp) / 'profile').as_uri()}", "--headless",
              "--convert-to", "txt:Text (encoded):UTF8", "--outdir", tmp, str(p)], timeout=180)
        out = Path(tmp) / (p.stem + ".txt")
        if not out.exists():
            raise RuntimeError("LibreOffice 没有转出文字")
        return out.read_text(encoding="utf-8", errors="replace")


def _count(s: str) -> int:
    return len(re.sub(r"\s", "", s))


def tesseract(path: Path) -> str:
    return _run(["tesseract", str(path), "stdout", "-l", TESS_LANG]).strip()


def pdf_pages(p: Path) -> tuple[list[dict], int]:
    """每页先取文字层；字太少就把这一页渲染成图片识字，取字多的那个。"""
    m = re.search(r"^Pages:\s+(\d+)", _run(["pdfinfo", str(p)], timeout=60), re.M)
    total = int(m.group(1)) if m else 0
    layers = _run(["pdftotext", "-enc", "UTF-8", str(p), "-"], timeout=180).split("\f")
    pages = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(min(total, MAX_PDF_PAGES)):
            layer = layers[i].strip() if i < len(layers) else ""
            if _count(layer) >= MIN_PAGE_TEXT:
                pages.append({"n": i + 1, "how": "text", "text": layer})
                continue
            prefix = Path(tmp) / f"p{i + 1}"
            _run(["pdftoppm", "-f", str(i + 1), "-l", str(i + 1), "-r", str(RENDER_DPI),
                  "-png", "-singlefile", str(p), str(prefix)])
            ocr = tesseract(prefix.with_suffix(".png"))
            pages.append({"n": i + 1, "how": "ocr", "text": ocr} if _count(ocr) > _count(layer)
                         else {"n": i + 1, "how": "text", "text": layer})
    return pages, total


def _ocr_tesseract(paths: list[Path]) -> dict[str, dict]:
    """Linux：tesseract 识字、poppler 读 PDF。"""
    out = {}
    for p in paths:
        try:
            if p.suffix.lower() in PDF_EXTS:
                pages, total = pdf_pages(p)
                out[str(p)] = {"path": str(p), "ok": True, "kind": "pdf", "pages": pages, "total": total}
            else:
                out[str(p)] = {"path": str(p), "ok": True, "kind": "ocr", "text": tesseract(p)}
        except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as e:
            out[str(p)] = {"path": str(p), "ok": False, "error": f"{type(e).__name__}: {e}"}
    return out


# ---------- Windows：自带的识字（ocr.ps1）+ pypdf ----------

def win_ocr(request: dict, timeout: int = 600) -> dict:
    """调 ocr.ps1。请求、结果都走 UTF-8 的 JSON 临时文件，避开中文 Windows 命令行的编码问题。"""
    with tempfile.TemporaryDirectory() as tmp:
        req, res = Path(tmp) / "in.json", Path(tmp) / "out.json"
        req.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        _run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(OCR_PS1),
              "-In", str(req), "-Out", str(res)], timeout=timeout)
        d = json.loads(res.read_text(encoding="utf-8-sig"))
    items = d.get("items") or []
    d["items"] = [items] if isinstance(items, dict) else items  # PowerShell 只有一项时可能不是数组
    return d


def tidy_cjk(text: str) -> str:
    """Windows 识字会在每个汉字之间加空格（「报 价 单 ： 3」）：去掉汉字之间、中文标点前后的空格。"""
    return CJK_PUNCT_GAP.sub("", CJK_GAP.sub("", text or ""))


def pdf_text_layers(p: Path) -> tuple[list[str], int]:
    """每页的文字层（最多 MAX_PDF_PAGES 页）和总页数。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("没装 pypdf（安装脚本会装；手动装：python -m pip install pypdf）") from None
    reader = PdfReader(str(p))
    out = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        try:
            out.append((page.extract_text() or "").strip())
        except Exception:  # noqa: BLE001 — 个别页解析出错，当成没有文字层，交给识字
            out.append("")
    return out, len(reader.pages)


def _ocr_windows(paths: list[Path], ocr=None) -> dict[str, dict]:
    ocr = ocr or win_ocr
    out: dict[str, dict] = {}
    images = [p for p in paths if p.suffix.lower() not in PDF_EXTS]
    if images:
        try:
            got = {i.get("path"): i for i in ocr({"images": [str(p) for p in images]})["items"]}
        except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as e:
            got = {str(p): {"ok": False, "error": f"{type(e).__name__}: {e}"} for p in images}
        for p in images:
            i = got.get(str(p)) or {"ok": False, "error": "没有返回结果"}
            out[str(p)] = ({"path": str(p), "ok": True, "kind": "ocr", "text": tidy_cjk(i.get("text", ""))}
                           if i.get("ok") else {"path": str(p), "ok": False, "error": i.get("error", "")})
    for p in paths:
        if p.suffix.lower() not in PDF_EXTS:
            continue
        try:
            layers, total = pdf_text_layers(p)
            short = [n for n, t in enumerate(layers, 1) if _count(t) < MIN_PAGE_TEXT]
            ocrd = {}
            if short:
                d = ocr({"pdf": str(p), "pages": short})
                ocrd = {i["page"]: tidy_cjk(i.get("text", "")) for i in d["items"] if i.get("ok")}
            pages = []
            for n, layer in enumerate(layers, 1):
                o = ocrd.get(n, "")
                pages.append({"n": n, "how": "ocr", "text": o} if _count(o) > _count(layer)
                             else {"n": n, "how": "text", "text": layer})
            out[str(p)] = {"path": str(p), "ok": True, "kind": "pdf", "pages": pages, "total": total}
        except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as e:
            out[str(p)] = {"path": str(p), "ok": False, "error": f"{type(e).__name__}: {e}"}
        except Exception as e:  # noqa: BLE001 — pypdf 读坏文件会抛各种异常
            out[str(p)] = {"path": str(p), "ok": False, "error": f"PDF 读不了：{type(e).__name__}: {e}"}
    return out


def ocr_extract(paths: list[Path]) -> dict[str, dict]:
    """图片识字、PDF 取文字。返回 {路径: 结果}：
    图片 {"ok", "kind": "ocr", "text"}；PDF {"ok", "kind": "pdf", "pages": [{"n", "how", "text"}], "total"}；
    失败 {"ok": False, "error"}。"""
    return _ocr_windows(paths) if IS_WIN else _ocr_tesseract(paths)


# ---------- 写 .txt ----------

def write_sidecar(p: Path, base: Path, how: str, body: str) -> None:
    lines = [f"# 「{p.name}」里的文字（自动提取" + ("，识字可能有错字" if "识字" in how else "") + "）", "",
             f"- 原文件：{rel_from_root(p, base)}"]
    doc = owner_doc(p)
    if doc:
        lines.append(f"- 所在文档：{rel_from_root(doc, base)}")
    lines += [f"- 提取方式：{how}", f"- 提取时间：{datetime.now():%Y-%m-%d %H:%M}", "", "---", "", body.strip(), ""]
    out = sidecar(p)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.chmod(tmp, 0o644)  # Agent 要能读
        os.replace(tmp, out)
    except BaseException:
        os.unlink(tmp)
        raise


def pdf_body(d: dict) -> tuple[str, str]:
    pages = d.get("pages") or []
    hows = {pg["how"] for pg in pages if pg.get("text", "").strip()}
    how = {frozenset({"text"}): "PDF 文字层", frozenset({"ocr"}): "PDF 扫描页识字"}.get(
        frozenset(hows), "PDF 文字层 + 扫描页识字")
    parts = [f"## 第 {pg['n']} 页\n\n{pg['text'].strip()}" for pg in pages if pg.get("text", "").strip()]
    try:
        total = int(d.get("total", len(pages)))
    except (TypeError, ValueError):
        total = len(pages)
    if total > len(pages):
        parts.append(f"（共 {total} 页，只提取了前 {len(pages)} 页）")
    return how, "\n\n".join(parts)


def process(base: Path, st: dict, budget_sec: float = DEFAULT_BUDGET_SEC, osa=ocr_extract) -> dict:
    now = time.time()
    todo = candidates(base, st, now)
    counts = {"ok": 0, "empty": 0, "fail": 0, "img": 0, "pdf": 0, "doc": 0}
    deadline = time.monotonic() + budget_sec
    done = 0

    def record(p: Path, status: str, error: str = "") -> None:
        rec = {"sig": signature(p), "status": status, "at": int(time.time())}
        if error:
            rec["error"] = error[:300]
        st["files"][p.relative_to(base).as_posix()] = rec
        counts[status] += 1

    def finish(p: Path, how: str, body: str, kind: str) -> None:
        if body.strip():
            write_sidecar(p, base, how, body)
            record(p, "ok")
            counts[kind] += 1
        else:
            record(p, "empty")

    errors = (subprocess.TimeoutExpired, zipfile.BadZipFile, OSError, RuntimeError, ValueError, KeyError)
    i = 0
    while i < len(todo) and time.monotonic() < deadline:
        p = todo[i]
        ext = p.suffix.lower()
        if ext in IMAGE_EXTS:  # 连着的图片一批一批识，每批做完检查一次时间
            batch = []
            while i < len(todo) and todo[i].suffix.lower() in IMAGE_EXTS and len(batch) < IMAGE_BATCH:
                batch.append(todo[i])
                i += 1
            done += len(batch)
            try:
                res = osa(batch)
            except errors as e:  # 整批出错（比如超时）：这一批都记失败，一天后再试
                for b in batch:
                    record(b, "fail", f"{type(e).__name__}: {e}")
                save_state(st)
                continue
            for b in batch:
                d = res.get(str(b))
                if not d or not d.get("ok"):
                    record(b, "fail", (d or {}).get("error", "没有返回结果"))
                else:
                    finish(b, "图片识字（Windows 自带）" if IS_WIN else "图片识字（tesseract）", d.get("text", ""), "img")
            save_state(st)
            continue
        i += 1
        done += 1
        try:
            if ext in PDF_EXTS:
                d = osa([p]).get(str(p))
                if not d or not d.get("ok"):
                    record(p, "fail", (d or {}).get("error", "没有返回结果"))
                else:
                    how, body = pdf_body(d)
                    finish(p, how, body, "pdf")
            elif ext == ".docx":
                finish(p, "Word 正文", read_docx(p), "doc")
            elif ext == ".pptx":
                body = "\n\n".join(f"## 第 {n} 页\n\n{t}" for n, t in read_pptx(p) if t.strip())
                finish(p, "PPT 每页文字", body, "doc")
            else:
                finish(p, "LibreOffice 转文字", read_office_text(p), "doc")
        except errors as e:
            record(p, "fail", f"{type(e).__name__}: {e}")
        save_state(st)  # 做一点存一点，中途被停也不白做
    counts["left"] = len(todo) - done
    counts["attempted"] = done
    return counts


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base = raw_dir()
    if not base.is_dir():
        print(f"原文目录不存在：{base}")
        return 0
    st = load_state()
    if "--status" in args:
        todo = candidates(base, st, time.time())
        n_ok = sum(1 for r in st["files"].values() if r["status"] == "ok")
        print(f"已提取 {n_ok} 个，待提取 {len(todo)} 个。")
        return 0
    c = process(base, st)
    msg = (f"提取文字：新增 {c['ok']} 个（图片 {c['img']}、PDF {c['pdf']}、Word/PPT {c['doc']}），"
           f"没有文字 {c['empty']} 个，失败 {c['fail']} 个")
    if c["left"]:
        msg += f"，{c['left']} 个留到下一轮"
    print(msg)
    # 只有「试了好几个、全都失败」才算任务失败（多半是识字功能本身坏了），个别坏文件不告警
    return 2 if c["attempted"] >= 3 and c["fail"] == c["attempted"] else 0


if __name__ == "__main__":
    sys.exit(main())
