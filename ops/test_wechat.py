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


if __name__ == "__main__":
    unittest.main()
