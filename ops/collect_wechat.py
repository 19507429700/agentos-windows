"""微信聊天采集（本机、只读）：把本机微信 4.x 聊天库里的聊天，增量存成 Markdown 原文。Windows 版。

和 Mac 版一样的上层逻辑（读会话、认人名、读消息、过滤群、存成 Markdown、复制聊天里的文件）；
只换了最底下「打开加密库」那一层：Mac 用 SQLCipher 库，Windows 用 ops/wechat_db.py 纯 Python 解密。
本脚本只读不改、不发消息、不取钥匙。钥匙由你本人事先用取钥匙工具取好（教程第十三步）。

会话列表来自 session.db，人名/群名来自 contact.db，消息在 message_<数字>.db 的 Msg_<md5(会话id)> 表。
只导出文本消息；图片、视频等只留占位（如「[图片]」）；公众号文章留标题和链接，「文件」消息把本机文件复制进知识库。
语音：下好了语音模型（安装脚本会下）时，在本机转成文字，写成「[语音 14秒] 转出的文字」；没有模型就只留「[语音]」。

配置（%USERPROFILE%\\.agentos\\wechat.env，每行 名字=值；都可以不填）：
  WECHAT_KEYS      钥匙文件，默认 %USERPROFILE%\\.wechat-cli\\all_keys.json
  WECHAT_DB_ROOT   某账号的 db_storage 文件夹；不填就自动找（找钥匙对得上的那个账号）
  WECHAT_SELF_ID   你自己的 wxid，默认从账号文件夹名推断
  WECHAT_GROUPS    采哪些群（逗号/顿号分隔）：填群名关键词或群 id；私聊一律全采，群只采命中的。留空只采私聊
  WECHAT_INITIAL_DAYS  每个会话第一次采集往回看多少天，默认 3
  WECHAT_FILES_MAX_MB  「文件」消息里的真实文件复制进知识库的大小上限，默认 25；填 0 关闭
  WECHAT_VOICE     语音转文字，默认开（有模型才生效）；填 0 关闭

输出：spaces\\me\\raw\\wechat\\<日期>\\<会话名>-<会话id后6位>.md，每条一行「- 时:分 发送人：内容」，只追加。
状态：%USERPROFILE%\\.agentos\\wechat_state.json；解密缓存：%USERPROFILE%\\.agentos\\wechat-cache\\

用法：
  python ops/collect_wechat.py           采集（定时任务每 10 分钟跑一次）
  python ops/collect_wechat.py --check   检查：找到哪个账号、钥匙对不对、能读出几个会话
  python ops/collect_wechat.py --groups  列出最近的群名，方便填 WECHAT_GROUPS

底线：只读不发；原文只追加不修改；钥匙文件不存在时安静退出 0；读取失败退出码非 0（供告警）。
"""

from __future__ import annotations

import configparser
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import NO_WINDOW, RAW, STATE, child_env, python, read_env  # noqa: E402
import wechat_db  # noqa: E402

log = logging.getLogger("collect_wechat")

STATE_DIR = STATE
DEFAULT_KEYS = Path.home() / ".wechat-cli" / "all_keys.json"
MSG_DB_RE = re.compile(r"message_\d+\.db")  # 只要聊天记录库，不要 message_fts.db、message_resource.db
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# 非文本消息的占位（按 local_type 低 32 位）。只给人看个大概，不解析媒体。
TYPE_LABEL = {
    3: "[图片]", 34: "[语音]", 42: "[名片]", 43: "[视频]", 47: "[表情]",
    48: "[位置]", 49: "[链接/文件]", 50: "[通话]", 51: "[视频号]",
    10000: "[系统消息]", 10002: "[系统消息]",
}
# local_type=49 的「应用消息」再按内部 appmsg <type> 细分
APPMSG_LABEL = {5: "公众号", 6: "文件", 8: "表情", 19: "聊天记录",
                33: "小程序", 36: "小程序", 51: "视频号", 57: "引用", 2000: "转账"}


def _xml_pick(xml: str, tag: str) -> str | None:
    """从消息 XML 里取一个标签的文本，去掉 CDATA 包裹。"""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    if not m:
        return None
    v = m.group(1).strip()
    c = re.match(r"<!\[CDATA\[(.*?)\]\]>\s*$", v, re.S)
    return ((c.group(1) if c else v).strip()) or None


