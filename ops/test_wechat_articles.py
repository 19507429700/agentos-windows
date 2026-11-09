"""公众号文章抓取归档的离线测试：用假的 fetch_wx.py，不碰网络、不碰微信。

运行：/usr/bin/python3 -m unittest ops/test_wechat_articles.py -v
"""

import io
import json
import os
from contextlib import redirect_stdout
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parent

# 假抓取脚本：和真的 fetch_wx.py 一样，失败时也写一个只有元信息的空壳 .md
FAKE_FETCH = textwrap.dedent('''
    import sys, hashlib
    from pathlib import Path
    args = sys.argv[1:]
    url = args[args.index("--url") + 1]
    out = Path(args[args.index("--out_dir") + 1])
    out.mkdir(parents=True, exist_ok=True)
    p = out / ("wx_" + hashlib.md5(url.encode()).hexdigest()[:6] + ".md")
    head = '---\\ntitle: "%s"\\npublish_time: "2026-11-05"\\nsource_url: "%s"\\n---\\n'
    if "GONE" in url:
        p.write_text(head % ("", url), encoding="utf-8")
        print("  -> %s | FAIL: 文章不可用：已被发布者删除 / 因违规下架 / 公众号已迁移（不是 IP 问题）" % p, file=sys.stderr)
        sys.exit(2)
    if "BLOCK" in url:
        p.write_text(head % ("", url), encoding="utf-8")
        print("  -> %s | FAIL: all 4 UAs blocked (IP 维度硬墙，换 IP/proxy 才解)" % p, file=sys.stderr)
        sys.exit(2)
    p.write_text(head % ("好文章", url) + "\\n# 好文章\\n\\n" + "正文内容。" * 30 + "\\n", encoding="utf-8")
    sys.exit(0)
''')

OK_URL = "https://mp.weixin.qq.com/s/OKokOKokOK12"
BLOCK_URL = "https://mp.weixin.qq.com/s/BLOCKblock12"
GONE_URL = "https://mp.weixin.qq.com/s/GONEgone1234"


class FetchArticlesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.raw, self.state, self.inbox = t / "raw", t / "state", t / "inbox"
        chat = self.raw / "wechat/2026-11-06/群-abc123.md"
        chat.parent.mkdir(parents=True)
        chat.write_text(
            "# 群\n\n- 会话: x（群）\n\n"
            f"- 10:00 甲：[公众号] 好文章 — 某号\n  {OK_URL}\n"
            f"- 10:01 乙：[公众号] 被拦的 — 某号\n  {BLOCK_URL}\n"
            f"- 10:02 丙：[公众号] 被删的 — 某号\n  {GONE_URL}\n",
            encoding="utf-8")
        fake = t / "fake_fetch.py"
        fake.write_text(FAKE_FETCH, encoding="utf-8")
        self.env = dict(os.environ, AGENTOS_RAW=str(self.raw), AGENTOS_STATE_DIR=str(self.state),
                        WECHAT_ARTICLES_INBOX=str(self.inbox), WECHAT_FETCH_PY=str(fake),
                        WECHAT_FETCH_PYTHON=sys.executable)

    def tearDown(self):
        self.tmp.cleanup()

    def run_py(self, script, *args):
        return subprocess.run([sys.executable, str(OPS / script), *args], env=self.env,
                              capture_output=True, text=True, encoding="utf-8")

    def state_json(self):
        return json.loads((self.state / "wechat_articles.json").read_text())

    def test_fail_not_archived_gone_not_retried(self):
        r = self.run_py("fetch_wechat_articles.py")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # 成功 1、失败 1 → 部分成功
        self.assertIn("成功 1，失败 1", r.stdout)
        self.assertIn("失效 1", r.stdout)

        # 只有成功的那篇进了知识库；失败留下的空壳没进收件夹，临时目录也清干净了
        archived = list((self.raw / "wechat/articles").glob("*.md"))
        self.assertEqual([p.name for p in archived], ["2026-11-05-好文章.md"])
        self.assertEqual(len(list(self.inbox.glob("*.md"))), 1)
        self.assertEqual([p for p in self.inbox.iterdir() if p.name.startswith(".fetch-")], [])

        st = self.state_json()
        self.assertEqual(list(st["done"]), ["s:OKokOKokOK12"])
        self.assertEqual(list(st["gone"]), ["s:GONEgone1234"])
        self.assertIn("已被发布者删除", st["gone"]["s:GONEgone1234"]["reason"])

        # 下一轮只剩被拦的那篇要重抓；被删的不再列出
        r = self.run_py("wechat_articles.py", "urls")
        self.assertEqual(r.stdout.split(), [BLOCK_URL])

        r = self.run_py("fetch_wechat_articles.py")
        self.assertEqual(r.returncode, 2)  # 只抓了被拦的一篇，又失败 → 全部失败
        self.assertIn("成功 0，失败 1", r.stdout)

    def test_only_gone_counts_as_success(self):
        chat = next((self.raw / "wechat").rglob("群-*.md"))
        chat.write_text(f"- 10:02 丙：[公众号] 被删的 — 某号\n  {GONE_URL}\n", encoding="utf-8")
        r = self.run_py("fetch_wechat_articles.py")
        self.assertEqual(r.returncode, 0, r.stdout)  # 文章没了不算失败，任务不会一直处于失败状态
        r = self.run_py("fetch_wechat_articles.py")
        self.assertIn("没有待抓取", r.stdout)

    def test_ingest_skips_empty_shell(self):
        self.inbox.mkdir(parents=True)
        (self.inbox / "空壳.md").write_text(f'---\ntitle: ""\nsource_url: "{BLOCK_URL}"\n---\n',
                                          encoding="utf-8")
        r = self.run_py("wechat_articles.py", "ingest")
        self.assertIn("正文是空的", r.stdout)
        self.assertFalse((self.raw / "wechat/articles").exists())
        self.assertEqual(self.state_json()["done"], {})


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
JPG = b"\xff\xd8\xff\xe0" + b"1" * 40


