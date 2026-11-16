"""讯飞录音笔采集的离线测试：用假的官方客户端，不联网。

运行：py -m unittest ops/test_iflyrec.py -v
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import collect_iflyrec as ci  # noqa: E402

NOW = time.time()


def iso(sec_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - sec_ago))


class FakeCli:
    """两条录音：A 有总结和两页转写；B 刚录完，还没有转写。"""

    def __init__(self):
        self.calls = []
        self.b_ready = False
        self.auth_broken = False
        self.change_once = False

    def __call__(self, *args):
        self.calls.append(args)
        if self.auth_broken:
            raise ci.CliError("AUTH_REQUIRED", "需要授权")
        cmd = args[0]
        if cmd == "search":
            if "--cursor" not in args:
                return {"recordings": [{"recordingId": "111111222222", "title": "周会：内容/商业化",
                                        "durationSeconds": "1224.8", "createdAt": iso(3600)}],
                        "hasMore": "True", "nextCursor": "p2"}
            return {"recordings": [{"recordingId": "333333444444", "title": "刚录的",
                                    "durationSeconds": "30", "createdAt": iso(60)}],
                    "hasMore": "False", "nextCursor": None}
        rid = args[1]
        if cmd == "summary":
            if rid == "333333444444":
                raise ci.CliError("CONTENT_NOT_AVAILABLE", "没有总结")
            return {"contentVersion": "s1", "text": "# 周会\n## 决定\n- 下周上线", "hasMore": "False"}
        if cmd == "transcript":
            if rid == "333333444444" and not self.b_ready:
                return {"contentVersion": "t0", "segments": [], "hasMore": "False"}
            if "--cursor" not in args:
                return {"contentVersion": "t1", "segments": [{"speakerName": "讲话人1", "text": "先说结论"}],
                        "hasMore": "True", "nextCursor": "c2"}
            if self.change_once:
                self.change_once = False
                return {"contentVersion": "t2", "segments": [], "hasMore": "False"}
            return {"contentVersion": "t1", "segments": [{"speakerId": "2", "text": "好的\n第二行"}],
                    "hasMore": "False"}
        raise AssertionError(args)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / "raw"
        self.cli = FakeCli()
        self.st = {"done": {}, "pending": {}}
        p = mock.patch.object(ci, "run_cli", self.cli)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, now=NOW):
        return ci.collect(self.st, self.raw, now, 30, 7, 15)

    def files(self):
        return sorted(self.raw.rglob("*.md"))

    def test_saves_summary_and_full_transcript(self):
        self.assertEqual(self.run_once(), 1)
        [f] = self.files()
        self.assertEqual(f.name, "录音-周会：内容_商业化-222222.md")  # 斜杠换成下划线
        t = f.read_text(encoding="utf-8")
        self.assertIn("- 来源：讯飞录音笔", t)
        self.assertIn("- 时长：20 分 25 秒", t)
        self.assertIn("### 周会", t)          # 总结标题降两级
        self.assertIn("#### 决定", t)
        self.assertIn("- 讲话人1：先说结论", t)
        self.assertIn("- 讲话人2：好的\n  第二行", t)   # 没名字用编号，多行缩进
        self.assertIn("333333444444", self.st["pending"])  # 刚录的还没转写：等下一轮

    def test_not_saved_twice_and_pending_retried(self):
        self.run_once()
        self.cli.b_ready = True
        self.assertEqual(self.run_once(), 1)   # 只存新转写好的那条
        self.assertEqual(len(self.files()), 2)
        self.assertEqual(self.st["pending"], {})
        self.assertEqual(self.run_once(), 0)

    def test_gives_up_waiting_after_two_days(self):
        self.run_once()
        self.run_once(now=NOW + ci.PENDING_GIVE_UP_SEC + 1)
        b = [f for f in self.files() if "444444" in f.name][0].read_text(encoding="utf-8")
        self.assertIn("（暂无转写）", b)
        self.assertIn("（讯飞没有生成总结）", b)
        self.assertIn("2 天仍没有转写", b)

    def test_content_changed_rereads_from_start(self):
        self.cli.change_once = True
        self.run_once()
        t = self.files()[0].read_text(encoding="utf-8")
        self.assertEqual(t.count("先说结论"), 1)  # 旧版本拼的内容丢掉了，没有重复

    def test_window_after_first_run(self):
        self.run_once()
        first_since = [a for a in self.cli.calls if a[0] == "search"][0][2]
        self.cli.calls.clear()
        self.run_once()
        later_since = [a for a in self.cli.calls if a[0] == "search"][0][2]
        self.assertLess(first_since, later_since)  # 第一次往回 30 天，之后 7 天


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "cli.py").write_text("")
        self.env = mock.patch.dict(os.environ, {"AGENTOS_RAW": str(d / "raw"), "IFLYREC_CLI": str(d / "cli.py")})
        self.env.start()
        self.state = mock.patch.object(ci, "STATE_DIR", d / "state")
        self.state.start()

    def tearDown(self):
        self.state.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_auth_broken_exits_nonzero(self):
        cli = FakeCli()
        cli.auth_broken = True
        with mock.patch.object(ci, "run_cli", cli), self.assertLogs("collect_iflyrec", "ERROR") as logs:
            self.assertEqual(ci.main([]), 2)
        self.assertIn("重新授权", logs.output[0])

    def test_no_client_quiet(self):
        with mock.patch.dict(os.environ, {"IFLYREC_CLI": str(Path(self.tmp.name) / "没有.py")}):
            self.assertEqual(ci.main([]), 0)



class InstallTest(unittest.TestCase):
    """install_iflyrec：下载后核对指纹、放到 .agentos\\iflyrec-skill；指纹不对就停。"""

    def setUp(self):
        import hashlib, io, json, zipfile
        import install_iflyrec as ii
        self.ii = ii
        self.tmp = tempfile.TemporaryDirectory()
        cli = b"print('hi')"
        rel = {"files": {"recording-assistant-prod/scripts/recording_assistant.py": hashlib.sha256(cli).hexdigest()}}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("recording-assistant-prod/scripts/recording_assistant.py", cli)
            z.writestr("recording-assistant-prod/release.json", json.dumps(rel))
        self.zip = buf.getvalue()
        self.sha = hashlib.sha256(self.zip).hexdigest()
        self.dest = Path(self.tmp.name) / "iflyrec-skill"
        self.p = mock.patch.object(ii, "DEST", self.dest)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def fake_fetch(self, sha):
        import json

        def fetch(url):
            if url.endswith("latest.json"):
                return json.dumps({"name": "iflytek-recorder", "skillDirectory": "recording-assistant-prod",
                                   "version": "1.4.5", "artifact": {"url": self.ii.BASE + "/releases/x.zip",
                                                                    "sha256": sha}}).encode()
            return self.zip
        return fetch

    def test_installs_verified_files(self):
        with mock.patch.object(self.ii, "fetch", self.fake_fetch(self.sha)):
            self.assertEqual(self.ii.download(), "1.4.5")
        self.assertEqual((self.dest / "scripts/recording_assistant.py").read_bytes(), b"print('hi')")

    def test_bad_hash_stops(self):
        with mock.patch.object(self.ii, "fetch", self.fake_fetch("0" * 64)), self.assertRaises(SystemExit):
            self.ii.download()
        self.assertFalse(self.dest.exists())



if __name__ == "__main__":
    unittest.main()
