"""微信图片：把本机微信存的加密图片解成普通图片（只读，不改微信的任何文件）。

图片存在账号目录下 msg/attach/<会话id的md5>/<年-月>/Img/<名字>.dat（_t.dat 是缩略图），
文件名要从 message/message_resource.db 按消息的 server_id 查出来。
解密方法来自开源 macos-wechat-export 的 media_pipeline.py（MIT 协议）：
  - V2 格式（开头 07 08 56 32 08 07）：前一段 AES-128-ECB，钥匙 = md5(数字 + 自己的 wxid) 的前 16 位，
    「数字」取自微信 app_data/net(或 ilink)/kvcomm/key_<数字>_*.statistic；末尾一段按「数字 & 255」异或
  - V1 格式：固定钥匙 cfcd208495d565ef
  - 更老的格式：整个文件按一个字节异或
解出来认得的格式（JPG、PNG、GIF、WEBP）才算成功；认不出就不存，聊天记录里照旧写「[图片]」。
只用标准库；AES 优先用 cryptography 包，没有就用系统自带的 openssl 命令（Mac 自带）。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
from datetime import datetime
from pathlib import Path

MAGIC = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"), (b"RIFF", ".webp"))
V1, V2 = b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07"
V1_KEY = b"cfcd208495d565ef"


def image_ext(data: bytes) -> str | None:
    for magic, ext in MAGIC:
        if data.startswith(magic):
            return ext
    return None


def kvcomm_codes(app_data_dirs: list[Path]) -> set[int]:
    """kvcomm/key_<数字>_*.statistic 里的数字。先看常见位置；都没有再在这些文件夹里整个搜一遍（Windows 上位置不固定）。"""
    def scan(paths):
        out = set()
        for p in paths:
            m = re.fullmatch(r"key_(\d+)_.*\.statistic", p.name)
            if m:
                out.add(int(m[1]))
        return out
    codes = scan(p for base in app_data_dirs for sub in ("net/kvcomm", "ilink/kvcomm")
                 for p in (base / sub).glob("key_*_*.statistic"))
    if not codes:
        codes = scan(p for base in app_data_dirs for p in base.rglob("key_*_*.statistic") if p.parent.name == "kvcomm")
    return codes


def app_data_dirs(account_dir: Path) -> list[Path]:
    """微信的 app_data 在哪：WECHAT_APP_DATA 指定的；Mac 在 xwechat_files 旁边；Windows 再试 %APPDATA%\\Tencent\\xwechat。"""
    out = []
    if os.environ.get("WECHAT_APP_DATA"):
        out.append(Path(os.environ["WECHAT_APP_DATA"]).expanduser())
    out.append(account_dir.parent.parent / "app_data")       # …/Documents/xwechat_files/<账号> → …/Documents/app_data
    if os.environ.get("APPDATA"):
        out.append(Path(os.environ["APPDATA"]) / "Tencent" / "xwechat")
    return [p for p in out if p.is_dir()]


def candidate_keys(account_dir: Path, self_id: str) -> list[tuple[bytes, int]]:
    own = [x for x in dict.fromkeys([self_id, account_dir.name]) if x]
    return [(hashlib.md5((str(c) + o).encode()).hexdigest()[:16].encode(), c & 255)
            for c in sorted(kvcomm_codes(app_data_dirs(account_dir))) for o in own]


def _aes_ecb(key: bytes, data: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return d.update(data) + d.finalize()
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES  # pycryptodome
        return AES.new(key, AES.MODE_ECB).decrypt(data)
    except ImportError:
        pass
    ssl = shutil.which("openssl") or "/usr/bin/openssl"
    r = subprocess.run([ssl, "enc", "-aes-128-ecb", "-d", "-nopad", "-K", key.hex()],
                       input=data, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise ValueError("openssl 解密失败")
    return r.stdout


def _split(data: bytes) -> tuple[int, int] | None:
    """V1/V2 头 15 字节：6 字节标记 + AES 段长度 + 异或段长度 + 1 字节。返回 (AES 段补齐后的长度, 异或段长度)。"""
    if len(data) < 31:
        return None
    a, x = struct.unpack("<II", data[6:14])
    n = a + (16 - a % 16) if a else 0
    return (n, x) if 15 + n + x <= len(data) else None


def _aes_part(data: bytes, n: int, key: bytes) -> bytes | None:
    # 先只解第一块看是不是图片，不是就换下一把钥匙（省得每把钥匙都解整张图）
    if not image_ext(_aes_ecb(key, data[15:31])):
        return None
    v = _aes_ecb(key, data[15:15 + n])
    pad = v[-1] if v else 0
    return v[:-pad] if 1 <= pad <= 16 and v.endswith(bytes([pad]) * pad) else v


def _join(data: bytes, v: bytes, n: int, x: int, xor: int) -> bytes:
    tail = data[15 + n:]
    return v + (tail[:-x] if x else tail) + (bytes(c ^ xor for c in tail[-x:]) if x else b"")


def decode_dat(data: bytes, keys: list[tuple[bytes, int]]) -> tuple[bytes, str] | None:
    """解一个 .dat，返回 (图片内容, 扩展名)；解不开返回 None。"""
    if not data:
        return None
    ext = image_ext(data)
    if ext:
        return data, ext
    if data.startswith((V1, V2)):
        nx = _split(data)
        if not nx:
            return None
        n, x = nx
        if data.startswith(V1):  # 固定钥匙，异或字节要试：按图片结尾标记认对的那个
            v = _aes_part(data, n, V1_KEY)
            if v is None:
                return None
            outs = [_join(data, v, n, x, xor) for xor in (range(256) if x else [0])]
            good = [o for o in outs if _xor_tail_ok(o)] or outs[:1]
            return good[0], image_ext(good[0])
        for key, xor in keys:
            try:
                v = _aes_part(data, n, key)
            except ValueError:
                continue
            if v is not None:
                out = _join(data, v, n, x, xor)
                return out, image_ext(out)
        return None
    for xor in {data[0] ^ 0xFF, data[0] ^ 0x89, data[0] ^ 0x47, data[0] ^ 0x52}:
        out = bytes(c ^ xor for c in data)
        if image_ext(out):
            return out, image_ext(out)
    return None


def _xor_tail_ok(img: bytes) -> bool:
    """V1 的异或字节要试 256 个：JPG 以 FFD9 结尾、PNG 以 IEND 块结尾，对上才算这个异或对。"""
    if img.startswith(b"\xff\xd8"):
        return img.rstrip(b"\x00").endswith(b"\xff\xd9")
    if img.startswith(b"\x89PNG"):
        return b"IEND" in img[-16:]
    return True


def image_files(account_dir: Path, chat_id: str, ts: int, hashes: set[str]) -> list[Path]:
    """按资源库查出的名字找图片文件：原图在前、缩略图在后。"""
    month = datetime.fromtimestamp(ts).strftime("%Y-%m")
    folder = account_dir / "msg" / "attach" / hashlib.md5(chat_id.encode()).hexdigest() / month / "Img"
    found = [p for h in hashes for p in folder.glob(h + "*.dat") if p.is_file()]
    return sorted(found, key=lambda p: (p.name.endswith("_t.dat"), -p.stat().st_size))


def resource_hashes(packed_hex_values: list[str]) -> set[str]:
    hashes = set()
    for v in packed_hex_values:
        if v:
            hashes.update(x.decode() for x in re.findall(rb"[a-fA-F0-9]{32}", bytes.fromhex(v)))
    return hashes