def fake_fetch(url):
    if "bad" in url:
        raise OSError("http 403")
    if "html" in url:
        return b"<html>not an image</html>", "text/html"
    return (PNG, "image/png") if url.endswith("png") else (JPG, "")


class ArticleImagesTest(unittest.TestCase):
    """图片下载到 <文章名>-图片/NN.ext，正文改成 ![图N](spaces/me/raw/…)。"""

    def setUp(self):
        sys.path.insert(0, str(OPS))
        import wechat_articles as wa
        self.wa = wa
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = self.root / "spaces/me/raw"   # 和运行副本一样的目录层级
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self._env = dict(os.environ)
        os.environ.update(AGENTOS_RAW=str(self.raw), WECHAT_ARTICLES_INBOX=str(self.inbox))
        self.wa.STATE_DIR = self.root / "state"
        self._fetch, self._delay = wa.fetch_image, wa.IMAGE_DELAY_SEC
        wa.fetch_image, wa.IMAGE_DELAY_SEC = fake_fetch, 0

    def tearDown(self):
        self.wa.fetch_image, self.wa.IMAGE_DELAY_SEC = self._fetch, self._delay
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def article(self, body):
        return ('---\ntitle: "带图的文章"\npublish_time: "2026-11-05 10:00"\n'
                f'source_url: "{OK_URL}"\n---\n\n# 带图的文章\n\n' + "正文内容。" * 20 + "\n\n" + body)

    def test_ingest_downloads_images(self):
        (self.inbox / "local.jpg").write_bytes(JPG)
        body = ("![](https://mmbiz.qpic.cn/a.png)\n\n"
                "![封面](https://mmbiz.qpic.cn/b?wx_fmt=jpeg&amp;from=appmsg)\n\n"
                "![](https://mmbiz.qpic.cn/a.png)\n\n"          # 同一张图第二次出现
                "![](https://mmbiz.qpic.cn/bad.png)\n\n"        # 下载失败
                "![](https://mmbiz.qpic.cn/html)\n\n"           # 返回的不是图片
                "![](local.jpg)\n\n"                            # 别的工具下好的本地图
                "![](data:image/gif;base64,R0lGOD)\n")
        (self.inbox / "a.md").write_text(self.article(body), encoding="utf-8")
        with io.StringIO() as buf, redirect_stdout(buf):
            self.wa.cmd_ingest(None)
        dest = self.raw / "wechat/articles/2026-11-05-带图的文章.md"
        img_dir = self.raw / "wechat/articles/2026-11-05-带图的文章-图片"
        text = dest.read_text(encoding="utf-8")
        rel = "spaces/me/raw/wechat/articles/2026-11-05-带图的文章-图片"
        self.assertEqual(sorted(p.name for p in img_dir.iterdir()), ["01.png", "02.jpg", "03.jpg"])
        self.assertEqual((img_dir / "01.png").read_bytes(), PNG)
        self.assertEqual(text.count(f"![图1]({rel}/01.png)"), 2)       # 重复的图只存一份
        self.assertIn(f"![图2]({rel}/02.jpg)", text)
        self.assertIn(f"![图3]({rel}/03.jpg)", text)                  # 本地图复制过来
        self.assertIn("![](https://mmbiz.qpic.cn/bad.png)", text)    # 失败的保留原网址
        self.assertIn("![](https://mmbiz.qpic.cn/html)", text)
        self.assertIn("data:image/gif", text)
        self.assertIn("- 图片：已下载 3 张，在同名「-图片」文件夹；2 张没下下来，保留原网址", text)

        # 补图片：网络好了再跑 images，只补没下下来的，编号接着往后排
        self.wa.fetch_image = lambda url: (PNG, "image/png")
        with io.StringIO() as buf, redirect_stdout(buf):
            self.wa.cmd_images()
        text = dest.read_text(encoding="utf-8")
        self.assertEqual(sorted(p.name for p in img_dir.iterdir()),
                         ["01.png", "02.jpg", "03.jpg", "04.png", "05.png"])
        self.assertNotIn("](https://mmbiz", text)
        self.assertIn("- 图片：已下载 5 张，在同名「-图片」文件夹\n", text)
        self.assertEqual(text.count("- 图片："), 1)

        # 再跑一次什么都不动
        before = dest.read_text(encoding="utf-8")
        with io.StringIO() as buf, redirect_stdout(buf):
            self.wa.cmd_images()
        self.assertEqual(dest.read_text(encoding="utf-8"), before)

    def test_images_backfills_old_archive_without_image_line(self):
        dest = self.raw / "wechat/articles/2026-11-04-旧文章.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("# 旧文章\n\n- 来源：微信公众号文章（你本人抓取并归档）\n- 链接：x\n"
                        "- 归档：2026-11-06 19:14\n\n---\n\n正文\n![](https://mmbiz.qpic.cn/a.png)\n",
                        encoding="utf-8")
        with io.StringIO() as buf, redirect_stdout(buf):
            self.wa.cmd_images()
        text = dest.read_text(encoding="utf-8")
        self.assertIn("- 归档：2026-11-06 19:14\n- 图片：已下载 1 张", text)
        self.assertIn("![图1](spaces/me/raw/wechat/articles/2026-11-04-旧文章-图片/01.png)", text)


if __name__ == "__main__":
    unittest.main()
