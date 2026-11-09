"""桥接服务离线测试：假 Claude（stream-json 输出）+ 假飞书，不碰网络和 sudo。

运行：~/.agentos/venv/bin/python -m unittest ops/bridge/test_bridge.py -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bridge  # noqa: E402

DEAD_SID = "deadbeef-0000-0000-0000-000000000000"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, re, sys, time, uuid
sid_arg = sys.argv[1]
full = sys.stdin.read()
prompt = full.split("\n\n（系统提示")[0]
m = re.search(r"spaces/me/outbox/(\d+)/", full)
if prompt == "MAKEFILE" and m:  # 模拟 Agent 产出文件
    out = os.path.join(os.environ["FAKE_RUNTIME"], "spaces/me/outbox", m.group(1))
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "合同.docx"), "w").write("doc")
    open(os.path.join(out, "截图.png"), "w").write("png")
if sid_arg.startswith("deadbeef"):
    print("No conversation found with session ID: " + sid_arg, file=sys.stderr)
    sys.exit(1)
sid = str(uuid.uuid4()) if sid_arg == "-" else sid_arg
emit = lambda e: print(json.dumps(e, ensure_ascii=False), flush=True)
emit({"type": "system", "subtype": "init", "session_id": sid})
emit({"type": "assistant", "session_id": sid, "message": {"content": [
    {"type": "tool_use", "name": "Bash", "input": {"command": "rg 张总 spaces/"}}]}})
if prompt.startswith("SLEEP "):
    time.sleep(float(prompt.split()[1]))
if prompt == "CRASH":
    sys.exit(3)
emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
      "result": f"echo:{prompt}|resumed:{sid_arg}"})
'''


class FakeFeishu:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def _rec(self, *c):
        with self.lock:
            self.calls.append(c)

    def reply_text(self, message_id, text):
        self._rec("reply", message_id, text)

    def send_text(self, chat_id, text):
        self._rec("text", chat_id, text)

    def send_markdown(self, chat_id, md):
        self._rec("md", chat_id, md)

    def download(self, message_id, key, rtype):
        self._rec("download", message_id, key, rtype)
        return b"\x89PNG fake", ""

    def send_file(self, chat_id, path):
        self._rec("file", chat_id, path.name)
        return True

    def send_to_user(self, open_id, msg_type, content):
        self._rec("user", open_id, msg_type, json.loads(content))
        return True

    def of(self, kind):
        with self.lock:
            return [c for c in self.calls if c[0] == kind]


def wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake = Path(self.tmp.name) / "fake_claude.py"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        self.cfg = bridge.Config(
            app_id="x", app_secret="y", owner_open_id="ou_owner", runtime_dir=self.tmp.name,
            agent_user="agentos", max_concurrency=2, stuck_notice_sec=1, hard_timeout_sec=3,
            claude_cmd=[sys.executable, str(fake)])
        os.environ["FAKE_RUNTIME"] = self.tmp.name
        self.store = bridge.Store(Path(self.tmp.name) / "bridge.db")
        self.fs = FakeFeishu()
        self.b = bridge.Bridge(self.cfg, self.store, self.fs)
        threading.Thread(target=self.b.dispatch_forever, daemon=True).start()
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, text, sender="ou_owner", chat="oc_1", chat_type="p2p"):
        self.n += 1
        mid = f"om_{self.n}"
        self.b.on_message(mid, chat, chat_type, sender, "text", json.dumps({"text": text}))
        return mid

    def test_unbound_owner_gets_open_id(self):
        self.b.cfg.owner_open_id = ""
        self.send("hi", sender="ou_me")
        self.assertTrue(wait_for(lambda: self.fs.of("reply")))
        self.assertIn("ou_me", self.fs.of("reply")[0][2])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_non_owner_and_group_ignored(self):
        self.send("hi", sender="ou_stranger")
        self.send("hi", chat_type="group")
        time.sleep(0.5)
        self.assertEqual(self.fs.calls, [])

    def test_roundtrip_and_session_resume(self):
        self.send("第一句")
        self.assertTrue(wait_for(lambda: len(self.fs.of("md")) == 1))
        first = self.fs.of("md")[0][2]
        self.assertTrue(first.startswith("echo:第一句|resumed:-"), first)
        self.assertEqual(self.fs.of("reply")[0][2], "收到，处理中…")
        sid = self.store.get_session("oc_1")
        self.assertRegex(sid, bridge.SESSION_ID_RE)

        self.send("第二句")
        self.assertTrue(wait_for(lambda: len(self.fs.of("md")) == 2))
        self.assertEqual(self.fs.of("md")[1][2], f"echo:第二句|resumed:{sid}")

        self.send("新对话")
        self.assertTrue(wait_for(lambda: self.store.get_session("oc_1") is None))

    def test_duplicate_event_runs_once(self):
        self.b.on_message("om_dup", "oc_1", "p2p", "ou_owner", "text", json.dumps({"text": "a"}))
        self.b.on_message("om_dup", "oc_1", "p2p", "ou_owner", "text", json.dumps({"text": "a"}))
        self.assertTrue(wait_for(lambda: self.fs.of("md")))
        time.sleep(0.5)
        self.assertEqual(len(self.fs.of("md")), 1)
        self.assertEqual(len(self.fs.of("reply")), 1)

    def test_dead_session_falls_back_to_new(self):
        self.store.set_session("oc_1", DEAD_SID)
        self.send("还在吗")
        self.assertTrue(wait_for(lambda: self.fs.of("md")))
        self.assertEqual(self.fs.of("md")[0][2], "echo:还在吗|resumed:-")
        self.assertNotEqual(self.store.get_session("oc_1"), DEAD_SID)

    def test_same_chat_serial_other_chat_parallel(self):
        self.send("SLEEP 0.8", chat="oc_a")
        self.send("SLEEP 0.8", chat="oc_a")
        self.send("SLEEP 0.8", chat="oc_b")
        start = time.time()
        self.assertTrue(wait_for(lambda: len(self.fs.of("md")) == 3))
        elapsed = time.time() - start
        # oc_a 两条串行约 1.6s，oc_b 并行；全串行会是 2.4s
        self.assertLess(elapsed, 2.3)
        self.assertGreater(elapsed, 1.5)

    def test_stuck_notice_then_hard_timeout(self):
        self.send("SLEEP 10")
        self.assertTrue(wait_for(lambda: self.fs.of("md"), timeout=8))
        stuck = self.fs.of("text")
        self.assertEqual(len(stuck), 1)
        self.assertIn("rg 张总", stuck[0][2])
        self.assertIn("超时", self.fs.of("md")[0][2])
        row = self.store.db.execute("SELECT status FROM tasks").fetchone()
        self.assertEqual(row["status"], "failed")

    def test_crash_reported(self):
        self.send("CRASH")
        self.assertTrue(wait_for(lambda: self.fs.of("md")))
        self.assertIn("异常退出", self.fs.of("md")[0][2])

    def test_unsupported_type(self):
        self.b.on_message("om_st", "oc_1", "p2p", "ou_owner", "sticker", json.dumps({"file_key": "k"}))
        self.assertTrue(wait_for(lambda: self.fs.of("reply")))
        self.assertIn("还不支持", self.fs.of("reply")[0][2])

    def test_attachment_saved_to_inbox_and_agent_asked(self):
        self.b.on_message("om_img", "oc_1", "p2p", "ou_owner", "image", json.dumps({"image_key": "img_1"}))
        self.b.on_message("om_f", "oc_1", "p2p", "ou_owner", "file",
                          json.dumps({"file_key": "file_1", "file_name": "../../合同 v2.pdf"}))
        self.assertTrue(wait_for(lambda: len(self.fs.of("md")) == 2))
        saved = sorted(p.name for p in Path(self.tmp.name, "spaces/me/raw/inbox").rglob("*") if p.is_file())
        self.assertEqual(len(saved), 2)
        self.assertTrue(any(n.endswith("-合同_v2.pdf") for n in saved), saved)  # ../ 被去掉
        self.assertTrue(any(n.endswith("-图片.png") for n in saved), saved)
        self.assertEqual(sorted(c[3] for c in self.fs.of("download")), ["file", "image"])
        self.assertTrue(all("已存进收件箱" in c[2] for c in self.fs.of("reply")))
        self.assertTrue(all("spaces/me/raw/inbox/" in c[2] for c in self.fs.of("md")))  # Agent 拿到了路径

    def test_duplicate_attachment_downloaded_once(self):
        args = ("om_dupf", "oc_1", "p2p", "ou_owner", "file", json.dumps({"file_key": "f", "file_name": "a.txt"}))
        self.b.on_message(*args)
        self.assertTrue(wait_for(lambda: self.fs.of("md")))
        self.b.on_message(*args)
        time.sleep(0.5)
        self.assertEqual(len(self.fs.of("download")), 1)

    def test_outbox_files_sent_after_reply(self):
        self.send("MAKEFILE")
        self.assertTrue(wait_for(lambda: len(self.fs.of("file")) == 2))
        self.assertEqual(sorted(c[2] for c in self.fs.of("file")), ["合同.docx", "截图.png"])
        kinds = [c[0] for c in self.fs.calls if c[0] in ("md", "file")]
        self.assertEqual(kinds[0], "md")  # 先回文字，再发文件

    def test_stop_aborts_running_and_clears_queue(self):
        self.send("SLEEP 5")
        self.send("排队一")
        self.send("排队二")
        self.assertTrue(wait_for(lambda: "oc_1" in self.b.procs))
        started = time.time()
        self.send("停止")
        self.assertTrue(wait_for(lambda: not self.b.busy, timeout=5))
        self.assertLess(time.time() - started, 3)
        reply = self.fs.of("reply")[-1][2]
        self.assertIn("已中止", reply)
        self.assertIn("2 条排队", reply)
        time.sleep(0.5)
        self.assertEqual(self.fs.of("md"), [])  # 被停的任务不再推结果，排队的也没跑
        statuses = [r[0] for r in self.store.db.execute("SELECT status FROM tasks ORDER BY id")]
        self.assertEqual(statuses, ["failed", "cancelled", "cancelled"])

        self.send("还在吗")  # 停止后照常接活
        self.assertTrue(wait_for(lambda: self.fs.of("md")))

    def test_stop_when_idle(self):
        self.send("/stop")
        self.assertTrue(wait_for(lambda: self.fs.of("reply")))
        self.assertIn("没有在跑", self.fs.of("reply")[0][2])

    def test_status_and_help(self):
        self.send("状态")
        self.assertTrue(wait_for(lambda: self.fs.of("reply")))
        self.assertIn("正在干活：否", self.fs.of("reply")[0][2])
        self.send("帮助")
        self.assertTrue(wait_for(lambda: self.fs.of("md")))
        self.assertIn("停止", self.fs.of("md")[0][2])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_recover_interrupted(self):
        self.store.enqueue("om_x", "oc_1", "ou_owner", "做一半")
        self.store.db.execute("UPDATE tasks SET status='running'")
        self.b.notify_interrupted()
        self.assertIn("中断", self.fs.of("text")[0][2])
        self.assertEqual(self.store.db.execute("SELECT status FROM tasks").fetchone()[0], "failed")


class DraftConfirmTest(unittest.TestCase):
    """两步确认：草稿 → 卡片 → 本人点发送才发。用假飞书和假 SMTP。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name)
        self.cfg = bridge.Config(
            app_id="x", app_secret="y", owner_open_id="ou_owner", runtime_dir=self.tmp.name,
            agent_user="agentos", max_concurrency=1, stuck_notice_sec=60, hard_timeout_sec=60,
            claude_cmd=["true"], mail_user="me@qq.com", mail_password="pw", smtp_host="smtp.qq.com")
        self.fs = FakeFeishu()
        self.b = bridge.Bridge(self.cfg, bridge.Store(self.rt / "bridge.db"), self.fs)
        self.sent = []
        self.b.smtp_send = lambda d: self.sent.append(d)
        self.pending = self.rt / "spaces/me/outbox/pending"
        self.pending.mkdir(parents=True)
        (self.rt / "spaces/me/outbox/7").mkdir()
        (self.rt / "spaces/me/outbox/7/合同.docx").write_text("doc")

    def tearDown(self):
        self.tmp.cleanup()

    def draft(self, name="d1.json", **kw):
        d = {"to": ["19507429700@163.com"], "subject": "合同", "body": "请查收",
             "attachments": ["spaces/me/outbox/7/合同.docx"]}
        d.update(kw)
        (self.pending / name).write_text(json.dumps(d, ensure_ascii=False))

    def cards(self):
        return [c for c in self.fs.of("user") if c[2] == "interactive"]

    def click(self, act, who="ou_owner"):
        value = self.cards()[-1][3]["body"]["elements"][2]["columns"][0 if act == "send" else 1] \
            ["elements"][0]["behaviors"][0]["value"]
        return self.b.on_card_action(who, value)

    def test_card_once_then_send_only_after_click(self):
        self.draft()
        self.b.scan_drafts(settle_sec=0)
        self.b.scan_drafts(settle_sec=0)
        self.assertEqual(len(self.cards()), 1)  # 同一草稿只弹一次
        self.assertEqual(self.cards()[0][1], "ou_owner")
        self.assertIn("19507429700@163.com", json.dumps(self.cards()[0][3], ensure_ascii=False))
        self.assertEqual(self.sent, [])  # 没点就不发
        self.assertIn("正在发送", self.click("send")["toast"]["content"])
        self.assertTrue(wait_for(lambda: self.sent))
        self.assertEqual(self.sent[0]["attachments"][0].name, "合同.docx")
        self.assertTrue(wait_for(lambda: any("已发出" in c[3].get("text", "") for c in self.fs.of("user"))))
        self.assertIn("处理过", self.click("send")["toast"]["content"])  # 连点不会发两封
        self.assertEqual(len(self.sent), 1)

    def test_cancel_and_non_owner(self):
        self.draft()
        self.b.scan_drafts(settle_sec=0)
        self.assertIn("只有本人", self.click("send", who="ou_stranger")["toast"]["content"])
        self.assertIn("先不发", self.click("cancel")["toast"]["content"])
        self.assertIn("处理过", self.click("send")["toast"]["content"])
        time.sleep(0.3)
        self.assertEqual(self.sent, [])

    def test_changed_after_card_is_refused(self):
        self.draft()
        self.b.scan_drafts(settle_sec=0)
        self.draft(to=["evil@x.com"])  # 弹卡后草稿被改
        self.click("send")
        self.assertTrue(wait_for(lambda: any("被改过" in c[3].get("text", "") for c in self.fs.of("user"))))
        self.assertEqual(self.sent, [])
        self.b.scan_drafts(settle_sec=0)  # 改过的内容重新弹卡让你确认
        self.assertEqual(len(self.cards()), 2)
        self.assertIn("evil@x.com", json.dumps(self.cards()[1][3]))

    def test_invalid_drafts_reported_not_carded(self):
        self.draft("a.json", to=["不是邮箱"])
        self.draft("b.json", attachments=["/Users/syzlmacx/.agentos/bridge.env"])
        self.draft("c.json", attachments=["spaces/me/outbox/../../../../.env"])
        (self.pending / "d.json").write_text("{坏")
        self.b.scan_drafts(settle_sec=0)
        self.assertEqual(self.cards(), [])
        texts = [c[3]["text"] for c in self.fs.of("user")]
        self.assertEqual(len(texts), 4)
        self.assertTrue(any("邮箱地址不对" in t for t in texts))
        self.assertEqual(sum("附件只能来自" in t for t in texts), 2)

    def test_smtp_message_built_correctly(self):
        box = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout):
                box["server"] = (host, port)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                box["login"] = (u, p)

            def send_message(self, msg):
                box["msg"] = msg
        self.draft(cc=["c@x.com"])
        d = bridge.outbox.load_draft(self.pending / "d1.json", self.rt)
        bridge.outbox.send_mail(d, "me@qq.com", "pw", "smtp.qq.com", smtp_factory=FakeSMTP)
        m = box["msg"]
        self.assertEqual((box["server"], box["login"]), (("smtp.qq.com", 465), ("me@qq.com", "pw")))
        self.assertEqual((m["From"], m["To"], m["Cc"], m["Subject"]), ("me@qq.com", "19507429700@163.com", "c@x.com", "合同"))
        self.assertEqual([p.get_filename() for p in m.iter_attachments()], ["合同.docx"])

    def test_draft_still_being_written_waits(self):
        self.draft()
        self.b.scan_drafts(settle_sec=60)
        self.assertEqual(self.fs.of("user"), [])


class HelpersTest(unittest.TestCase):
    def test_split_chunks(self):
        self.assertEqual(bridge.split_chunks("abc", 10), ["abc"])
        text = "\n".join(["x" * 40] * 10)
        chunks = bridge.split_chunks(text, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual("\n".join(chunks), text)
        self.assertTrue(all(len(c) <= 50 for c in bridge.split_chunks("y" * 120, 50)))

    def test_extract_post(self):
        content = json.dumps({"title": "标题", "content": [[{"tag": "text", "text": "第一行"}],
                                                          [{"tag": "a", "text": "链接", "href": "u"}]]})
        self.assertEqual(bridge.extract_text("post", content), "标题\n第一行\n链接")
        self.assertIsNone(bridge.extract_text("image", "{}"))

    def test_load_env(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# c\nAGENTOS_TEST_K='v 1'\nBAD LINE\n")
        try:
            bridge.load_env(Path(f.name))
            self.assertEqual(os.environ["AGENTOS_TEST_K"], "v 1")
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
