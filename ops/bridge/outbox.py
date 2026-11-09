"""两步确认：Agent 写的外发草稿 → 飞书确认卡片 → 你点「发送」才用你的邮箱发出。

Agent 拿不到任何发送凭据，只能在 spaces/me/outbox/pending/ 写一份 JSON 草稿：
  {"to": ["a@b.com"], "cc": [], "subject": "主题", "body": "正文",
   "attachments": ["spaces/me/outbox/12/合同.docx"]}
发信由桥接服务（你的账号）用 ~/.agentos/mail.env 里的邮箱和授权码完成。
确认这一步由代码卡住，不靠提示词：没有你本人点「发送」，草稿永远只是文件。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
MAX_RECIPIENTS = 10
BODY_PREVIEW = 800


class DraftError(ValueError):
    pass


def draft_key(path: Path) -> str:
    """文件名 + 内容指纹：卡片发出后草稿被改过，指纹就对不上，拒绝发送。"""
    return f"{path.name}:{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}"


def _addrs(v) -> list[str]:
    if not v:
        return []
    items = [v] if isinstance(v, str) else list(v)
    return [str(a).strip() for a in items if str(a).strip()]


def load_draft(path: Path, runtime: Path) -> dict:
    """读并校验草稿；不合格抛 DraftError（原因会推给你）。"""
    try:
        d = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise DraftError(f"草稿不是合法 JSON：{e}") from None
    to, cc = _addrs(d.get("to")), _addrs(d.get("cc"))
    if not to:
        raise DraftError("没有收件人")
    if len(to) + len(cc) > MAX_RECIPIENTS:
        raise DraftError(f"收件人超过 {MAX_RECIPIENTS} 个")
    bad = [a for a in to + cc if not EMAIL_RE.match(a)]
    if bad:
        raise DraftError(f"邮箱地址不对：{', '.join(bad)}")
    subject = str(d.get("subject") or "").strip()
    if not subject:
        raise DraftError("没有主题")
    allowed = [(runtime / "spaces/me/outbox").resolve(), (runtime / "spaces/me/raw/inbox").resolve()]
    atts = []
    for a in d.get("attachments") or []:
        p = Path(a)
        p = (p if p.is_absolute() else runtime / p).resolve()
        if not any(p.is_relative_to(base) for base in allowed):
            raise DraftError(f"附件只能来自 outbox/ 或收件箱：{a}")
        if not p.is_file():
            raise DraftError(f"附件不存在：{a}")
        atts.append(p)
    return {"to": to, "cc": cc, "subject": subject, "body": str(d.get("body") or ""), "attachments": atts}


def _summary_md(d: dict) -> str:
    body = d["body"] if len(d["body"]) <= BODY_PREVIEW else d["body"][:BODY_PREVIEW] + "…（正文较长，已截断显示）"
    lines = [f"**收件人**：{', '.join(d['to'])}"]
    if d["cc"]:
        lines.append(f"**抄送**：{', '.join(d['cc'])}")
    lines.append(f"**主题**：{d['subject']}")
    if d["attachments"]:
        lines.append("**附件**：" + "、".join(p.name for p in d["attachments"]))
    lines += ["**正文**：", body or "（空）"]
    return "\n".join(lines)


def confirm_card(key: str, d: dict) -> dict:
    def button(text, kind, act):
        return {"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": kind,
                "behaviors": [{"type": "callback", "value": {"draft": key, "act": act}}]}
    return {"schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "📮 待你确认：发一封邮件"}, "template": "orange"},
            "body": {"elements": [
                {"tag": "markdown", "content": _summary_md(d)},
                {"tag": "markdown", "content": "点「发送」才会发出；点「先不发」什么都不会发。"},
                {"tag": "column_set", "columns": [
                    {"tag": "column", "width": "auto", "elements": [button("发送", "primary", "send")]},
                    {"tag": "column", "width": "auto", "elements": [button("先不发", "default", "cancel")]}]}]}}


def decided_card(d: dict | None, status: str, template: str = "grey") -> dict:
    elements = [{"tag": "markdown", "content": _summary_md(d)}] if d else []
    return {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": status}, "template": template},
            "body": {"elements": elements or [{"tag": "markdown", "content": status}]}}


def send_mail(d: dict, user: str, password: str, host: str, port: int = 465,
              smtp_factory=smtplib.SMTP_SSL) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, ", ".join(d["to"]), d["subject"]
    if d["cc"]:
        msg["Cc"] = ", ".join(d["cc"])
    msg.set_content(d["body"] or " ")
    for p in d["attachments"]:
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    with smtp_factory(host, port, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)
