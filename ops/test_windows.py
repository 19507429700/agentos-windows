"""Windows 版新写的脚本的离线测试（在 Mac、Linux 上也能跑）：
run_agent（选底座、空值不传）、run_job（日志、超时、告警只发一次）、daily_review（找原文、取结果）、set_key。

运行：python -m unittest ops/test_windows.py -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import agentos_env  # noqa: E402
import daily_review  # noqa: E402
import run_agent  # noqa: E402
import run_job  # noqa: E402
import set_key  # noqa: E402


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self._saved = (run_agent.STATE, run_job.STATE, run_job.LOGS, set_key.STATE)
        run_agent.STATE = run_job.STATE = set_key.STATE = self.state
        run_job.LOGS = self.state / "logs"

    def tearDown(self):
        run_agent.STATE, run_job.STATE, run_job.LOGS, set_key.STATE = self._saved
        self.tmp.cleanup()


class RunAgentTest(TempState):
    def test_engine_and_empty_values_dropped(self):
        (self.state / "agent.env").write_text(
            "﻿AGENT_ENGINE=claude\nCLAUDE_CODE_OAUTH_TOKEN=\nANTHROPIC_BASE_URL=https://api.example.com/anthropic\n"
            "ANTHROPIC_AUTH_TOKEN=sk-xxx\nANTHROPIC_MODEL=\nCODEBUDDY_API_KEY=\n", encoding="utf-8")
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "外面带进来的旧令牌"
        try:
            engine, env = run_agent.build_env()
        finally:
            del os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        self.assertEqual(engine, "claude")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)          # 空值、外面的旧值都不传
        self.assertNotIn("ANTHROPIC_MODEL", env)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.example.com/anthropic")
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertTrue(env["PATH"].startswith(str(Path(sys.executable).parent)))  # Agent 的 python 就是这个

    def test_default_engine_and_bad_engine(self):
        engine, _ = run_agent.build_env()          # 没有 agent.env 也能跑，默认 claude
        self.assertEqual(engine, "claude")
        (self.state / "agent.env").write_text("AGENT_ENGINE=gpt\n", encoding="utf-8")
        self.assertEqual(run_agent.main(["-"]), 2)
        self.assertEqual(run_agent.main(["not-a-session-id"]), 2)

    def test_missing_exe(self):
        (self.state / "agent.env").write_text("AGENT_ENGINE=codebuddy\n", encoding="utf-8")
        old = run_agent.find_exe, run_agent.sync_codebuddy_rules
        run_agent.find_exe = lambda name, env: None
        run_agent.sync_codebuddy_rules = lambda: None
        try:
            self.assertEqual(run_agent.main(["-"]), 127)
        finally:
            run_agent.find_exe, run_agent.sync_codebuddy_rules = old


class RunJobTest(TempState):
    def setUp(self):
        super().setUp()
        self.sent = []
        self._notify = run_job.notify
        run_job.notify = lambda title, body: self.sent.append((title, body))

    def tearDown(self):
        run_job.notify = self._notify
        super().tearDown()

    def test_log_fail_once_then_recover(self):
        fail = [sys.executable, "-c", "print('第一行'); import sys; sys.exit(3)"]
        ok = [sys.executable, "-c", "print('好了')"]
        self.assertEqual(run_job.run("demo", 1, fail), 3)
        self.assertEqual(run_job.run("demo", 1, fail), 3)       # 连续失败只告警一次
        self.assertEqual(len(self.sent), 1)
        self.assertIn("退出码 3", self.sent[0][1])
        self.assertIn("第一行", self.sent[0][1])
        self.assertEqual(run_job.run("demo", 1, ok), 0)
        self.assertEqual(self.sent[-1][0], "✅ AgentOS 已恢复：demo")
        log = (self.state / "logs" / "demo.log").read_text(encoding="utf-8")
        self.assertIn("好了", log)
        self.assertIn("<== 退出码 0", log)

    def test_timeout(self):
        slow = [sys.executable, "-c", "import time; time.sleep(30)"]
        self.assertEqual(run_job.run("slow", 0.02, slow), 124)
        self.assertIn("超时", self.sent[0][1])

    def test_python_placeholder(self):
        self.assertEqual(run_job.run("py", 1, ["python", "-c", "print(1)"]), 0)


class DailyReviewTest(unittest.TestCase):
    def test_files_and_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            for rel in ("feishu/2026-11-09/私聊-a.md", "feishu/2026-11-09/文档-周报-abc.md",
                        "mail/2026-11-09/1.md", "feishu/2026-11-08/旧.md", "inbox/2026-11-09/图.png.txt"):
                (raw / rel).parent.mkdir(parents=True, exist_ok=True)
                (raw / rel).write_text("x", encoding="utf-8")
            old = daily_review.RAW
            daily_review.RAW = raw
            try:
                files, docs = daily_review.day_files("2026-11-09")
            finally:
                daily_review.RAW = old
        self.assertEqual(files, ["spaces/me/raw/feishu/2026-11-09/私聊-a.md", "spaces/me/raw/mail/2026-11-09/1.md"])
        self.assertEqual(docs, ["spaces/me/raw/feishu/2026-11-09/文档-周报-abc.md"])
        prompt = daily_review.build_prompt("2026-11-09", files, docs, ["飞书"])
        self.assertIn("昨天是 2026-11-08", prompt)
        self.assertIn("没采到的来源：飞书", prompt)

    def test_parse_result(self):
        ok = '{"type":"system"}\n{"type":"result","is_error":false,"result":"摘要"}\n'
        self.assertEqual(daily_review.parse_result(ok, 0), (True, "摘要"))
        bad = '{"type":"result","is_error":true,"result":"Failed to authenticate: OAuth token revoked"}\n'
        good, why = daily_review.parse_result(bad, 1)
        self.assertFalse(good)
        self.assertIn("OAuth token revoked", why)
        good, why = daily_review.parse_result("找不到 claude 命令", 127)
        self.assertIn("找不到 claude 命令", why)


class SetKeyTest(TempState):
    def test_replace_add_and_keep_others(self):
        f = self.state / "agent.env"
        f.write_text("﻿# 注释\nAGENT_ENGINE=claude\nCLAUDE_CODE_OAUTH_TOKEN=\n", encoding="utf-8")
        self.assertEqual(set_key.main(["agent", "CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-a&b|c"]), 0)
        self.assertEqual(set_key.main(["agent", "NEW_KEY", "x=y\r"]), 0)
        self.assertEqual(f.read_text(encoding="utf-8"),
                         "# 注释\nAGENT_ENGINE=claude\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-a&b|c\nNEW_KEY=x=y\n")
        self.assertEqual(agentos_env.read_env(f)["NEW_KEY"], "x=y")
        self.assertEqual(set_key.main(["agent", "bad name", "v"]), 2)


if __name__ == "__main__":
    unittest.main()