def parse_appmsg(xml: str) -> tuple[str, str | None]:
    """把 local_type=49 的应用消息 XML 变成（可读占位, 文件名或None）。文件名只有「文件」类才有。"""
    try:
        at = int(_xml_pick(xml, "type") or 0)
    except ValueError:
        at = 0
    title = _xml_pick(xml, "title")
    src = _xml_pick(xml, "sourcedisplayname") or _xml_pick(xml, "appname")
    if at == 5:  # 公众号文章 / 链接：标题 + 来源 + 链接（链接放续行，便于以后读正文）
        head = f"[公众号] {title or '链接'}" + (f" — {src}" if src else "")
        url = _xml_pick(xml, "url")
        return head + (f"\n{url}" if url else ""), None
    if at == 6:  # 文件：带上文件名，正文另行把本机真实文件复制出来供 Agent 读
        return f"[文件] {title or '未命名文件'}", (title or None)
    if at in (33, 36):
        return f"[小程序] {title or ''}".rstrip() + (f" — {src}" if src else ""), None
    if at in (19, 57):
        return f"[{APPMSG_LABEL[at]}] {title or ''}".strip(), None
    if at == 2000:
        return "[转账]", None
    if title and "当前版本不支持" not in title:
        return f"[{APPMSG_LABEL.get(at, '卡片')}] {title}", None
    return "[链接/卡片]", None


# 不采的系统/占位会话
SKIP_USERS = {"brandsessionholder", "@placeholder_foldgroup", "notifymessage",
              "weixin", "fmessage", "medianote", "floatbottle", "newsapp"}


def load_env(path: Path) -> None:
    for k, v in read_env(path).items():
        os.environ.setdefault(k, v)


# ---------- 状态 ----------

def state_path() -> Path:
    return STATE_DIR / "wechat_state.json"


def load_state() -> dict:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("chats", {})
    return st


def save_state(st: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# ---------- 读取封装 ----------

def zstd_decompress(data: bytes) -> bytes:
    """微信 4.x 有的消息正文是 zstd 压缩的。Python 3.14 自带 compression.zstd，更早的用 zstandard 包。"""
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(data)
    except ImportError:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=64 * 1024 * 1024)


def candidate_roots() -> list[Path]:
    """本机所有微信账号的 db_storage。微信 4.x 把数据根目录写在 %APPDATA%\\Tencent\\xwechat\\config\\*.ini 里，
    没有就看默认的「文档\\xwechat_files」。"""
    bases: list[Path] = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        for ini in sorted((Path(appdata) / "Tencent" / "xwechat" / "config").glob("*.ini")):
            try:
                data = ini.read_bytes()
            except OSError:
                continue
            try:
                text = data.decode("utf-8-sig").strip()
            except UnicodeDecodeError:
                text = data.decode("gbk", "replace").strip()  # 路径里有中文时，老版本可能存成 GBK
            for line in [text] + text.splitlines():  # 有的版本整个文件就是一个路径，有的是 键=路径
                v = line.split("=", 1)[-1].strip().strip('"')
                if v and (Path(v) / "xwechat_files").is_dir():
                    bases.append(Path(v) / "xwechat_files")
    bases += [Path.home() / "Documents" / "xwechat_files", Path.home() / "文档" / "xwechat_files"]
    out, seen = [], set()
    for b in bases:
        for d in sorted(b.glob("*/db_storage")) if b.is_dir() else []:
            if (d / "session" / "session.db").is_file() and d.resolve() not in seen:
                seen.add(d.resolve())
                out.append(d)
    return out


def pick_root(keys: list[bytes]) -> tuple[Path | None, str]:
    """自动找账号：钥匙对得上 session.db 的那个。返回 (db_storage, 说明)。"""
    roots = candidate_roots()
    if not roots:
        return None, "没找到微信的数据文件夹（xwechat_files）。微信是 4.0 以上吗？在 wechat.env 里用 WECHAT_DB_ROOT 指定"
    matched = []
    for r in roots:
        try:
            with open(r / "session" / "session.db", "rb") as f:
                first = f.read(wechat_db.PAGE)
        except OSError:
            continue
        if wechat_db.find_key(first, keys):
            matched.append(r)
    if len(matched) == 1:
        return matched[0], f"自动找到账号：{matched[0]}"
    if not matched:
        return None, ("找到 " + "、".join(str(r) for r in roots) + "，但钥匙都对不上：钥匙是别的账号的，"
                      "或者微信退出登录、更新过，要重新取钥匙")
    return None, "钥匙对得上好几个账号，在 wechat.env 里用 WECHAT_DB_ROOT 指定一个：" + "、".join(map(str, matched))


