"""只读打开微信 4.x 的加密聊天库（SQLCipher 4），给 collect_wechat.py 用。

微信 4.x（Windows、Mac 一样）的每个 .db 都是 SQLCipher 4 加密的：每页 4096 字节，
页尾 80 字节是 IV（16）+ HMAC-SHA512（64），第 1 页开头 16 字节是盐。钥匙是每个库一把 32 字节的原始密钥，
由取钥匙工具（如 WorkBuddy 的 wechat-cli 技能）事先写进一个 JSON 文件（all_keys.json / keys.json）。

Windows 上没有现成的 SQLCipher 库，这里用纯 Python 解密：
  1. 读 .db 和旁边的 -wal（微信新写的消息常常还在 -wal 里，只读 .db 会漏）；
  2. 用钥匙逐页解密，把 -wal 里已提交的页盖上去，写成一份普通 SQLite 文件放进缓存目录；
  3. 用 Python 自带的 sqlite3 以只读方式查询。
原库只读不写；库和 -wal 没变就直接用上次的缓存。缓存在 %USERPROFILE%\\.agentos\\wechat-cache，
那个文件夹 Agent 的权限规则读不到。

钥匙文件不要求格式：里面所有 64 位十六进制的钥匙都拿来试，用第 1 页的 HMAC 校验，对得上的就是这个库的钥匙。
需要 cryptography 包（安装脚本会装）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import struct
import tempfile
import time
from pathlib import Path

PAGE = 4096
SALT = 16
IV = 16
HMAC_LEN = 64
RESERVE = 80  # IV + HMAC，向上取整到 16 的倍数正好 80
SQLITE_HEADER = b"SQLite format 3\x00"
HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")


class WechatDBError(RuntimeError):
    pass


# ---------- 钥匙 ----------

def keys_from_file(path: Path) -> list[bytes]:
    """钥匙文件里所有像钥匙的 64 位十六进制串（去重、保持顺序）。不管文件是哪种工具写的什么结构。"""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as e:
        raise WechatDBError(f"读不了钥匙文件 {path}：{e}") from e
    try:  # 是 JSON 的话把所有字符串值摊平，免得键名（文件路径里的哈希）被当成钥匙
        found = []
        def walk(v):
            if isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, str):
                found.extend(HEX64.findall(v))
        walk(json.loads(text))
    except ValueError:
        found = HEX64.findall(text)
    out, seen = [], set()
    for h in found:
        k = bytes.fromhex(h)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _mac_key(key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, 32)


def _page_hmac_ok(mac_key: bytes, page: bytes, pgno: int) -> bool:
    off = SALT if pgno == 1 else 0
    body = page[off:PAGE - RESERVE + IV]
    want = page[PAGE - RESERVE + IV:PAGE - RESERVE + IV + HMAC_LEN]
    got = hmac.new(mac_key, body + struct.pack("<I", pgno), hashlib.sha512).digest()
    return hmac.compare_digest(got, want)


def find_key(first_page: bytes, keys: list[bytes]) -> bytes | None:
    """用第 1 页的 HMAC 找出这个库的钥匙。"""
    if len(first_page) < PAGE:
        return None
    salt = first_page[:SALT]
    for k in keys:
        if len(k) == 32 and _page_hmac_ok(_mac_key(k, salt), first_page, 1):
            return k
    return None


# ---------- 解密 ----------

def _decryptor(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:
        raise WechatDBError("缺少 cryptography 包：重新运行一次安装脚本 ops\\windows\\install.ps1") from e

    def dec(page: bytes, pgno: int) -> bytes:
        off = SALT if pgno == 1 else 0
        iv = page[PAGE - RESERVE:PAGE - RESERVE + IV]
        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = d.update(page[off:PAGE - RESERVE]) + d.finalize()
        return (SQLITE_HEADER if pgno == 1 else b"") + plain + page[PAGE - RESERVE:]
    return dec


def _wal_checksum(data: bytes, s1: int, s2: int, big: bool) -> tuple[int, int]:
    """SQLite WAL 的累加校验（和 sqlite3 源码 walChecksumBytes 一样）。"""
    words = struct.unpack((">" if big else "<") + f"{len(data) // 4}I", data)
    for i in range(0, len(words), 2):
        s1 = (s1 + words[i] + s2) & 0xFFFFFFFF
        s2 = (s2 + words[i + 1] + s1) & 0xFFFFFFFF
    return s1, s2


def wal_pages(wal: bytes) -> tuple[dict[int, bytes], int | None]:
    """读 -wal：返回 {页号: 加密的页}（只算已提交的事务）和提交后库的总页数。
    和 SQLite 读 WAL 的规则一样：盐对不上、校验对不上就停，最后一次提交之后的帧不要。"""
    if len(wal) < 32:
        return {}, None
    magic, _ver, psize, _ckpt, salt1, salt2, c1, c2 = struct.unpack(">8I", wal[:32])
    if magic not in (0x377F0682, 0x377F0683) or psize != PAGE:
        return {}, None
    big = magic == 0x377F0683
    s1, s2 = _wal_checksum(wal[:24], 0, 0, big)
    if (s1, s2) != (c1, c2):
        return {}, None
    pages: dict[int, bytes] = {}
    pending: dict[int, bytes] = {}
    db_size = None
    pos = 32
    frame = 24 + PAGE
    while pos + frame <= len(wal):
        pgno, commit, fs1, fs2, fc1, fc2 = struct.unpack(">6I", wal[pos:pos + 24])
        if (fs1, fs2) != (salt1, salt2):
            break
        s1, s2 = _wal_checksum(wal[pos:pos + 8], s1, s2, big)
        s1, s2 = _wal_checksum(wal[pos + 24:pos + frame], s1, s2, big)
        if (s1, s2) != (fc1, fc2):
            break
        pending[pgno] = wal[pos + 24:pos + frame]
        if commit:
            pages.update(pending)
            pending = {}
            db_size = commit
        pos += frame
    return pages, db_size


def _read_all(path: Path) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


def decrypt_to(db: Path, keys: list[bytes], out: Path) -> None:
    """把加密的 db（连同 -wal 里已提交的内容）解密成一份普通 SQLite 文件 out。"""
    raw = _read_all(db)
    if len(raw) < PAGE:
        raise WechatDBError(f"{db.name} 太小或读不到")
    key = find_key(raw[:PAGE], keys)
    if key is None:
        raise WechatDBError(f"{db.name} 没有对得上的钥匙（钥匙过期？换了账号？重新取一次钥匙）")
    wal, size = wal_pages(_read_all(Path(str(db) + "-wal")))
    n = len(raw) // PAGE
    if size:
        n = size
    dec = _decryptor(key)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".tmp-", suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            for pgno in range(1, n + 1):
                page = wal.get(pgno)
                if page is None:
                    start = (pgno - 1) * PAGE
                    page = raw[start:start + PAGE]
                if len(page) < PAGE:
                    break
                f.write(dec(page, pgno))
        for i in range(5):  # 上一份缓存正被别的进程（比如复盘前的补采）打开着时，Windows 换不了，等一下再试
            try:
                os.replace(tmp, out)
                break
            except PermissionError:
                if i == 4:
                    raise
                time.sleep(1)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- 只读打开（带缓存） ----------

def _sig(db: Path) -> list:
    """判断库有没有变：库的大小和修改时间，加上 -wal 内容的哈希。
    微信一直开着这些文件，Windows 上修改时间不一定及时更新；-wal 重置后大小也可能不变，所以 -wal 看内容。"""
    try:
        st = db.stat()
        out = [st.st_size, st.st_mtime_ns]
    except OSError:
        out = [None, None]
    out.append(hashlib.sha1(_read_all(Path(str(db) + "-wal"))).hexdigest())
    return out


class Cache:
    """解密后的副本放在 cache_dir，按「相对 db_storage 的路径」存。库和 -wal 的大小、修改时间没变就复用。"""

    def __init__(self, root: Path, keys: list[bytes], cache_dir: Path):
        self.root = Path(root)
        self.keys = keys
        self.dir = Path(cache_dir)
        self._sigs_path = self.dir / "sigs.json"
        try:
            self._sigs = json.loads(self._sigs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._sigs = {}

    def plain_path(self, rel: str) -> Path:
        src = self.root / rel
        out = self.dir / rel.replace("\\", "/")
        sig = _sig(src)
        if self._sigs.get(rel) != sig or not out.exists():
            decrypt_to(src, self.keys, out)
            self._sigs[rel] = sig
            self.dir.mkdir(parents=True, exist_ok=True)
            self._sigs_path.write_text(json.dumps(self._sigs), encoding="utf-8")
        return out

    def open(self, rel: str) -> "DB":
        return DB(self.plain_path(rel))


class DB:
    """和 Mac 版 sqlcipher_probe.DB 一样的用法：with DB(...) as db: db.query(sql) -> [dict]。只读。"""

    def __init__(self, path: Path):
        uri = "file:" + Path(path).resolve().as_posix() + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.text_factory = lambda b: b.decode("utf-8", "replace")
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError as e:
            self.conn.close()
            raise WechatDBError(f"解密后的库打不开（可能微信正在大量写入，下一轮再试）：{e}") from e

    def query(self, sql: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
