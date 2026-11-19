"""微信采集（Windows 版）的离线测试：不碰真微信。

运行：python -m unittest ops/test_wechat.py -v
真库上的对照（解密结果和 SQLCipher 官方库逐表比行数、比最新消息）在 Mac 上做过，见提交说明。
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wechat_db as w  # noqa: E402

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # 没装 cryptography 时跳过要加密的测试
    Cipher = None


def encrypt_page(key: bytes, plain: bytes, pgno: int, salt: bytes) -> bytes:
    """按 SQLCipher 4 的规则加密一页（测试用，和 wechat_db 的解密互为逆运算）。"""
    off = w.SALT if pgno == 1 else 0
    iv = os.urandom(w.IV)
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    enc = e.update(plain[off:w.PAGE - w.RESERVE]) + e.finalize()
    head = salt if pgno == 1 else b""
    body = enc + iv
    mac = hmac.new(w._mac_key(key, salt), body + struct.pack("<I", pgno), hashlib.sha512).digest()
    page = head + body + mac
    return page + b"\0" * (w.PAGE - len(page))


def plain_db_with_reserve(path: Path, rows: list[str]) -> bytes:
    """做一个「每页末尾留 80 字节」的普通 SQLite 库（微信的库就是这样），返回文件内容。
    Python 的 sqlite3 设不了保留字节：先建一个只有文件头的空库，改文件头第 20 字节，SQLite 之后写数据会照着留。"""
    con = sqlite3.connect(path)
    con.execute("PRAGMA page_size=4096")
    con.execute("PRAGMA user_version=1")
    con.close()
    data = bytearray(path.read_bytes())
    data[20] = w.RESERVE
    data[105:107] = (w.PAGE - w.RESERVE).to_bytes(2, "big")  # 空的第 1 页：内容区从可用区末尾开始
    path.write_bytes(bytes(data))
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t(v) VALUES (?)", [(r,) for r in rows])
    con.commit()
    con.close()
    data = path.read_bytes()
    assert data[20] == w.RESERVE
    return data


@unittest.skipIf(Cipher is None, "没装 cryptography")
class PageTest(unittest.TestCase):
    def test_roundtrip_and_key_pick(self):
        key, other, salt = os.urandom(32), os.urandom(32), os.urandom(16)
        plain = w.SQLITE_HEADER + os.urandom(w.PAGE - 16 - w.RESERVE) + b"\0" * w.RESERVE
        page1 = encrypt_page(key, plain, 1, salt)
        self.assertEqual(page1[:16], salt)
        self.assertEqual(w.find_key(page1, [other, key]), key)
        self.assertIsNone(w.find_key(page1, [other]))
        dec = w._decryptor(key)(page1, 1)
        self.assertEqual(dec[:w.PAGE - w.RESERVE], plain[:w.PAGE - w.RESERVE])
        p2 = os.urandom(w.PAGE - w.RESERVE) + b"\0" * w.RESERVE
        self.assertEqual(w._decryptor(key)(encrypt_page(key, p2, 2, salt), 2)[:w.PAGE - w.RESERVE],
                         p2[:w.PAGE - w.RESERVE])

    def test_fallback_to_pycryptodome(self):
        # Windows 上实测过：cryptography 明明装着，在安装脚本里却加载失败。这时要自动改用 pycryptodome
        try:
            import Crypto.Cipher.AES  # noqa: F401
        except ImportError:
            self.skipTest("没装 pycryptodome")
        from unittest import mock
        key, salt = os.urandom(32), os.urandom(16)
        p2 = os.urandom(w.PAGE - w.RESERVE) + b"\0" * w.RESERVE
        page = encrypt_page(key, p2, 2, salt)
        with mock.patch.dict(sys.modules, {"cryptography.hazmat.primitives.ciphers": None}):
            dec = w._decryptor(key)(page, 2)
        self.assertEqual(dec[:w.PAGE - w.RESERVE], p2[:w.PAGE - w.RESERVE])

    def test_no_aes_backend_shows_real_reason(self):
        from unittest import mock
        with mock.patch.dict(sys.modules, {"cryptography.hazmat.primitives.ciphers": None, "Crypto.Cipher": None}):
            with self.assertRaises(w.WechatDBError) as cm:
                w._decryptor(os.urandom(32))
        self.assertIn("cryptography：ModuleNotFoundError", str(cm.exception))
        self.assertIn("pycryptodome：ModuleNotFoundError", str(cm.exception))

    def test_keys_from_file_any_shape(self):
        k1, k2 = os.urandom(32).hex(), os.urandom(32).hex()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "all_keys.json"
            # 键名是文件路径，里面也可能有 32/64 位十六进制，不能当成钥匙
            p.write_text(json.dumps({"message\\message_0.db": {"enc_key": k1, "salt": "ab" * 16},
                                     "a" * 64: k2, "dup": {"key": k1}}), encoding="utf-8")
            self.assertEqual(w.keys_from_file(p), [bytes.fromhex(k1), bytes.fromhex(k2)])
            p.write_text(f"随便什么格式 {k2}\n", encoding="utf-8")
            self.assertEqual(w.keys_from_file(p), [bytes.fromhex(k2)])

    def test_decrypt_whole_db(self):
        key, salt = os.urandom(32), os.urandom(16)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            plain = plain_db_with_reserve(d / "plain.db", ["你好", "第二条"])
            pages = [plain[i:i + w.PAGE] for i in range(0, len(plain), w.PAGE)]
            enc = b"".join(encrypt_page(key, pg, i + 1, salt) for i, pg in enumerate(pages))
            (d / "session").mkdir()
            (d / "session" / "session.db").write_bytes(enc)
            cache = w.Cache(d, [os.urandom(32), key], d / "cache")
            with cache.open("session/session.db") as db:
                self.assertEqual([r["v"] for r in db.query("SELECT v FROM t ORDER BY id")], ["你好", "第二条"])
            # 库没变：直接用缓存，不再解密
            mtime = (d / "cache" / "session" / "session.db").stat().st_mtime_ns
            cache.plain_path("session/session.db")
            self.assertEqual((d / "cache" / "session" / "session.db").stat().st_mtime_ns, mtime)
            # 钥匙不对：说清楚
            with self.assertRaises(w.WechatDBError):
                w.decrypt_to(d / "session" / "session.db", [os.urandom(32)], d / "x.db")


class WalTest(unittest.TestCase):
    def make_wal(self, frames, salts=(1, 2), bad_after=None):
        hdr = struct.pack(">6I", 0x377F0682, 3007000, w.PAGE, 0, *salts)
        s1, s2 = w._wal_checksum(hdr, 0, 0, False)
        out = hdr + struct.pack(">2I", s1, s2)
        for i, (pgno, commit, data) in enumerate(frames):
            fh = struct.pack(">4I", pgno, commit, *salts)
            s1, s2 = w._wal_checksum(fh[:8], s1, s2, False)
            s1, s2 = w._wal_checksum(data, s1, s2, False)
            cs = (s1, s2) if bad_after is None or i < bad_after else (0, 0)
            out += fh + struct.pack(">2I", *cs) + data
        return out

    def test_only_committed_frames(self):
        a, b, c = (bytes([x]) * w.PAGE for x in (1, 2, 3))
        pages, size = w.wal_pages(self.make_wal([(2, 0, a), (3, 5, b), (4, 0, c)]))
        self.assertEqual(sorted(pages), [2, 3])  # 第 3 帧之后没提交，不要
        self.assertEqual(size, 5)

    def test_later_frame_wins_and_bad_checksum_stops(self):
        a, b = bytes([1]) * w.PAGE, bytes([2]) * w.PAGE
        pages, _ = w.wal_pages(self.make_wal([(2, 3, a), (2, 3, b)]))
        self.assertEqual(pages[2], b)
        pages, _ = w.wal_pages(self.make_wal([(2, 3, a), (2, 3, b)], bad_after=1))
        self.assertEqual(pages[2], a)

    def test_empty_or_garbage(self):
        self.assertEqual(w.wal_pages(b""), ({}, None))
        self.assertEqual(w.wal_pages(os.urandom(100)), ({}, None))


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        import collect_wechat as cw
        self.cw = cw
        self.old = (cw.STATE_DIR, cw.DEFAULT_KEYS, dict(os.environ))
        cw.STATE_DIR = self.root / "state"
        cw.DEFAULT_KEYS = self.root / "nokeys.json"
        for k in ("WECHAT_KEYS", "WECHAT_DB_ROOT", "WECHAT_GROUPS"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.cw.STATE_DIR, self.cw.DEFAULT_KEYS, env = self.old
        os.environ.clear()
        os.environ.update(env)
        self.tmp.cleanup()

    def test_no_keys_quiet_but_check_fails(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.cw.main([]), 0)
            self.assertEqual(self.cw.main(["--check"]), 2)

    @unittest.skipIf(Cipher is None, "没装 cryptography")
    def test_pick_root_by_key(self):
        key = os.urandom(32)
        appdata = self.root / "appdata"
        cfg = appdata / "Tencent" / "xwechat" / "config"
        cfg.mkdir(parents=True)
        base = self.root / "我的微信"
        (cfg / "123.ini").write_text(str(base), encoding="utf-8")
        for acct, k in (("wxid_other_1a2b", os.urandom(32)), ("wxid_me_3c4d", key)):
            s = base / "xwechat_files" / acct / "db_storage" / "session"
            s.mkdir(parents=True)
            plain = w.SQLITE_HEADER + b"\0" * (w.PAGE - 16)
            (s / "session.db").write_bytes(encrypt_page(k, plain, 1, os.urandom(16)))
        os.environ["APPDATA"] = str(appdata)
        root, why = self.cw.pick_root([key])
        self.assertEqual(root.parent.name, "wxid_me_3c4d")
        root, why = self.cw.pick_root([os.urandom(32)])
        self.assertIsNone(root)
        self.assertIn("对不上", why)

    def test_zstd(self):
        try:
            import zstandard
            data = zstandard.ZstdCompressor().compress("中文消息".encode())
        except ImportError:
            try:
                from compression import zstd
                data = zstd.compress("中文消息".encode())
            except ImportError:
                self.skipTest("没有 zstd")
        self.assertEqual(self.cw.zstd_decompress(data).decode(), "中文消息")


class FakeWechat:
    """read_new 返回固定的几条消息；voice_audio 只认识 111、222 两条语音。"""

    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def display_name(self, u):
        return {"wxid_friend": "小王"}.get(u, u)

    def message_dbs_for(self, chat_id, cached):
        return ["message_0.db"]

    def read_new(self, chat_id, dbname, after_id, start_ts, is_group):
        return self.rows

    def voice_audio(self, ids):
        self.asked.append(list(ids))
        return {i: b"\x02#!SILK_V3" + i.encode() for i in ids if i in ("111", "222")}

    def copy_file(self, *a):
        return None

    def save_image(self, chat_id, svr_id, ts, raw_dir, day, self_id):
        return f"spaces/me/raw/wechat/images/{day}/x-{svr_id}.png" if svr_id == "555" else None


class VoiceTest(unittest.TestCase):
    """语音转文字：一个会话的语音一次取、一次转；转不了就只留「[语音]」。"""

    def setUp(self):
        import collect_wechat
        self.cw = collect_wechat
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / "raw"
        self.ts = 1794802892
        self.rows = [
            (1, self.ts, "wxid_friend", "在吗", None, "", ""),
            (2, self.ts + 60, "wxid_friend", "[语音]", None, "111", ""),
            (3, self.ts + 120, "wxid_me", "[语音]", None, "222", ""),
            (4, self.ts + 180, "wxid_friend", "[语音]", None, "333", ""),  # 库里找不到音频
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, transcribe):
        wx = FakeWechat(self.rows)
        n = self.cw.collect_chat(wx, {"username": "wxid_friend", "last_timestamp": self.ts + 200},
                                 {"chats": {}}, self.raw, 3, "wxid_me", self.ts + 300, 25, transcribe)
        text = "".join(p.read_text(encoding="utf-8") for p in sorted(self.raw.rglob("*.md")))
        return n, text, wx

    def test_voice_turned_into_text(self):
        seen = []

        def fake(audio):
            seen.append(sorted(audio))
            return {"111": {"text": "明天下午三点开会", "sec": 4.2}, "222": {"text": "", "sec": 1.0}}
        n, text, wx = self.collect(fake)
        self.assertEqual(n, 4)
        self.assertEqual(wx.asked, [["111", "222", "333"]])
        self.assertEqual(seen, [["111", "222"]])
        self.assertIn("小王：在吗", text)
        self.assertIn("小王：[语音 4秒] 明天下午三点开会", text)
        self.assertIn("我：[语音 1秒]（没听出内容）", text)
        self.assertIn("小王：[语音]\n", text)

    def test_transcribe_failure_or_off_keeps_placeholder(self):
        def broken(audio):
            raise RuntimeError("模型坏了")
        for t in (broken, None):
            self.tmp.cleanup()
            self.tmp = tempfile.TemporaryDirectory()
            self.raw = Path(self.tmp.name) / "raw"
            n, text, wx = self.collect(t)
            self.assertEqual(n, 4)
            self.assertEqual(text.count("：[语音]\n"), 3)
        self.assertEqual(wx.asked, [])  # 没开转写，不去取音频

    def test_transcribe_voices_runs_script(self):
        """transcribe_voices 用专用 Python 跑 transcribe_voice.py：用一个假的「Python」代替，看结果怎么解析。"""
        from unittest import mock
        d = Path(self.tmp.name)
        (d / "model" ).mkdir()
        (d / "model" / "model.bin").write_bytes(b"x")
        fake = d / "python"
        fake.write_text(
            "#!/bin/sh\n# 参数：-X utf8 transcribe_voice.py <文件夹>\n"
            "for f in \"$4\"/*.silk; do n=$(basename \"$f\");\n"
            "  case \"$n\" in bad*) echo '{\"name\": \"'$n'\", \"ok\": false, \"error\": \"坏文件\"}';;\n"
            "  *) echo '{\"name\": \"'$n'\", \"ok\": true, \"text\": \"你好\", \"sec\": 2.0}';; esac; done\n"
            "echo '模型加载的提示行，不是 JSON'\n")
        fake.chmod(0o755)
        if os.name == "nt":
            self.skipTest("假 Python 是 shell 脚本，只在 Mac/Linux 上跑")
        with mock.patch.object(self.cw, "python", lambda: str(fake)), \
                mock.patch.dict(os.environ, {"AGENTOS_WHISPER_MODEL": str(d / "model")}):
            self.assertEqual(self.cw.transcribe_voices({"111": b"x", "bad2": b"y"}),
                             {"111": {"text": "你好", "sec": 2.0}})
        with mock.patch.dict(os.environ, {"AGENTOS_WHISPER_MODEL": str(d / "没有模型")}):
            self.assertEqual(self.cw.transcribe_voices({"111": b"x"}), {})  # 没模型：不跑，直接空

    def test_voice_line(self):
        self.assertEqual(self.cw.voice_line(None), "[语音]")
        self.assertEqual(self.cw.voice_line({"text": "好的", "sec": 13.8}), "[语音 14秒] 好的")
        self.assertEqual(self.cw.voice_line({"text": "好的", "sec": None}), "[语音] 好的")


class ImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / "raw"
        self.ts = 1794802892
        import collect_wechat
        self.cw = collect_wechat

    def tearDown(self):
        self.tmp.cleanup()

    def test_image_line_points_to_saved_file(self):
        rows = [(1, self.ts, "wxid_friend", "[图片]", None, "", "555"),
                (2, self.ts + 60, "wxid_friend", "[图片]", None, "", "666")]  # 本机没有这张
        self.cw.collect_chat(FakeWechat(rows), {"username": "wxid_friend", "last_timestamp": self.ts + 100},
                        {"chats": {}}, self.raw, 3, "wxid_me", self.ts + 300, 25, None)
        text = "".join(p.read_text(encoding="utf-8") for p in self.raw.rglob("*.md"))
        self.assertIn("小王：[图片] 可看：spaces/me/raw/wechat/images/2026-11-16/x-555.png", text)
        self.assertIn("小王：[图片]\n", text)

    def test_images_off(self):
        rows = [(1, self.ts, "wxid_friend", "[图片]", None, "", "555")]
        self.cw.collect_chat(FakeWechat(rows), {"username": "wxid_friend", "last_timestamp": self.ts + 100},
                        {"chats": {}}, self.raw, 3, "wxid_me", self.ts + 300, 25, None, images=False)
        text = "".join(p.read_text(encoding="utf-8") for p in self.raw.rglob("*.md"))
        self.assertNotIn("可看", text)

    def test_decode_v2_v1_and_plain(self):
        """按同样的格式造一张加密图（V2：AES 段 + 原样段 + 异或段），能解回原图。"""
        import hashlib, struct
        import wechat_images as wi
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(60)) + b"IEND\xaeB`\x82"
        acct = Path(self.tmp.name) / "xwechat_files" / "wxid_me_ab12"
        (acct.parent.parent / "app_data" / "net" / "kvcomm").mkdir(parents=True)
        (acct.parent.parent / "app_data" / "net" / "kvcomm" / "key_12345_1.statistic").write_text("")
        acct.mkdir(parents=True)
        keys = wi.candidate_keys(acct, "wxid_me")
        key, xor = hashlib.md5(b"12345wxid_me").hexdigest()[:16].encode(), 12345 & 255
        self.assertIn((key, xor), keys)

        def enc(k, plain):
            pad = 16 - len(plain) % 16
            plain += bytes([pad]) * pad
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                e = Cipher(algorithms.AES(k), modes.ECB()).encryptor()
                return e.update(plain) + e.finalize()
            except ImportError:
                import subprocess
                return subprocess.run(["/usr/bin/openssl", "enc", "-aes-128-ecb", "-nopad", "-K", k.hex()],
                                      input=plain, capture_output=True).stdout

        def build(marker, k, x_byte, a=20, x=6):
            mid = png[a:len(png) - x]
            return (marker + struct.pack("<II", a, x) + b"\x01" + enc(k, png[:a]) + mid
                    + bytes(c ^ x_byte for c in png[-x:]))
        self.assertEqual(wi.decode_dat(build(wi.V2, key, xor), keys), (png, ".png"))
        self.assertEqual(wi.decode_dat(build(wi.V1, wi.V1_KEY, 0x37), []), (png, ".png"))
        self.assertEqual(wi.decode_dat(bytes(c ^ 0x5A for c in png), []), (png, ".png"))  # 老格式：整体异或
        self.assertIsNone(wi.decode_dat(build(wi.V2, b"0" * 16, 0), keys))  # 钥匙不对：解不开


if __name__ == "__main__":
    unittest.main()