def _sql_text(s: str) -> str:
    """把任意字符串安全地变成 SQL 文本字面量（避开引号/非 ASCII 问题）。"""
    return "CAST(x'" + s.encode().hex() + "' AS TEXT)"


class Wechat:
    def __init__(self, root: Path, keys: list[bytes], cache_dir: Path):
        self.root = root
        self.cache = wechat_db.Cache(root, keys, cache_dir)
        self._name_cache: dict[str, str] = {}

    def open(self, rel: str):
        return self.cache.open(rel)

    def sessions(self) -> list[dict]:
        with self.open("session/session.db") as db:
            return db.query("SELECT username, type, last_timestamp, summary "
                            "FROM SessionTable ORDER BY sort_timestamp DESC")

    def display_name(self, username: str) -> str:
        """username（wxid 或 群id）→ 备注/昵称；查不到就用 id。结果缓存。"""
        if username in self._name_cache:
            return self._name_cache[username]
        name = username
        try:
            with self.open("contact/contact.db") as db:
                rows = db.query("SELECT remark, nick_name FROM contact "
                                f"WHERE username={_sql_text(username)} LIMIT 1")
            if rows:
                name = (rows[0].get("remark") or rows[0].get("nick_name") or username).strip() or username
        except Exception:
            pass
        self._name_cache[username] = name
        return name

    def message_dbs_for(self, chat_id: str, cached: list[str] | None) -> list[str]:
        """返回含有该会话消息表的 message_*.db 文件名列表。cached 命中就不重新扫。"""
        table = "Msg_" + hashlib.md5(chat_id.encode()).hexdigest()
        if cached:
            return cached
        found = []
        for p in sorted((self.root / "message").glob("message_*.db")):
            if not MSG_DB_RE.fullmatch(p.name):
                continue
            try:
                with self.open("message/" + p.name) as db:
                    if db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                                f"name={_sql_text(table)}"):
                        found.append(p.name)
            except Exception as e:
                log.warning("打开 %s 失败：%s", p.name, e)
        return found

    def read_new(self, chat_id: str, dbname: str, after_id: int, start_ts: int, is_group: bool):
        """读某个 message_db 里该会话 local_id>after_id（或首次按时间）的消息，产出 (ts, sender_wxid, text)。"""
        table = "Msg_" + hashlib.md5(chat_id.encode()).hexdigest()
        if after_id:
            clause = f"local_id > {int(after_id)}"
        else:
            clause = f"create_time >= {int(start_ts)}"
        out = []
        with self.open("message/" + dbname) as db:
            rows = db.query("SELECT local_id, server_id, create_time, local_type, real_sender_id, "
                            "hex(message_content) AS body FROM \"" + table + "\" "
                            f"WHERE {clause} ORDER BY local_id ASC")
            for r in rows:
                lid = int(r["local_id"])
                ts = int(r["create_time"])
                ltype = int(r["local_type"]) & 0xFFFFFFFF
                sender = db.query("SELECT user_name FROM Name2Id WHERE rowid="
                                  + str(int(r["real_sender_id"])))
                wxid = sender[0]["user_name"] if len(sender) == 1 else ""
                text, attach = self._decode(r["body"], ltype, chat_id, wxid, is_group)
                voice = str(r.get("server_id") or "") if ltype == 34 else ""  # 语音：凭 server_id 去 media 库取音频
                out.append((lid, ts, wxid, text, attach, voice))
        return out

    def voice_audio(self, svr_ids: list[str]) -> dict[str, bytes]:
        """语音消息的音频（SILK）存在 message/media_*.db 的 VoiceInfo 表，按消息的 server_id 对上。"""
        want = [s for s in svr_ids if s.isdigit() and s != "0"]
        out: dict[str, bytes] = {}
        if not want:
            return out
        for p in sorted((self.root / "message").glob("media_*.db")):
            if not re.fullmatch(r"media_\d+\.db", p.name):
                continue
            try:
                with self.open("message/" + p.name) as db:
                    for r in db.query("SELECT svr_id, hex(voice_data) AS b FROM VoiceInfo "
                                      f"WHERE svr_id IN ({','.join(want)})"):
                        if r.get("b"):
                            out.setdefault(str(r["svr_id"]), bytes.fromhex(r["b"]))
            except Exception as e:
                log.warning("读语音库 %s 失败：%s", p.name, e)
        return out

    def _decode(self, body_hex: str, ltype: int, chat_id: str, wxid: str, is_group: bool):
        """返回 (可读文本, 文件名或None)。文件名只有「文件」类消息才有。"""
        # 纯二进制类型（图片/语音/视频等）记录里没有可读内容，只给占位
        if ltype not in (1, 49):
            return TYPE_LABEL.get(ltype, "[非文本消息]"), None
        raw = bytes.fromhex(body_hex) if body_hex else b""
        if raw.startswith(ZSTD_MAGIC):
            try:
                raw = zstd_decompress(raw)
            except Exception:
                return TYPE_LABEL.get(ltype, "[非文本消息]"), None
        text = raw.decode("utf-8", "replace")
        # 群消息正文前带 "wxid:\n" 前缀，去掉
        if is_group and wxid and text.startswith(wxid + ":\n"):
            text = text[len(wxid) + 2:]
        if ltype == 1:
            return (text or "（空）"), None
        return parse_appmsg(text)  # ltype == 49：应用消息，细分成公众号/文件/小程序等

    def copy_file(self, filename: str, raw_dir: Path, day: str, max_mb: float):
        """把微信本机存的真实文件（msg/file 下）复制到知识库里 Agent 能读的地方，返回相对路径。"""
        name = os.path.basename(filename)
        if not name:
            return None
        # msg/file 和 db_storage 同级，在账号目录下（db_storage 的上一级）
        src = None
        for dp, _dirs, files in os.walk(self.root.parent / "msg" / "file"):
            if name in files:
                src = Path(dp) / name
                break
        if not src:
            return None  # 文件可能还没下载到本机
        try:
            if src.stat().st_size > max_mb * 1024 * 1024:
                return None  # 太大，不搬
        except OSError:
            return None
        dest = Path(raw_dir) / "wechat" / "files" / day / name
        try:
            if not (dest.exists() and dest.stat().st_size == src.stat().st_size):
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        except OSError as e:
            log.warning("复制文件 %s 失败：%s", name, e)
            return None
        return f"spaces/me/raw/wechat/files/{day}/{name}"


