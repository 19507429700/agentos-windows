#!/usr/bin/env python3
"""邮箱采集：IMAP 只读增量拉取，过滤广告和系统通知，存成 Markdown 原文。

以你自己的账号运行（不是 agentos）：邮箱授权码通常也能发信，所以只放在你这边，Agent 拿不到。
  python3 ops/collect_mail.py            # 增量拉取（第一次拉最近 MAIL_INITIAL_DAYS 天）
  python3 ops/collect_mail.py --check    # 只测试能否登录，不拉取

配置 ~/.agentos/mail.env（权限 600）：
  MAIL_IMAP_HOST=imap.qq.com   MAIL_USER=...   MAIL_PASSWORD=<授权码，不是登录密码>
可选：MAIL_IMAP_PORT=993  MAIL_FOLDERS=INBOX  MAIL_INITIAL_DAYS=7
输出：$AGENTOS_RAW/mail/<日期>/<时分秒>-<UID>.md，默认 AGENTOS_RAW=/Users/agentos/agentos/spaces/me/raw
状态：~/.agentos/mail_state.json（每个文件夹的 UIDVALIDITY 和已拉到的 UID）

底线：邮件只用 BODY.PEEK 读，不改已读状态；已存在的原文文件永不覆盖；拉取失败退出码非 0（供告警）。
只用标准库，兼容 /usr/bin/python3（3.9）。
"""

from __future__ import annotations

import argparse
import email
import email.policy
import html
import imaplib
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

log = logging.getLogger("collect_mail")

STATE_DIR = Path(os.environ.get("AGENTOS_STATE_DIR", Path.home() / ".agentos"))
MAX_BODY_CHARS = 20000
BULK_SENDER_RE = re.compile(
    r"(no-?reply|do-?not-?reply|notification|notice|newsletter|marketing|mailer-daemon"
    r"|postmaster|edm|promo|digest|bounce"
    r"|@healthchecks\.io$)", re.I)  # 自己的任务监控发的登录、停摆提醒，不是往来邮件


# ---------- 配置与状态 ----------

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def write_atomic(path: Path, data: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# ---------- 过滤 ----------

def is_bulk(msg: EmailMessage) -> str | None:
    """广告、订阅、系统通知返回原因，正常往来邮件返回 None。"""
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return "订阅/群发"
    if (msg.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk"):
        return "群发"
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return "系统自动"
    sender = " ".join(addr for _, addr in getaddresses([msg.get("From", "")]))
    if BULK_SENDER_RE.search(sender):
        return "通知类发件人"
    return None


# ---------- 解析 ----------

def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|head).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):  # 罕见编码
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", errors="replace")
    if part.get_content_subtype() == "html":
        text = html_to_text(text)
    text = text.replace("\r\n", "\n").strip()
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + f"\n\n（正文过长，已截断，原文 {len(text)} 字）"
    return text


def attachment_names(msg: EmailMessage) -> list[str]:
    return [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]


def mail_time(msg: EmailMessage) -> datetime:
    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
        if dt.tzinfo is None:
            return dt
        return dt.astimezone()  # 转成本机时区（Asia/Shanghai）
    except (TypeError, ValueError, IndexError):
        return datetime.now()


def to_markdown(msg: EmailMessage, folder: str, uid: int) -> str:
    def hdr(name):
        return " ".join(str(msg.get(name, "")).split())

    lines = [f"# {hdr('Subject') or '（无主题）'}", "",
             f"- 发件人: {hdr('From')}",
             f"- 收件人: {hdr('To')}"]
    if msg.get("Cc"):
        lines.append(f"- 抄送: {hdr('Cc')}")
    lines += [f"- 时间: {mail_time(msg).strftime('%Y-%m-%d %H:%M %z')}",
              f"- 邮箱: {folder}  UID: {uid}",
              f"- Message-ID: {hdr('Message-ID')}"]
    atts = attachment_names(msg)
    if atts:
        lines.append(f"- 附件: {', '.join(atts)}")
    lines += ["", "---", "", body_text(msg) or "（无正文）", ""]
    return "\n".join(lines)


# ---------- IMAP ----------

