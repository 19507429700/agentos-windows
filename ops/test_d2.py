"""D2 离线测试：检索（search.py）、邮箱采集（collect_mail.py）、飞书授权与实测（feishu_*.py）。
用假 IMAP、假飞书接口，不碰网络。

运行：/usr/bin/python3 -m unittest ops/test_d2.py -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collect_feishu  # noqa: E402
import collect_mail  # noqa: E402
import feishu_auth  # noqa: E402
import feishu_probe  # noqa: E402
import feishu_user as fu  # noqa: E402
import search  # noqa: E402


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.space = Path(self.tmp.name)
        self.write("raw/feishu/2026-10-30/chat.md", "张总：定金先按 30% 走\n李总：方案下周给\n")
        self.write("raw/mail/2026-11-01/093000-7.md", "# 场地报价\n对方把定金提到 50%，周四前回复张总\n")
        self.write("kb/people/张总.md", "# 张总\n展会场地负责人\n")
        self.db = search.open_index(self.space)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def write(self, rel, text):
        p = self.space / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def find(self, *terms, **kw):
        search.refresh(self.db, self.space)
        rows, total = search.search(self.db, list(terms), **kw)
        return [f"{r[0]}:{r[1]}" for r in rows], total

    def test_short_and_long_terms_with_line_numbers(self):
        hits, total = self.find("张总")  # 2 个字：走 LIKE
        self.assertEqual(total, 3)
        self.assertEqual(hits[0], "raw/mail/2026-11-01/093000-7.md:2")  # 新日期在前
        self.assertIn("raw/feishu/2026-10-30/chat.md:1", hits)
        hits, _ = self.find("定金", "50%")  # 同一行同时出现
        self.assertEqual(hits, ["raw/mail/2026-11-01/093000-7.md:2"])
        hits, _ = self.find("场地负责人")  # ≥3 个字：走 FTS
        self.assertEqual(hits, ["kb/people/张总.md:2"])

    def test_filters(self):
        self.assertEqual(self.find("张总", source="feishu")[0], ["raw/feishu/2026-10-30/chat.md:1"])
        hits, _ = self.find("张总", since="2026-10-31")
        self.assertNotIn("raw/feishu/2026-10-30/chat.md:1", hits)
        self.assertIn("kb/people/张总.md:1", hits)  # kb 没有日期，不受 since 影响

    def test_incremental_refresh(self):
        self.find("张总")
        self.assertEqual(search.refresh(self.db, self.space), 0)  # 没变化不重建
        p = self.write("raw/feishu/2026-10-30/chat.md", "王总：改天再说\n")
        os.utime(p, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.find("李总")[1], 0)
        self.assertEqual(self.find("王总")[1], 1)
        p.unlink()
        self.assertEqual(self.find("王总")[1], 0)

    def test_special_chars_are_literal(self):
        self.write("raw/inbox/2026-11-02/note.txt", 'a"b 100%_x\n')
        self.assertEqual(self.find('a"b')[1], 1)
        self.assertEqual(self.find("0%_")[1], 1)
        self.assertEqual(self.find("%")[1], 3)


def make_mail(subject, sender, body, when, **headers):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, sender, "me@example.com"
    m["Date"] = format_datetime(when)
    m["Message-ID"] = f"<{abs(hash(subject))}@example.com>"
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    """只实现 collect_folder 用到的几个调用，行为按 imaplib 返回格式。"""

    def __init__(self, mails, uidvalidity=100):
        self.mails = dict(mails)  # uid → bytes
        self.uidvalidity = uidvalidity
        self.fetched, self.readonly = [], None

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.mails)).encode()]

    def response(self, code):
        if code == "UIDVALIDITY":
            return code, [str(self.uidvalidity).encode()]
        if code == "UIDNEXT":
            return code, [str(max(self.mails, default=0) + 1).encode()]
        return code, [None]

    def uid(self, cmd, *args):
        if cmd == "search":
            if args[1] == "SINCE":
                uids = sorted(self.mails)
            else:  # "UID", "N:*"
                lo = int(args[2].split(":")[0])
                uids = [u for u in sorted(self.mails) if u >= lo] or [max(self.mails)]  # 模拟 N:* 的怪癖
            return "OK", [" ".join(map(str, uids)).encode()]
        if cmd == "fetch":
            uid = int(args[0])
            assert "PEEK" in args[1], "必须用 BODY.PEEK，不能改已读状态"
            self.fetched.append(uid)
            return "OK", [(f"1 (UID {uid} BODY[] {{9}}".encode(), self.mails[uid]), b")"]
        raise AssertionError(cmd)


class CollectMailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / "raw"
        now = datetime(2026, 11, 2, 14, 3).astimezone()
        self.imap = FakeIMAP({
            5: make_mail("合同修改", "王总 <wang@client.com>", "定金改成 50%，请周四前回复。", now),
            6: make_mail("双十一大促", "shop <promo@shop.com>", "全场五折", now, List_Unsubscribe="<mailto:u@shop.com>"),
            7: make_mail("登录提醒", "安全中心 <no-reply@service.com>", "新设备登录", now),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def files(self):
        return sorted(p.relative_to(self.raw).as_posix() for p in self.raw.rglob("*.md"))

    def test_first_run_filters_and_saves_readonly(self):
        st = {}
        stats = collect_mail.collect_folder(self.imap, "INBOX", st, self.raw, 7)
        self.assertTrue(self.imap.readonly)
        self.assertEqual(stats["saved"], 1)
        self.assertEqual(sum(stats["filtered"].values()), 2)
        self.assertEqual(self.files(), ["mail/2026-11-02/140300-5.md"])
        md = (self.raw / "mail/2026-11-02/140300-5.md").read_text()
        self.assertIn("# 合同修改", md)
        self.assertIn("王总 <wang@client.com>", md)
        self.assertIn("UID: 5", md)
        self.assertIn("定金改成 50%", md)
        self.assertEqual(st, {"uidvalidity": 100, "last_uid": 7})

    def test_own_monitoring_mail_filtered(self):
        msg = message_from_bytes(make_mail("Log in to Healthchecks.io", '"Healthchecks.io" <healthchecks.io@healthchecks.io>',
                                           "To log into Healthchecks.io, please open the link below", datetime(2026, 11, 3, 17, 56)),
                                 policy=policy.default)
        self.assertEqual(collect_mail.is_bulk(msg), "通知类发件人")
        ok = message_from_bytes(make_mail("问个事", "张三 <healthchecks@client.com>", "你好", datetime(2026, 11, 3)),
                                policy=policy.default)
        self.assertIsNone(collect_mail.is_bulk(ok))  # 只挡 healthchecks.io 这个域名

    def test_incremental_only_new(self):
        st = {}
        collect_mail.collect_folder(self.imap, "INBOX", st, self.raw, 7)
        self.imap.fetched.clear()
        stats = collect_mail.collect_folder(self.imap, "INBOX", st, self.raw, 7)
        self.assertEqual(self.imap.fetched, [])  # N:* 返回旧的最大 UID，要被滤掉
        self.assertEqual(stats["saved"], 0)
        self.imap.mails[8] = make_mail("回复：报价", "李总 <li@x.com>", "收到", datetime(2026, 11, 3, 9, 0).astimezone())
        collect_mail.collect_folder(self.imap, "INBOX", st, self.raw, 7)
        self.assertEqual(self.imap.fetched, [8])
        self.assertIn("mail/2026-11-03/090000-8.md", self.files())

    def test_empty_first_run_does_not_pull_history_later(self):
        self.imap.uid = lambda cmd, *a: ("OK", [b""]) if a[1] == "SINCE" else FakeIMAP.uid(self.imap, cmd, *a)
        st = {}
        collect_mail.collect_folder(self.imap, "INBOX", st, self.raw, 7)
        self.assertEqual(st["last_uid"], 7)  # 起点定在当前最新一封

    def test_uidvalidity_change_restarts(self):
        st = {"uidvalidity": 1, "last_uid": 999}
        collect_mail.collect_folder(self.imap, "INBOX", st, self.raw, 7)
        self.assertEqual(st["uidvalidity"], 100)
        self.assertEqual(self.files(), ["mail/2026-11-02/140300-5.md"])

    def test_never_overwrites_raw(self):
        p = self.raw / "mail/2026-11-02/140300-5.md"
        p.parent.mkdir(parents=True)
        p.write_text("原文")
        stats = collect_mail.collect_folder(self.imap, "INBOX", {}, self.raw, 7)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(p.read_text(), "原文")

    def test_html_body(self):
        m = EmailMessage()
        m["Subject"], m["From"] = "周报", "a@b.com"
        m.set_content("<html><style>x{}</style><p>第一段</p><p>第二段 &amp; 结尾</p></html>", subtype="html")
        self.assertEqual(collect_mail.body_text(m), "第一段\n第二段 & 结尾")

    def test_main_reports_missing_config(self):
        with tempfile.TemporaryDirectory() as d:
            old = collect_mail.STATE_DIR
            collect_mail.STATE_DIR = Path(d)
            saved = {k: os.environ.pop(k, None) for k in ("MAIL_IMAP_HOST", "MAIL_USER", "MAIL_PASSWORD")}
            try:
                self.assertEqual(collect_mail.main([]), 2)
            finally:
                collect_mail.STATE_DIR = old
                os.environ.update({k: v for k, v in saved.items() if v is not None})

    def test_state_is_json(self):
        st = {}
        collect_mail.collect_folder(self.imap, "INBOX", st, self.raw, 7)
        self.assertEqual(json.loads(json.dumps(st)), st)


class FakeFeishu:
    """记录请求的假飞书：token 接口按 refresh_token 轮换，messages 按会话返回预设结果。"""

    def __init__(self):
        self.calls, self.n = [], 0
        self.used_refresh = set()
        self.chat_errors = {}

    def __call__(self, method, url, form=None, headers=None, json_body=None):
        self.calls.append((method, url, form))
        if url == fu.TOKEN_URL:
            if form["grant_type"] == "refresh_token":
                if form["refresh_token"] in self.used_refresh:
                    return {"code": 20064, "error_description": "refresh token revoked"}
                self.used_refresh.add(form["refresh_token"])
            self.n += 1
            return {"code": 0, "access_token": f"u-{self.n}", "expires_in": 7200,
                    "refresh_token": f"r-{self.n}", "refresh_token_expires_in": 604800,
                    "scope": " ".join(fu.SCOPES)}
        if "/im/v1/messages" in url:
            for cid, code in self.chat_errors.items():
                if cid in url:
                    return {"code": code, "msg": "The chat type is not supported."}
            return {"code": 0, "data": {"items": [{"create_time": "1761955200000"}], "has_more": False}}
        if "/pages" in url:
            page = 2 if "page_token=p2" in url else 1
            return {"code": 0, "data": {"items": [page * 10 + i for i in range(3)],
                                        "has_more": page == 1, "page_token": "p2"}}
        raise AssertionError(url)


class FeishuUserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = fu.STATE_DIR
        fu.STATE_DIR = Path(self.tmp.name)
        self.saved_env = {k: os.environ.get(k) for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")}
        os.environ.update(FEISHU_APP_ID="cli_x", FEISHU_APP_SECRET="s")
        self.http = FakeFeishu()

    def tearDown(self):
        fu.STATE_DIR = self.old_state
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_no_token_tells_to_authorize(self):
        with self.assertRaisesRegex(fu.FeishuError, "feishu_auth"):
            fu.access_token(self.http)

    def test_exchange_saves_private_file(self):
        fu.exchange_code("c1", self.http, now=1000)
        self.assertEqual(oct(fu.token_file().stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.http.calls[0][2]["redirect_uri"], fu.REDIRECT_URI)
        self.assertEqual(fu.access_token(self.http, now=1000), "u-1")
        self.assertEqual(len(self.http.calls), 1)  # 没过期不续期

    def test_refresh_rotates_single_use_token(self):
        fu.exchange_code("c1", self.http, now=1000)
        self.assertEqual(fu.access_token(self.http, now=1000 + 7200 - 60), "u-2")  # 快过期，续期
        self.assertEqual(self.http.calls[-1][2]["refresh_token"], "r-1")
        self.assertEqual(fu.access_token(self.http, now=1000 + 2 * 7200), "u-3")  # 用的是新的 r-2
        self.assertEqual(self.http.calls[-1][2]["refresh_token"], "r-2")

    def test_refresh_expired_asks_reauth(self):
        fu.exchange_code("c1", self.http, now=1000)
        with self.assertRaisesRegex(fu.FeishuError, "过期"):
            fu.access_token(self.http, now=1000 + 604800 + 1)

    def test_paginate_and_limit(self):
        fu.exchange_code("c1", self.http, now=time.time())
        self.assertEqual(list(fu.paginate("/pages", {}, self.http)), [10, 11, 12, 20, 21, 22])
        self.assertEqual(list(fu.paginate("/pages", {}, self.http, limit=4)), [10, 11, 12, 20])

    def test_retries_once_on_internal_error(self):
        fu.exchange_code("c1", self.http, now=time.time())
        old, fu.RETRY_DELAY_SEC = fu.RETRY_DELAY_SEC, 0
        flaky = {"n": 0}

        def http(method, url, form=None, headers=None, json_body=None):
            if "/flaky" in url:
                flaky["n"] += 1
                return {"code": 2200, "msg": "Internal Error"} if flaky["n"] == 1 else {"code": 0, "data": {"ok": 1}}
            if "/broken" in url:
                return {"code": 2200, "msg": "Internal Error"}
            return self.http(method, url, form, headers, json_body)
        try:
            self.assertEqual(fu.api_get("/flaky", None, http), {"ok": 1})
            self.assertEqual(flaky["n"], 2)
            with self.assertRaisesRegex(fu.FeishuError, "2200"):
                fu.api_get("/broken", None, http)  # 只重试一次，还不行就报错
        finally:
            fu.RETRY_DELAY_SEC = old

    def test_probe_reports_unreadable_chat(self):
        fu.exchange_code("c1", self.http, now=time.time())
        self.http.chat_errors["oc_secret"] = 231203
        ok = feishu_probe.probe_chat({"chat_id": "oc_a", "name": "项目群"}, time.time(), self.http)
        self.assertEqual((ok["oldest"], ok["recent"]), ("2025-11-01", "1"))
        bad = feishu_probe.probe_chat({"chat_id": "oc_secret", "name": "保密群"}, time.time(), self.http)
        self.assertIn("231203", bad["error"])

    def test_callback_checks_state(self):
        cb = feishu_auth.Callback("s1")
        self.assertEqual(cb.handle("/feishu/callback?code=x&state=evil")[0], 400)
        self.assertFalse(cb.done.is_set())
        self.assertEqual(cb.handle("/other")[0], 404)
        self.assertEqual(cb.handle("/feishu/callback?code=abc&state=s1")[0], 200)
        self.assertEqual((cb.code, cb.done.is_set()), ("abc", True))
        url = feishu_auth.authorize_url("cli_x", "s1")
        self.assertIn("offline_access", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A18080%2Ffeishu%2Fcallback", url)


def fs_msg(mid, ms, sender, text=None, **kw):
    item = {"message_id": mid, "create_time": str(ms), "msg_type": kw.pop("msg_type", "text"),
            "sender": {"id": sender, "id_type": "open_id", "sender_type": kw.pop("sender_type", "user")},
            "body": {"content": json.dumps(kw.pop("content", {"text": text}), ensure_ascii=False)}}
    item.update(kw)
    return item


class FakeChats(FakeFeishu):
    """会话 → 消息列表；按 start_time 过滤、每页 2 条；通讯录按 open_id 给名字。"""

    def __init__(self, chats, people):
        super().__init__()
        self.chats, self.people, self.contact_calls = chats, people, 0
        self.search_pages = [[]]

    def __call__(self, method, url, form=None, headers=None, json_body=None):
        if url == fu.TOKEN_URL:
            return super().__call__(method, url, form, headers)
        from urllib.parse import parse_qs, urlparse
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path.endswith("/im/v1/messages"):
            items = [m for m in self.chats[q["container_id"]] if int(m["create_time"]) // 1000 >= int(q["start_time"])]
            start = int(q.get("page_token", 0))
            page = items[start:start + 2]
            more = start + 2 < len(items)
            return {"code": 0, "data": {"items": page, "has_more": more, "page_token": str(start + 2) if more else ""}}
        if "/contact/v3/users/" in u.path:
            self.contact_calls += 1
            oid = u.path.rsplit("/", 1)[1]
            if oid not in self.people:
                return {"code": 41050, "msg": "no user authority"}
            return {"code": 0, "data": {"user": {"name": self.people[oid]}}}
        if u.path.endswith("/im/v1/messages/search"):
            self.last_search = json_body
            i = int(q.get("page_token", 0))
            more = i + 1 < len(self.search_pages)
            return {"code": 0, "data": {"items": self.search_pages[i], "has_more": more,
                                        "page_token": str(i + 1) if more else ""}}
        raise AssertionError(url)


class CollectFeishuTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = fu.STATE_DIR
        fu.STATE_DIR = Path(self.tmp.name)
        self.saved_env = {k: os.environ.get(k) for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")}
        os.environ.update(FEISHU_APP_ID="cli_x", FEISHU_APP_SECRET="s")
        self.raw = Path(self.tmp.name) / "raw"
        self.t0 = datetime(2026, 11, 3, 9, 30).timestamp()
        ms = int(self.t0 * 1000)
        self.http = FakeChats({
            "oc_group01": [
                fs_msg("m1", ms, "ou_wang", "定金先按 30%"),
                fs_msg("m2", ms + 60000, "ou_me", "@_user_1 收到", mentions=[{"key": "@_user_1", "id": "ou_wang", "name": "王总"}]),
                fs_msg("m3", ms + 120000, "ou_wang", "撤回了", deleted=True),
                fs_msg("m4", ms + 180000, "cli_bot", msg_type="interactive", sender_type="app",
                       content={"title": "日程提醒"}),
                fs_msg("m5", ms + 240000, "ou_ext", msg_type="post",
                       content={"title": "方案", "content": [[{"tag": "text", "text": "第一段"}], [{"tag": "img"}]]}),
            ],
            "oc_p2p0001": [fs_msg("p1", ms, "ou_me", "学姐在吗"), fs_msg("p2", ms + 1000, "ou_wei", "在")],
        }, {"ou_wang": "王总", "ou_me": "黄佳旭", "ou_wei": "魏玉珊"})
        fu.exchange_code("c", self.http, now=time.time())
        self.st = collect_feishu.load_state()

    def tearDown(self):
        fu.STATE_DIR = self.old_state
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def read(self, name):
        return (self.raw / "feishu" / "2026-11-03" / name).read_text()

    def test_first_run_writes_readable_lines(self):
        n = collect_feishu.collect_chat("oc_group01", "项目 群/A", "群聊", self.st, self.raw, 30,
                                        self.http, now=self.t0 + 3600)
        self.assertEqual(n, 4)  # 已删除的不存
        text = self.read("项目_群_A-roup01.md")
        self.assertIn("# 项目 群/A（2026-11-03）", text)
        self.assertIn("- 09:30 王总：定金先按 30%", text)
        self.assertIn("- 09:31 黄佳旭：@王总 收到", text)
        self.assertIn("- 09:33 机器人：[卡片] 日程提醒", text)
        self.assertIn("- 09:34 ou_ext：方案\n  第一段\n  [图片]", text)  # 外部联系人查不到名字就用 ID
        self.assertNotIn("撤回了", text)

    def test_incremental_no_duplicates_and_name_cache(self):
        args = ("oc_group01", "项目群", "群聊", self.st, self.raw, 30, self.http)
        collect_feishu.collect_chat(*args, now=self.t0 + 3600)
        calls = self.http.contact_calls
        self.assertEqual(collect_feishu.collect_chat(*args, now=self.t0 + 7200), 0)
        self.http.chats["oc_group01"].append(fs_msg("m6", int(self.t0 * 1000) + 300000, "ou_wang", "周四前回复"))
        self.assertEqual(collect_feishu.collect_chat(*args, now=self.t0 + 7200), 1)
        self.assertEqual(self.http.contact_calls, calls)  # 名字已缓存
        text = self.read("项目群-roup01.md")
        self.assertEqual(text.count("定金先按"), 1)
        self.assertTrue(text.rstrip().endswith("- 09:35 王总：周四前回复"))

    def test_p2p_named_after_other_person(self):
        collect_feishu.collect_chat("oc_p2p0001", "", "私聊", self.st, self.raw, 30, self.http,
                                    now=self.t0 + 3600, my_id="ou_me")
        self.assertEqual(self.st["p2p"]["oc_p2p0001"], "私聊-魏玉珊")
        self.assertIn("- 09:30 黄佳旭：学姐在吗", self.read("私聊-魏玉珊-2p0001.md"))

    def test_docs_from_links_versioned_and_failures_backed_off(self):
        tok, wtok, bad = "Abcdefghijklmnopqrst12", "Wikinodetoken12345678x", "Nopermissiontoken12345"
        (self.raw / "feishu/2026-11-03").mkdir(parents=True)
        (self.raw / "feishu/2026-11-03/群-x.md").write_text(
            f"- 09:30 王总：看下 https://a.feishu.cn/docx/{tok} 和 https://a.feishu.cn/wiki/{wtok}\n"
            f"- 09:31 王总：https://a.feishu.cn/wiki/{bad}\n")
        rev = {"n": 1}

        def http(method, url, form=None, headers=None, json_body=None):
            if url == fu.TOKEN_URL:
                return self.http(method, url, form, headers)
            if "get_node" in url:
                if bad in url:
                    return {"code": 131006, "msg": "permission denied"}
                return {"code": 0, "data": {"node": {"obj_type": "docx", "obj_token": "Docfromwiki1234567890", "title": "方案"}}}
            if "/drive/v1/files" in url or "my_library" in url:
                return {"code": 99991679, "msg": "no permission"}  # 还没开云盘权限：不影响读链接里的文档
            if "/blocks" in url:
                return {"code": 0, "data": {"items": [
                    {"block_id": "p", "block_type": 1, "children": ["t"]},
                    {"block_id": "t", "block_type": 2, "parent_id": "p",
                     "text": {"elements": [{"text_run": {"content": f"正文 v{rev['n']}"}}]}}]}}
            return {"code": 0, "data": {"document": {"revision_id": rev["n"], "title": "合同"}}}
        t = self.t0
        out = collect_feishu.collect_docs(self.st, self.raw, http, now=t)
        self.assertIn("新存 2 份", out)
        self.assertIn("读不了 1 份", out)
        self.assertIn("列云文档出错", out)
        files = sorted(p.name for p in (self.raw / "feishu").rglob("文档-*.md"))
        self.assertEqual(len(files), 2)
        self.assertIn("正文 v1", (self.raw / "feishu/2026-11-03" / files[0]).read_text())
        self.assertIn("新存 0 份", collect_feishu.collect_docs(self.st, self.raw, http, now=t + 4000))  # 没改过
        rev["n"] = 2
        self.assertIn("新存 2 份", collect_feishu.collect_docs(self.st, self.raw, http, now=t + 8000))  # 改过：另存
        self.assertEqual(len(list((self.raw / "feishu").rglob("文档-*.md"))), 4)
        self.assertIn("permission", self.st["docs"][bad]["error"])
        self.assertIn("读不了 0 份", collect_feishu.collect_docs(self.st, self.raw, http, now=t + 12000))  # 一天内不重试

    def test_old_format_docs_reread_once(self):
        tok = "Abcdefghijklmnopqrst12"
        (self.raw / "feishu/2026-11-03").mkdir(parents=True)
        (self.raw / "feishu/2026-11-03/群-x.md").write_text(f"- 09:30 王总：https://a.feishu.cn/docx/{tok}\n")
        self.st["docs"] = {tok: {"rev": 1, "checked": self.t0, "title": "合同"}}  # 旧存法存过，版本没变

        def http(method, url, form=None, headers=None, json_body=None):
            if url == fu.TOKEN_URL:
                return self.http(method, url, form, headers)
            if "/drive/v1/files" in url or "my_library" in url:
                return {"code": 0, "data": {}}
            if "/blocks" in url:
                return {"code": 0, "data": {"items": [{"block_id": "p", "block_type": 1, "children": []}]}}
            return {"code": 0, "data": {"document": {"revision_id": 1, "title": "合同"}}}
        self.assertIn("新存 1 份", collect_feishu.collect_docs(self.st, self.raw, http, now=self.t0 + 10))
        self.assertIn("新存 0 份", collect_feishu.collect_docs(self.st, self.raw, http, now=self.t0 + 5000))

    def test_docx_blocks_tables_and_images(self):
        tok = "Docwithimagetoken12345"
        (self.raw / "feishu/2026-11-03").mkdir(parents=True)
        (self.raw / "feishu/2026-11-03/群-x.md").write_text(f"- 09:30 王总：https://a.feishu.cn/docx/{tok}\n")
        el = lambda s, **st: {"elements": [{"text_run": {"content": s, "text_element_style": st}}]}  # noqa: E731
        blocks = [
            {"block_id": "p", "block_type": 1, "children": ["h", "t", "b1", "b2", "o1", "o2", "img", "tb", "code"]},
            {"block_id": "h", "block_type": 3, "heading1": el("报价")},
            {"block_id": "t", "block_type": 2, "text": {"elements": [
                {"text_run": {"content": "总价 ", "text_element_style": {}}},
                {"text_run": {"content": "3 万", "text_element_style": {"bold": True}}},
                {"text_run": {"content": "官网", "text_element_style": {"link": {"url": "https%3A%2F%2Fa.com"}}}}]}},
            {"block_id": "b1", "block_type": 12, "parent_id": "p", "bullet": el("甲方"), "children": ["b1c"]},
            {"block_id": "b1c", "block_type": 12, "parent_id": "b1", "bullet": el("联系人王总")},
            {"block_id": "b2", "block_type": 12, "parent_id": "p", "bullet": el("乙方")},
            {"block_id": "o1", "block_type": 13, "parent_id": "p", "ordered": el("签约")},
            {"block_id": "o2", "block_type": 13, "parent_id": "p", "ordered": el("付款")},
            {"block_id": "img", "block_type": 27, "image": {"token": "boxcnIMG1"}},
            {"block_id": "tb", "block_type": 31, "table": {"property": {"row_size": 2, "column_size": 2},
                                                          "cells": ["c1", "c2", "c3", "c4"]}},
            {"block_id": "c1", "block_type": 32, "children": ["c1t"]}, {"block_id": "c1t", "block_type": 2, "text": el("项目")},
            {"block_id": "c2", "block_type": 32, "children": ["c2t"]}, {"block_id": "c2t", "block_type": 2, "text": el("金额")},
            {"block_id": "c3", "block_type": 32, "children": ["c3t"]}, {"block_id": "c3t", "block_type": 2, "text": el("培训|3次")},
            {"block_id": "c4", "block_type": 32, "children": ["c4i"]}, {"block_id": "c4i", "block_type": 27, "image": {"token": "boxcnIMG2"}},
            {"block_id": "code", "block_type": 14, "code": el("print(1)\nprint(2)")},
        ]

        def http(method, url, form=None, headers=None, json_body=None):
            if url == fu.TOKEN_URL:
                return self.http(method, url, form, headers)
            if "/drive/v1/files" in url or "my_library" in url:
                return {"code": 0, "data": {}}
            if "/blocks" in url:
                return {"code": 0, "data": {"items": blocks}}
            return {"code": 0, "data": {"document": {"revision_id": 7, "title": "报价单"}}}
        downloads = []

        def fetch(url, headers):
            downloads.append(url)
            if "IMG2" in url:
                raise fu.FeishuError("1061004 forbidden")
            return b"\x89PNG\r\n\x1a\nfake", "image/png"
        out = collect_feishu.collect_docs(self.st, self.raw, http, now=self.t0, fetch=fetch)
        self.assertIn("新存 1 份（图片 1 张）", out)
        md = self.read(f"文档-报价单-{tok[-6:]}.md")
        img_rel = f"spaces/me/raw/feishu/2026-11-03/文档-报价单-{tok[-6:]}-图片/01.png"
        for want in ("## 报价", "总价 **3 万**[官网](https://a.com)", "- 甲方\n  - 联系人王总\n- 乙方",
                     "1. 签约\n2. 付款", f"![图1]({img_rel})", "| 项目 | 金额 |", "| 培训\\|3次 | [图2：没下载下来",
                     "```\nprint(1)\nprint(2)\n```", "图片 1 张"):
            self.assertIn(want, md)
        self.assertEqual((self.raw.parent / img_rel.replace("spaces/me/raw/", "raw/")).read_bytes()[:4], b"\x89PNG")
        self.assertTrue(downloads[0].endswith("/drive/v1/medias/boxcnIMG1/download"))
        # 版本没变：不重读；重新授权（权限变了）后，有图片没下载下来的那篇重读一次
        self.assertIn("新存 0 份", collect_feishu.collect_docs(self.st, self.raw, http, now=self.t0 + 4000, fetch=fetch))
        tok_file = fu.token_file()
        tok_data = json.loads(tok_file.read_text())
        tok_data["scope"] = tok_data.get("scope", "") + " docs:document.media:download"
        tok_file.write_text(json.dumps(tok_data))
        self.assertIn("新存 1 份", collect_feishu.collect_docs(self.st, self.raw, http, now=self.t0 + 4100, fetch=fetch))

    def test_sheet_bitable_and_my_docs_listing(self):
        def http(method, url, form=None, headers=None, json_body=None):
            if url == fu.TOKEN_URL:
                return self.http(method, url, form, headers)
            if url.endswith("/drive/v1/files?page_size=200"):
                return {"code": 0, "data": {"files": [
                    {"type": "folder", "token": "fldr1", "name": "资料"},
                    {"type": "sheet", "token": "Shtcnsheettoken1234567", "name": "报价表"},
                    {"type": "mindnote", "token": "Mindnotetoken123456789", "name": "脑图"}], "has_more": False}}
            if "folder_token=fldr1" in url:
                return {"code": 0, "data": {"files": [
                    {"type": "shortcut", "token": "s1", "name": "快捷",
                     "shortcut_info": {"target_type": "bitable", "target_token": "Bascnbitabletoken12345"}}]}}
            if url.endswith("/wiki/v2/spaces/my_library"):
                return {"code": 0, "data": {"space": {"space_id": "777"}}}
            if "/wiki/v2/spaces/777/nodes" in url:
                return {"code": 0, "data": {"items": [] if "parent_node_token" in url else [
                    {"node_token": "Wikinodedocx1234567890", "obj_type": "docx", "obj_token": "Docobj123456789012345",
                     "title": "周报", "has_child": False}]}}
            if "/docx/v1/documents/Docobj123456789012345/blocks" in url:
                return {"code": 0, "data": {"items": [{"block_id": "p", "block_type": 1, "children": []}]}}
            if "/docx/v1/documents/Docobj123456789012345" in url:
                return {"code": 0, "data": {"document": {"revision_id": 3, "title": "周报"}}}
            if "metas/batch_query" in url:
                return {"code": 0, "data": {"metas": [{"latest_modify_time": "1762300000"}]}}
            if url.endswith("/sheets/v3/spreadsheets/Shtcnsheettoken1234567"):
                return {"code": 0, "data": {"spreadsheet": {"title": "报价表"}}}
            if "/sheets/query" in url:
                return {"code": 0, "data": {"sheets": [{"sheet_id": "s1", "title": "Q4",
                                                         "grid_properties": {"row_count": 100, "column_count": 30}}]}}
            if "/sheets/v2/spreadsheets/" in url:
                self.assertIn("s1%21A1%3AAD100", url)
                return {"code": 0, "data": {"valueRange": {"values": [
                    ["客户", "金额", None], ["A 公司", 30000, None], [[{"type": "text", "text": "B"}, {"type": "text", "text": " 公司"}], 1.5, None], [None, None, None]]}}}
            if url.endswith("/bitable/v1/apps/Bascnbitabletoken12345"):
                return {"code": 0, "data": {"app": {"name": "客户库", "revision": 12}}}
            if "/tables?" in url:
                return {"code": 0, "data": {"items": [{"table_id": "tbl1", "name": "客户"}]}}
            if "/fields?" in url:
                return {"code": 0, "data": {"items": [{"field_name": "名称", "type": 1}, {"field_name": "签约日", "type": 5},
                                                     {"field_name": "照片", "type": 17}, {"field_name": "标签", "type": 4}]}}
            if "/records?" in url:
                return {"code": 0, "data": {"items": [{"fields": {
                    "名称": [{"type": "text", "text": "A 公司"}], "签约日": 1762300000000,
                    "照片": [{"file_token": "f1", "name": "门头.jpg", "type": "image/jpeg", "url": "https://open.feishu.cn/x/f1"},
                             {"file_token": "f2", "name": "合同.pdf", "type": "application/pdf"}],
                    "标签": ["老客户", "重点"]}}]}}
            raise AssertionError(url)
        out = collect_feishu.collect_docs(self.st, self.raw, http, now=self.t0,
                                          fetch=lambda url, h: (b"\xff\xd8\xffjpeg", "image/jpeg"))
        self.assertIn("新存 3 份（图片 1 张），读不了 1 份", out)
        self.assertIn("暂不支持", self.st["docs"]["Mindnotetoken123456789"]["error"])
        sheet = self.read("表格-报价表-234567.md")
        self.assertIn("## Q4\n\n| 客户 | 金额 |\n|---|---|\n| A 公司 | 30000 |\n| B 公司 | 1.5 |", sheet)
        base = self.read("多维表格-客户库-n12345.md")
        self.assertIn("| A 公司 | 2025-11-05", base)
        self.assertIn("![图1](spaces/me/raw/feishu/2026-11-03/多维表格-客户库-n12345-图片/01.jpg) [附件] 合同.pdf", base)
        self.assertIn("| 老客户、重点 |", base)
        self.assertTrue((self.raw / "feishu/2026-11-03/文档-周报-567890.md").exists())
        self.assertIn("新存 0 份", collect_feishu.collect_docs(self.st, self.raw, http, now=self.t0 + 4000))

    def test_minutes_transcript_saved_once_and_failures_backed_off(self):
        ok, bad = "obcnq3b9jl72l83w4f149w9c", "obcnnotreadyyet123456789"
        (self.raw / "feishu/2026-11-03").mkdir(parents=True)
        (self.raw / "feishu/2026-11-03/群-x.md").write_text(
            f"- 10:00 王总：会议妙记 https://a.feishu.cn/minutes/{ok}\n- 10:01 王总：还有 https://a.feishu.cn/minutes/{bad}\n")
        fetched = []

        def fetch(url, headers):
            fetched.append(url)
            self.assertIn("file_format=txt", url)
            self.assertTrue(headers["Authorization"].startswith("Bearer u-"))
            if bad in url:
                raise fu.FeishuError("2091005 permission deny")
            return "说话人 1 00:00:05\n定金按 30%\n"
        t = self.t0
        self.assertIn("新存 1 篇，读不了 1 篇", collect_feishu.collect_minutes(self.st, self.raw, self.http, fetch, now=t))
        text = self.read(f"妙记-{ok[-6:]}.md")
        self.assertIn("定金按 30%", text)
        self.assertIn("链接最早出现在 spaces/me/raw/feishu/2026-11-03/群-x.md", text)
        n = len(fetched)
        self.assertIn("新存 0 篇，读不了 0 篇", collect_feishu.collect_minutes(self.st, self.raw, self.http, fetch, now=t + 3600))
        self.assertEqual(len(fetched), n)  # 存过的不再读，失败的一天内不重试
        collect_feishu.collect_minutes(self.st, self.raw, self.http, fetch, now=t + 90000)
        self.assertEqual(fetched[n:], [u for u in fetched[n:] if bad in u])  # 一天后只重试失败的那个
        self.assertEqual(len(fetched), n + 1)

    def test_minutes_not_ready_retried_next_run(self):
        tok = "obcnz39k8v488cndy3w8r1v8"
        (self.raw / "feishu/2026-11-03").mkdir(parents=True)
        (self.raw / "feishu/2026-11-03/私聊-x.md").write_text(f"- 21:29 我：https://a.feishu.cn/minutes/{tok}\n")
        ready = {"v": False}

        def fetch(url, headers):
            if not ready["v"]:
                raise fu.FeishuError("2091003 minute not ready , try later")
            return "文字记录"
        self.assertIn("读不了 1 篇", collect_feishu.collect_minutes(self.st, self.raw, self.http, fetch, now=self.t0))
        ready["v"] = True
        self.assertIn("新存 1 篇", collect_feishu.collect_minutes(self.st, self.raw, self.http, fetch, now=self.t0 + 600))

    def test_api_get_text_retries_and_raises(self):
        calls = []

        def fetch(url, headers):
            calls.append(url)
            if len(calls) == 1:
                raise fu.FeishuError("网络错误（TimeoutError）：x")
            return "文字"
        old, fu.RETRY_DELAY_SEC = fu.RETRY_DELAY_SEC, 0
        try:
            self.assertEqual(fu.api_get_text("/minutes/v1/minutes/t/transcript", None, self.http, fetch), "文字")
            self.assertEqual(len(calls), 2)

            def denied(url, headers):
                raise fu.FeishuError("2091005 permission deny")
            with self.assertRaises(fu.FeishuError):
                fu.api_get_text("/x", None, self.http, denied)
        finally:
            fu.RETRY_DELAY_SEC = old

    def test_calendar_written(self):
        def http(method, url, form=None, headers=None, json_body=None):
            if url == fu.TOKEN_URL:
                return self.http(method, url, form, headers)
            if url.endswith("/calendars/primary"):
                return {"code": 0, "data": {"calendars": [{"calendar": {"calendar_id": "cal1"}}]}}
            ts = int(self.t0) + 3600
            return {"code": 0, "data": {"items": [
                {"summary": "见张总", "status": "confirmed", "start_time": {"timestamp": str(ts)},
                 "end_time": {"timestamp": str(ts + 1800)}, "location": {"name": "公司"}},
                {"summary": "取消了", "status": "cancelled", "start_time": {"timestamp": str(ts)}, "end_time": {}},
                {"summary": "生日", "status": "confirmed", "start_time": {"date": "2026-11-05"}, "end_time": {"date": "2026-11-06"}}]}}
        self.assertEqual(collect_feishu.collect_calendar(self.raw, http, now=self.t0), "2 个")
        text = (self.raw / "feishu/日程/最近日程.md").read_text()
        self.assertIn("- 2026-11-03 10:30–11:00 见张总（公司）", text)
        self.assertIn("- 2026-11-05 全天 生日", text)
        self.assertNotIn("取消了", text)

    def test_discover_p2p_via_search(self):
        self.http.search_pages = [[{"meta_data": {"chat_id": "oc_a", "is_p2p_chat": True}}],
                                  [{"meta_data": {"chat_id": "oc_b"}}, {"meta_data": {"chat_id": "oc_a"}}]]
        collect_feishu.discover_p2p(self.st, 30, self.http, now=self.t0)
        self.assertEqual(sorted(self.st["p2p"]), ["oc_a", "oc_b"])
        self.assertEqual(self.http.last_search["filter"]["chat_type"], "p2p")
        self.assertTrue(self.st["p2p_search_since"].startswith("2026-11-03T09:30"))


if __name__ == "__main__":
    unittest.main()