# ---------- 语音转文字 ----------

def whisper_dir() -> Path:
    return Path(os.environ.get("AGENTOS_WHISPER_MODEL") or STATE_DIR / "whisper" / "small")


def transcribe_voices(audio: dict[str, bytes]) -> dict[str, dict]:
    """把一批语音交给 ops/transcribe_voice.py（同一个专用 Python），返回 {server_id: {"text", "sec"}}。
    没有模型、超时或出错时返回空字典，调用方照旧只写「[语音]」，不影响采集。"""
    if not audio or not (whisper_dir() / "model.bin").exists():
        return {}
    script = Path(__file__).resolve().parent / "transcribe_voice.py"
    with tempfile.TemporaryDirectory(prefix="agentos-voice-") as tmp:
        for sid, data in audio.items():
            (Path(tmp) / f"{sid}.silk").write_bytes(data)
        try:
            r = subprocess.run([python(), "-X", "utf8", str(script), tmp], capture_output=True,
                               encoding="utf-8", errors="replace", env=child_env(),
                               timeout=min(60 + 20 * len(audio), 600), creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("语音转文字失败：%s", e)
            return {}
    out = {}
    for line in (r.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("ok"):
            out[d["name"].rsplit(".", 1)[0]] = {"text": d.get("text", ""), "sec": d.get("sec")}
    if r.returncode != 0 and not out:
        log.warning("语音转文字失败：%s", (r.stderr or "").strip()[-300:])
    return out


def voice_line(d: dict | None) -> str:
    """语音那一行写什么：「[语音 14秒] 转出的文字」；没转成就只写「[语音]」。"""
    if not d:
        return "[语音]"
    sec = d.get("sec")
    head = f"[语音 {round(sec)}秒]" if isinstance(sec, (int, float)) and sec >= 0.5 else "[语音]"
    return f"{head} {d['text']}" if d.get("text") else f"{head}（没听出内容）"


# ---------- 采集 ----------

def safe_name(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_")[:40] or "未命名"


def collect_chat(wx: Wechat, sess: dict, st: dict, raw_dir: Path, initial_days: int,
                 self_id: str, now: float, files_max_mb: float = 25, transcribe=None) -> int:
    chat_id = sess["username"]
    cs = st["chats"].setdefault(chat_id, {})
    last_ts_seen = int(sess.get("last_timestamp") or 0)
    if cs.get("last_ts") and last_ts_seen and last_ts_seen <= cs["last_ts"]:
        return 0  # 这个会话自上次采集后没有新活动
    is_group = chat_id.endswith("@chatroom")
    kind = "群" if is_group else "人"
    title = wx.display_name(chat_id)
    dbs = wx.message_dbs_for(chat_id, cs.get("dbs"))
    if not dbs:
        cs["dbs"] = []
        cs["last_ts"] = last_ts_seen
        return 0
    cs["dbs"] = dbs
    cursor = dict(cs.get("cursor", {}))
    start_ts = int(now - initial_days * 86400)
    by_day: dict[str, list[str]] = {}
    n = 0
    for dbname in dbs:
        try:
            rows = wx.read_new(chat_id, dbname, cursor.get(dbname, 0), start_ts, is_group)
        except Exception as e:
            log.warning("读 %s/%s 失败：%s", title, dbname, e)
            continue
        voices = [r[5] for r in rows if r[5]]
        heard = {}
        if voices and transcribe:  # 这个会话的语音一次转完（模型只加载一次）
            try:
                heard = transcribe(wx.voice_audio(voices))
            except Exception as e:  # noqa: BLE001 — 转不了就只留「[语音]」
                log.warning("%s 的语音转文字失败：%s", title, e)
        for lid, ts, wxid, text, attach, voice in rows:
            if voice:
                text = voice_line(heard.get(voice))
            cursor[dbname] = max(cursor.get(dbname, 0), lid)
            dt = datetime.fromtimestamp(ts)
            day = dt.strftime("%Y-%m-%d")
            who = "我" if wxid == self_id else (wx.display_name(wxid) if wxid else "?")
            if attach and files_max_mb > 0:  # 「文件」消息：把本机真实文件搬进知识库，附上路径供 Agent 读正文
                rel = wx.copy_file(attach, raw_dir, day, files_max_mb)
                if rel:
                    text = f"{text}\n（已存，可读正文：{rel}）"
            first, *rest = (text or "（空）").split("\n")
            line = f"- {dt:%H:%M} {who}：{first}"
            by_day.setdefault(dt.strftime("%Y-%m-%d"), []).append(
                "\n".join([line] + ["  " + r for r in rest]))
            n += 1
    for day, lines in sorted(by_day.items()):
        path = raw_dir / "wechat" / day / f"{safe_name(title)}-{chat_id[-6:]}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        with open(path, "a", encoding="utf-8") as f:  # 原文只追加
            if new_file:
                f.write(f"# {title}（{day}）\n\n- 会话: {chat_id}（{kind}）\n\n")
            f.write("\n".join(lines) + "\n")
    cs["cursor"] = cursor
    cs["last_ts"] = last_ts_seen
    cs["title"] = title
    return n


def setup(argv_check: bool) -> tuple[Wechat | None, Path | None, int]:
    """读配置、找钥匙和账号，返回 (Wechat, db_root, 出错时的退出码)。"""
    load_env(STATE_DIR / "wechat.env")
    keys_path = Path(os.environ.get("WECHAT_KEYS") or DEFAULT_KEYS).expanduser()
    if not keys_path.exists():
        if argv_check:
            log.error("没有钥匙文件 %s：先按教程第十三步取钥匙（或在 wechat.env 里用 WECHAT_KEYS 指定位置）", keys_path)
            return None, None, 2
        log.info("没有钥匙文件 %s，跳过微信采集", keys_path)
        return None, None, 0
    try:
        keys = wechat_db.keys_from_file(keys_path)
    except wechat_db.WechatDBError as e:
        log.error("%s", e)
        return None, None, 2
    if not keys:
        log.error("钥匙文件 %s 里没有钥匙（64 位十六进制）", keys_path)
        return None, None, 2
    root_s = os.environ.get("WECHAT_DB_ROOT", "")
    if root_s:
        root = Path(root_s).expanduser()
        if not (root / "session" / "session.db").is_file():
            log.error("WECHAT_DB_ROOT 不对：%s 下面没有 session\\session.db（要填到 db_storage 这一层）", root)
            return None, None, 2
    else:
        root, why = pick_root(keys)
        if root is None:
            log.error("%s", why)
            return None, None, 2
        if argv_check:
            log.info("%s", why)
    return Wechat(root, keys, STATE_DIR / "wechat-cache"), root, 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    check, list_groups = "--check" in args, "--groups" in args
    # 检查时把说明打到标准输出（PowerShell 里不会被当成报错），定时采集时照常写日志
    logging.basicConfig(level=logging.INFO, stream=sys.stdout if (check or list_groups) else sys.stderr,
                        format="%(message)s" if (check or list_groups) else "%(asctime)s %(levelname)s %(message)s")
    wx, root, rc = setup(check or list_groups)
    if wx is None:
        return rc
    initial_days = int(os.environ.get("WECHAT_INITIAL_DAYS") or "3")
    files_max_mb = float(os.environ.get("WECHAT_FILES_MAX_MB") or "25")  # 0 关闭文件搬运
    raw_dir = Path(os.environ.get("AGENTOS_RAW") or RAW)
    # db_root 形如 ...\\xwechat_files\\wxid_xxx_yyyy\\db_storage，自己的 wxid 是账号文件夹名去掉末尾 _yyyy
    self_id = os.environ.get("WECHAT_SELF_ID") or root.parent.name.rsplit("_", 1)[0]
    groups = [g.strip() for g in re.split(r"[,，、\n]", os.environ.get("WECHAT_GROUPS", "")) if g.strip()]

    try:
        sessions = wx.sessions()
    except Exception as e:
        log.error("打开微信库失败（钥匙过期？微信退出登录或更新过？）：%s", e)
        return 2

    if check or list_groups:
        real = [s for s in sessions if s.get("username") and s["username"] not in SKIP_USERS
                and not s["username"].startswith("gh_")]
        grp = [s["username"] for s in real if s["username"].endswith("@chatroom")]
        print(f"✅ 读得出来：{len(real)} 个会话（私聊 {len(real) - len(grp)} 个，群 {len(grp)} 个）")
        print(f"   你自己的 wxid：{self_id}")
        if groups:
            hit = [wx.display_name(g) for g in grp if any(k == g or k in wx.display_name(g) for k in groups)]
            print(f"   WECHAT_GROUPS 命中的群（{len(hit)} 个）：" + ("、".join(hit) or "一个都没有，检查群名"))
        else:
            print("   WECHAT_GROUPS 没填：只采私聊。想采群，用 --groups 看群名，再填进 wechat.env")
        if list_groups:
            print("\n最近的群（按最后一条消息的时间，最多 40 个）：")
            for g in grp[:40]:
                print(f"   {wx.display_name(g)}")
        return 0

    # 语音转文字：有模型（安装脚本会下）就开；WECHAT_VOICE=0 关掉
    transcribe = None
    if os.environ.get("WECHAT_VOICE", "1") != "0":
        if (whisper_dir() / "model.bin").exists():
            transcribe = transcribe_voices
        else:
            log.info("没有语音模型（重跑安装脚本会下载），语音只留「[语音]」")

    st = load_state()
    now = time.time()
    total, chats = 0, 0
    for sess in sessions:
        u = sess.get("username") or ""
        if not u or u in SKIP_USERS or u.startswith("gh_"):
            continue
        if u.endswith("@chatroom"):  # 群：只采白名单命中的（群名含关键词，或 id 相等）
            name = wx.display_name(u)
            if not any(g == u or g in name for g in groups):
                continue
        try:
            n = collect_chat(wx, sess, st, raw_dir, initial_days, self_id, now, files_max_mb, transcribe)
        except Exception as e:
            log.warning("采集会话 %s 失败：%s", u, e)
            continue
        if n:
            total += n
            chats += 1
    save_state(st)
    log.info("微信采集完成：新存 %d 条，涉及 %d 个会话", total, chats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