def connect(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(host, port, timeout=60)
    conn.login(user, password)
    # 网易邮箱（163/126/yeah）不发 ID 会报 Unsafe Login；其他服务器不认也无妨
    imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
    try:
        conn._simple_command("ID", '("name" "agentos" "version" "1.0")')
    except imaplib.IMAP4.error:
        pass
    return conn


def quote_folder(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_fetch(data) -> tuple[int, bytes] | None:
    """从 UID FETCH 的返回里取出 (uid, 原始邮件字节)。"""
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            m = re.search(rb"UID (\d+)", item[0])
            if m:
                return int(m.group(1)), item[1]
    return None


def collect_folder(conn, folder: str, st: dict, raw_dir: Path, initial_days: int) -> dict:
    """拉一个文件夹的新邮件，返回 {"saved": n, "skipped": n, "filtered": {原因: n}}。st 会被就地更新。"""
    typ, _ = conn.select(quote_folder(folder), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"打不开邮箱文件夹 {folder}")
    _, uv = conn.response("UIDVALIDITY")
    _, un = conn.response("UIDNEXT")
    uidvalidity = int(uv[0]) if uv and uv[0] else 0
    if st.get("uidvalidity") != uidvalidity:  # 第一次，或服务器重建了文件夹
        st.clear()
        st["uidvalidity"] = uidvalidity
        # 补拉完最近几天后，起点推到当前最新一封；否则最近几天没邮件时，下次会拉全部历史
        floor = int(un[0]) - 1 if un and un[0] else 0
        since = (datetime.now() - timedelta(days=initial_days)).strftime("%d-%b-%Y")
        typ, data = conn.uid("search", None, "SINCE", since)
        last = 0
    else:
        floor = 0
        last = st.get("last_uid", 0)
        typ, data = conn.uid("search", None, "UID", f"{last + 1}:*")
    if typ != "OK":
        raise RuntimeError(f"搜索 {folder} 失败")
    # 「N:*」在没有新邮件时会返回最大的那封，所以要再滤一遍
    uids = sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > last)

    stats = {"saved": 0, "skipped": 0, "filtered": {}}
    for uid in uids:
        typ, data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")  # PEEK：不标已读
        got = parse_fetch(data) if typ == "OK" else None
        if got is None:
            raise RuntimeError(f"取不到 {folder} UID {uid}")
        msg = email.message_from_bytes(got[1], policy=email.policy.default)
        reason = is_bulk(msg)
        if reason:
            stats["filtered"][reason] = stats["filtered"].get(reason, 0) + 1
        else:
            dt = mail_time(msg)
            path = raw_dir / "mail" / dt.strftime("%Y-%m-%d") / f"{dt.strftime('%H%M%S')}-{uid}.md"
            if path.exists():
                stats["skipped"] += 1  # 原文只追加不修改
            else:
                write_atomic(path, to_markdown(msg, folder, uid))
                stats["saved"] += 1
        st["last_uid"] = max(st.get("last_uid", 0), uid)  # 每封都推进，中途失败下次从断点续
    st["last_uid"] = max(st.get("last_uid", 0), floor)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="IMAP 只读增量采集邮件到 raw/mail/")
    ap.add_argument("--check", action="store_true", help="只测试登录")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    load_env(STATE_DIR / "mail.env")
    host, user, password = (os.environ.get(k, "") for k in ("MAIL_IMAP_HOST", "MAIL_USER", "MAIL_PASSWORD"))
    if not (host and user and password):
        log.error("缺少 MAIL_IMAP_HOST / MAIL_USER / MAIL_PASSWORD，请写进 %s", STATE_DIR / "mail.env")
        return 2
    port = int(os.environ.get("MAIL_IMAP_PORT", "993"))
    folders = [f.strip() for f in os.environ.get("MAIL_FOLDERS", "INBOX").split(",") if f.strip()]
    raw_dir = Path(os.environ.get("AGENTOS_RAW", "/Users/agentos/agentos/spaces/me/raw"))
    initial_days = int(os.environ.get("MAIL_INITIAL_DAYS", "7"))
    state_path = STATE_DIR / "mail_state.json"

    try:
        conn = connect(host, port, user, password)
    except (imaplib.IMAP4.error, OSError) as e:
        log.error("登录 %s 失败（检查授权码、是否开了 IMAP、网络）：%s", host, e)
        return 1
    try:
        if a.check:
            log.info("登录 %s 成功", host)
            return 0
        state = load_state(state_path)
        failed = False
        for folder in folders:
            st = state.setdefault(folder, {})
            try:
                stats = collect_folder(conn, folder, st, raw_dir, initial_days)
                log.info("%s：新存 %d 封，已存在 %d 封，过滤 %s", folder, stats["saved"],
                         stats["skipped"], stats["filtered"] or "无")
            except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
                failed = True
                log.error("%s 采集失败：%s", folder, e)
            finally:
                write_atomic(state_path, json.dumps(state, ensure_ascii=False, indent=1), 0o600)
        return 1 if failed else 0
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


if __name__ == "__main__":
    sys.exit(main())
