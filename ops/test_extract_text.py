"""提取文字的离线测试：识字用假结果（不调 tesseract），Word / PPT 用临时造的文件。

运行：/usr/bin/python3 -m unittest ops/test_extract_text.py -v
"""

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import extract_text as et  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 2000


def make_docx(p: Path, paras: list[str]) -> None:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>' for t in paras)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", f'<w:document><w:body>{body}</w:body></w:document>')


def make_pptx(p: Path, slides: list[str], note: str = "") -> None:
    with zipfile.ZipFile(p, "w") as z:
        for i, t in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", f"<p:sld><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:sld>")
        if note:
            z.writestr("ppt/notesSlides/notesSlide1.xml", f"<p:notes><a:p><a:r><a:t>{note}</a:t></a:r></a:p></p:notes>")


class FakeOsa:
    """假的 extract_text.js：文件名里带 blank 的图没有字，带 bad 的识别失败。"""

    def __init__(self):
        self.calls = []

    def __call__(self, paths):
        self.calls.append([p.name for p in paths])
        out = {}
        for p in paths:
            if p.suffix == ".pdf":
                out[str(p)] = {"path": str(p), "ok": True, "kind": "pdf", "total": 2, "pages": [
                    {"n": 1, "how": "text", "text": "第一章 随机过程"},
                    {"n": 2, "how": "ocr", "text": "平稳随机过程的判定"}]}
            elif "bad" in p.name:
                out[str(p)] = {"path": str(p), "ok": False, "error": "识字失败"}
            else:
                out[str(p)] = {"path": str(p), "ok": True, "kind": "ocr",
                               "text": "" if "blank" in p.name else f"{p.name} 里的字：报价 3 万"}
        return out


class ExtractTextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.raw = root / "spaces/me/raw"   # 和运行副本一样的层级，出处才会写成 spaces/me/raw/…
        self.st = {"files": {}}
        et.STATE_DIR = root / "state"
        doc = self.raw / "feishu/2026-11-06/文档-报价单-abc123.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# 报价单\n![图1](spaces/me/raw/…/01.png)\n", encoding="utf-8")
        imgs = doc.with_name("文档-报价单-abc123-图片")
        imgs.mkdir()
        for name in ("01.png", "02-blank.png", "03-bad.png"):
            (imgs / name).write_bytes(PNG)
        (imgs / "icon.png").write_bytes(b"\x89PNG tiny")  # 太小，不识
        files = self.raw / "wechat/files/2026-11-07"
        files.mkdir(parents=True)
        (files / ".archivetemp通信原理习题.pdf").write_bytes(b"%PDF-1.4 fake")
        make_docx(files / "方案.docx", ["一、项目背景", "预算 &lt;50 万&gt;"])
        make_pptx(files / "课件.pptx", ["Z 变换", "收敛域"], note="期末必考")

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, osa=None):
        return et.process(self.raw, self.st, budget_sec=60, osa=osa or FakeOsa())

    def test_extracts_everything_and_writes_sidecars(self):
        osa = FakeOsa()
        c = self.run_once(osa)
        self.assertEqual((c["ok"], c["empty"], c["fail"], c["left"]), (4, 1, 1, 0))
        self.assertEqual((c["img"], c["pdf"], c["doc"]), (1, 1, 2))
        # 聊天里的文件排在图片前面；图片一批一批识；太小的图标不识
        self.assertEqual(osa.calls[0], [".archivetemp通信原理习题.pdf"])
        self.assertEqual(sorted(osa.calls[-1]), ["01.png", "02-blank.png", "03-bad.png"])

        img_txt = self.raw / "feishu/2026-11-06/文档-报价单-abc123-图片/01.png.txt"
        t = img_txt.read_text(encoding="utf-8")
        self.assertIn("- 原文件：spaces/me/raw/feishu/2026-11-06/文档-报价单-abc123-图片/01.png", t)
        self.assertIn("- 所在文档：spaces/me/raw/feishu/2026-11-06/文档-报价单-abc123.md", t)
        self.assertIn("识字可能有错字", t)
        self.assertIn("报价 3 万", t)
        self.assertEqual(oct(img_txt.stat().st_mode & 0o777), "0o644")
        self.assertFalse((img_txt.parent / "02-blank.png.txt").exists())  # 没字的不写

        # 隐藏文件名的 PDF：.txt 去掉开头的点，检索才不会跳过；按页分节
        pdf_txt = self.raw / "wechat/files/2026-11-07/archivetemp通信原理习题.pdf.txt"
        t = pdf_txt.read_text(encoding="utf-8")
        self.assertIn("## 第 1 页\n\n第一章 随机过程", t)
        self.assertIn("## 第 2 页\n\n平稳随机过程的判定", t)
        self.assertIn("PDF 文字层 + 扫描页识字", t)

        t = (self.raw / "wechat/files/2026-11-07/方案.docx.txt").read_text(encoding="utf-8")
        self.assertIn("一、项目背景\n预算 <50 万>", t)
        self.assertNotIn("识字可能有错字", t)
        t = (self.raw / "wechat/files/2026-11-07/课件.pptx.txt").read_text(encoding="utf-8")
        self.assertIn("## 第 1 页\n\nZ 变换\n（备注）期末必考", t)
        self.assertIn("## 第 2 页\n\n收敛域", t)

        # 状态存下来了；再跑一轮什么都不做（失败的要等一天）
        self.assertTrue(et.state_path().exists())
        osa2 = FakeOsa()
        c = self.run_once(osa2)
        self.assertEqual((c["attempted"], osa2.calls), (0, []))

    def test_redo_when_changed_or_sidecar_deleted_or_fail_expired(self):
        self.run_once()
        imgs = self.raw / "feishu/2026-11-06/文档-报价单-abc123-图片"
        (imgs / "01.png.txt").unlink()                       # .txt 被删了：重做
        (imgs / "02-blank.png").write_bytes(PNG + b"more")   # 原图换了：重做
        self.st["files"]["feishu/2026-11-06/文档-报价单-abc123-图片/03-bad.png"]["at"] -= et.FAIL_RETRY_SEC + 1
        osa = FakeOsa()
        self.run_once(osa)
        self.assertEqual(sorted(osa.calls[0]), ["01.png", "02-blank.png", "03-bad.png"])
        self.assertTrue((imgs / "01.png.txt").exists())

    def test_budget_leaves_rest_for_next_round(self):
        c = et.process(self.raw, self.st, budget_sec=0, osa=FakeOsa())
        self.assertEqual(c["attempted"], 0)
        self.assertEqual(c["left"], 6)

    def test_whole_batch_error_marks_batch_failed(self):
        def broken(paths):
            if paths[0].suffix == ".pdf":
                return FakeOsa()(paths)
            raise RuntimeError("识字工具失败")
        c = self.run_once(broken)
        self.assertEqual(c["fail"], 3)
        self.assertEqual(c["pdf"], 1)  # PDF 是单独调用的，不受图片那批出错影响

    def test_search_finds_text_in_images(self):
        """提取出来的 .txt 进了检索：搜图里的字能搜到。"""
        import search
        self.run_once()
        space = self.raw.parent
        db = search.open_index(space)
        try:
            search.refresh(db, space)
            rows, _total = search.search(db, ["报价"])
            paths = [r[0] for r in rows]
            self.assertIn("raw/feishu/2026-11-06/文档-报价单-abc123-图片/01.png.txt", paths)
            rows, _total = search.search(db, ["平稳随机过程"])
            self.assertEqual([r[0] for r in rows], ["raw/wechat/files/2026-11-07/archivetemp通信原理习题.pdf.txt"])
        finally:
            db.close()


