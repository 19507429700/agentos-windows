"""写飞书文档的离线测试：假飞书接口，不联网。

运行：py -m unittest ops/test_feishu_write.py -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import feishu_user as fu  # noqa: E402
import feishu_write as fw  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


class FakeApi:
    """convert 把每行 Markdown 变成一个块；图片行变成图片块。表格块带 merge_info。"""

    def __init__(self):
        self.calls = []
        self.docs = {}
        self.uploads = []
        self.fail_upload = False

    def post(self, path, body, params=None):
        self.calls.append(("POST", path, body, params))
        if path == "/docx/v1/documents":
            did = f"Doc{len(self.docs):023d}"
            self.docs[did] = []
            return {"document": {"document_id": did}}
        if path.endswith("/blocks/convert"):
            blocks, first, imgs = [], [], []
            for i, line in enumerate(l for l in body["content"].splitlines() if l.strip()):
                bid = f"tmp{i}"
                first.append(bid)
                if line.startswith("!["):
                    blocks.append({"block_id": bid, "block_type": 27, "image": {}})
                    imgs.append({"block_id": bid, "image_url": line.split("(")[1].rstrip(")")})
                elif line.startswith("|"):
                    blocks.append({"block_id": bid, "block_type": 31, "children": [bid + "c"],
                                   "table": {"property": {"row_size": 1, "merge_info": [{}]}}})
                    blocks.append({"block_id": bid + "c", "block_type": 32, "children": []})
                else:
                    blocks.append({"block_id": bid, "block_type": 2, "text": {"t": line}})
            return {"first_level_block_ids": first, "blocks": blocks, "block_id_to_image_urls": imgs}
        if path.endswith("/descendant"):
            did = path.split("/")[4]
            self.docs[did] += body["children_id"]
            return {"block_id_relations": [{"temporary_block_id": b["block_id"], "block_id": "real_" + b["block_id"]}
                                           for b in body["descendants"]]}
        raise AssertionError(path)

    def patch(self, path, body, params=None):
        self.calls.append(("PATCH", path, body, params))
        return {}

    def upload_image(self, path, block_id, doc_id):
        if self.fail_upload:
            raise fu.FeishuError("1061004 forbidden")
        self.uploads.append((path.name, block_id, doc_id))
        return "filetok"


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name) / "rt"
        self.req_dir = self.rt / "spaces/me/outbox/feishu-docs/12"
        self.draft = self.rt / "spaces/me/outbox/草稿/12"
        for d in (self.req_dir, self.draft, self.rt / "spaces/me/raw/feishu/x-图片"):
            d.mkdir(parents=True)
        (self.rt / "spaces/me/raw/feishu/x-图片/01.png").write_bytes(PNG)
        self.reg = Path(self.tmp.name) / "reg.json"
        self.api = FakeApi()

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, name, md, **kw):
        (self.draft / (name + ".md")).write_text(md, "utf-8")
        p = self.req_dir / (name + ".json")
        p.write_text(json.dumps({"markdown": f"spaces/me/outbox/草稿/12/{name}.md", **kw}, ensure_ascii=False))
        return p

    def run_req(self, p, folder="FOLDER"):
        return fw.process(p, self.rt, self.reg, folder, self.api, now=100, sleep=lambda s: None)

    def test_create_with_image_table_and_link(self):
        p = self.request("a", "# 周会\n![图1](spaces/me/raw/feishu/x-图片/01.png)\n| a |\n![网图](https://e.com/b.png)\n",
                         title="周会纪要")
        msg = self.run_req(p)
        [did] = self.api.docs
        self.assertEqual(msg, f"📄 已建好飞书文档《周会纪要》：https://feishu.cn/docx/{did}")
        create = self.api.calls[0]
        self.assertEqual(create[2], {"title": "周会纪要", "folder_token": "FOLDER"})
        convert = self.api.calls[1][2]["content"]
        self.assertIn("![图1](https://agentos.invalid/img/0.png)", convert)  # 本地图换占位
        self.assertIn("[图：网图](https://e.com/b.png)", convert)            # 网图换链接
        desc = [c for c in self.api.calls if c[1].endswith("/descendant")][0][2]
        table = [b for b in desc["descendants"] if b["block_type"] == 31][0]
        self.assertNotIn("merge_info", table["table"]["property"])
        self.assertEqual(desc["index"], -1)
        self.assertEqual(self.api.uploads, [("01.png", "real_tmp1", did)])
        patch = [c for c in self.api.calls if c[0] == "PATCH"][0]
        self.assertEqual(patch[1], f"/docx/v1/documents/{did}/blocks/real_tmp1")
        self.assertEqual(patch[2], {"replace_image": {"token": "filetok"}})
        self.assertEqual(json.loads(self.reg.read_text())[did]["title"], "周会纪要")

    def test_append_by_title_or_link_only_own_docs(self):
        self.run_req(self.request("a", "第一段", title="周会纪要"))
        [did] = self.api.docs
        msg = self.run_req(self.request("b", "第二段", append_to="周会纪要"))
        self.assertTrue(msg.startswith("📄 已把内容补进《周会纪要》"))
        self.run_req(self.request("c", "第三段", append_to=f"https://x.feishu.cn/docx/{did}?from=x"))
        self.assertEqual(len(self.api.docs), 1)       # 没有多建
        self.assertEqual(len(self.api.docs[did]), 3)  # 三段都追加在同一篇
        with self.assertRaises(fw.DocError) as e:     # 不是 AgentOS 建的：不碰
            self.run_req(self.request("d", "x", append_to="https://x.feishu.cn/docx/AbcdefghijklmnopqrstuvwxyZ"))
        self.assertIn("《周会纪要》", str(e.exception))

    def test_many_blocks_split_in_order(self):
        md = "\n".join(f"第{i}行" for i in range(fw.MAX_BLOCKS_PER_CALL + 20))
        self.run_req(self.request("a", md, title="长文"))
        calls = [c for c in self.api.calls if c[1].endswith("/descendant")]
        self.assertEqual(len(calls), 2)
        [did] = self.api.docs
        self.assertEqual(self.api.docs[did], [f"tmp{i}" for i in range(fw.MAX_BLOCKS_PER_CALL + 20)])

    def test_image_problems_reported_not_fatal(self):
        self.api.fail_upload = True
        msg = self.run_req(self.request("a", "![](spaces/me/raw/feishu/x-图片/01.png)\n![](没有.png)", title="t"))
        self.assertIn("已建好", msg)
        self.assertIn("01.png 上传失败", msg)
        self.assertIn("找不到图片 没有.png", msg)

    def test_bad_requests(self):
        for kw, md, want in [({}, "x", "title"), ({"title": "t"}, "  ", "空"),
                             ({"title": "t", "markdown": "/etc/hosts"}, "x", "只能放在")]:
            with self.assertRaises(fw.DocError) as e:
                self.run_req(self.request("bad", md, **kw))
            self.assertIn(want, str(e.exception))
        outside = self.rt / "spaces/me/raw/feishu/x-图片/01.png"
        md = f"![x]({outside.parent.parent.parent.parent.parent / 'secret.png'})"
        (self.rt / "spaces/secret.png").write_bytes(PNG)
        msg = self.run_req(self.request("e", md, title="t"))
        self.assertIn("找不到图片", msg)            # spaces/me 以外的图不传
        self.assertEqual(self.api.uploads, [])


if __name__ == "__main__":
    unittest.main()