class LinuxBackendTest(unittest.TestCase):
    """Linux 版的识字后端：pdfinfo / pdftotext / pdftoppm / tesseract 用假输出代替。"""

    def setUp(self):
        self.calls = []
        self._run = et._run

        def fake_run(cmd, timeout=0):
            self.calls.append(cmd[0])
            if cmd[0] == "pdfinfo":
                return "Title: x\nPages:          3\n"
            if cmd[0] == "pdftotext":   # 第 1 页文字很多；第 2 页只有页眉；第 3 页空（扫描页）
                return "正文" * 150 + "\f湖南大学 习题集 1\f\f"
            if cmd[0] == "pdftoppm":
                Path(cmd[-1] + ".png").write_bytes(b"png")
                return ""
            if cmd[0] == "tesseract":
                if cmd[1].endswith("bad.png"):
                    raise RuntimeError("tesseract 失败：读不了")
                return "识出来的题目：信源、发送设备、信道、接收设备和信宿\n"
            raise AssertionError(cmd)
        et._run = fake_run
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        et._run = self._run
        self.tmp.cleanup()

    def test_pdf_pages_text_or_ocr(self):
        pdf = self.dir / "a.pdf"
        pdf.write_bytes(b"%PDF")
        r = et.ocr_extract([pdf])[str(pdf)]
        self.assertTrue(r["ok"])
        self.assertEqual(r["total"], 3)
        self.assertEqual([(pg["n"], pg["how"]) for pg in r["pages"]], [(1, "text"), (2, "ocr"), (3, "ocr")])
        self.assertIn("信源", r["pages"][1]["text"])
        self.assertEqual(self.calls.count("pdftoppm"), 2)   # 文字多的第 1 页不用识字

    def test_images_and_failures(self):
        ok, bad = self.dir / "ok.png", self.dir / "bad.png"
        ok.write_bytes(b"x"); bad.write_bytes(b"x")
        r = et.ocr_extract([ok, bad])
        self.assertEqual(r[str(ok)]["kind"], "ocr")
        self.assertIn("接收设备", r[str(ok)]["text"])
        self.assertFalse(r[str(bad)]["ok"])
        self.assertIn("读不了", r[str(bad)]["error"])


if __name__ == "__main__":
    unittest.main()
