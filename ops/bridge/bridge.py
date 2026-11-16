"""AgentOS 桥接服务（Windows 版）：飞书长连接 → SQLite 队列 → Agent（ops/run_agent.py）→ 回推飞书。

由任务计划程序在你登录 Windows 后启动（AgentOS-Bridge），独占飞书凭据。
Windows 版没有单独的 agentos 账号：Agent 和你用同一个账号，靠权限规则（.claude/settings.json）
禁止它读 %USERPROFILE%\\.agentos 里的钥匙。配置见 %USERPROFILE%\\.agentos\\bridge.env。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ops/：agentos_env；写飞书文档要用 feishu_user
import outbox  # noqa: E402
from agentos_env import NO_WINDOW, REPO, kill_tree, python  # noqa: E402

log = logging.getLogger("bridge")

STATE_DIR = Path(os.environ.get("AGENTOS_STATE_DIR", Path.home() / ".agentos"))
ENV_FILE = STATE_DIR / "bridge.env"
SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
NEW_SESSION_WORDS = {"/new", "/reset", "新对话", "开新对话"}
STOP_WORDS = {"/stop", "停止", "别做了"}
STATUS_WORDS = {"/status", "状态"}
HELP_WORDS = {"/help", "/?", "帮助"}
CARD_CHUNK = 6000
# 文件收件箱：这些消息类型的附件下载存进 raw/inbox/<日期>/；值是下载接口的 type
ATTACH_TYPES = {"image": "image", "file": "file", "media": "file", "audio": "file"}
ATTACH_LABEL = {"image": "图片", "file": "文件", "media": "视频", "audio": "语音"}
# 文件回传：outbox/<任务号>/ 里的文件发回飞书
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
FILE_TYPES = {".pdf": "pdf", ".doc": "doc", ".docx": "doc", ".xls": "xls", ".xlsx": "xls",
              ".ppt": "ppt", ".pptx": "ppt", ".mp4": "mp4", ".opus": "opus"}
MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 飞书文件消息上限 30MB

HELP_TEXT = """**能做什么**
直接发文字，像跟助理说话一样派活。同一个对话会接着上文聊。我读得到你的飞书群聊和私聊、QQ 邮箱，回答都带出处。

**常用说法**
- 「明天见张总，帮我准备一下」：会前材料，出一份 Word
- 「按模板出一份合同 / 方案」「把对方改过的合同标一下」：合同出件
- 「查一下某公司 / 某行业」：网上调研，附来源链接
- 「这周我和某某谈了什么」：查往来记录
- 「把这份发到 xx@xx.com」：我写好草稿，弹确认卡片，你点「发送」才发
- 「建一个飞书文档，写上……」「往那篇文档里补上……」：建在你的云盘里，把链接发给你
- 发图片、文件给我：存进收件箱，我看完告诉你是什么

**每晚 23:00**
自动推复盘到飞书和微信。回复「复盘：你的回答」，我记进拍板记录。

**命令**
- `新对话`（或 /new）：清掉上下文，从头开始
- `停止`（或 /stop）：中止正在做的任务，并清掉排队的消息
- `状态`（或 /status）：看看有没有在干活、排了几条
- `帮助`（或 /help）：显示这段说明"""


# ---------- 配置 ----------

def load_env(path: Path) -> None:
    """把 KEY=VALUE 文件读进 os.environ（已存在的环境变量优先）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    app_id: str
    app_secret: str
    owner_open_id: str
    runtime_dir: str
    agent_user: str
    max_concurrency: int
    stuck_notice_sec: int
    hard_timeout_sec: int
    claude_cmd: list[str]
    # 两步确认后发信用的邮箱（~/.agentos/mail.env，和采集共用一份授权码）
    mail_user: str = ""
    mail_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    # 写飞书文档：建在哪个云盘文件夹（空 = 「我的空间」根目录）
    doc_folder: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        runtime = os.environ.get("AGENTOS_RUNTIME", "") or str(REPO)
        agent_user = os.environ.get("AGENT_USER", "agentos")
        override = os.environ.get("AGENTOS_CLAUDE_CMD")  # 仅测试用：直接跑某个假命令代替 Agent
        cmd = shlex.split(override) if override else [python(), str(Path(runtime) / "ops" / "run_agent.py")]
        return cls(
            app_id=os.environ.get("FEISHU_APP_ID", ""),
            app_secret=os.environ.get("FEISHU_APP_SECRET", ""),
            owner_open_id=os.environ.get("OWNER_OPEN_ID", ""),
            runtime_dir=runtime,
            agent_user=agent_user,
            max_concurrency=int(os.environ.get("MAX_CONCURRENCY", "2")),
            stuck_notice_sec=int(os.environ.get("STUCK_NOTICE_MIN", "15")) * 60,
            hard_timeout_sec=int(os.environ.get("HARD_TIMEOUT_MIN", "45")) * 60,
            claude_cmd=cmd,
            mail_user=os.environ.get("MAIL_USER", ""),
            mail_password=os.environ.get("MAIL_PASSWORD", ""),
            smtp_host=os.environ.get("MAIL_SMTP_HOST")
            or os.environ.get("MAIL_IMAP_HOST", "").replace("imap.", "smtp.", 1),
            smtp_port=int(os.environ.get("MAIL_SMTP_PORT", "465")),
            doc_folder=os.environ.get("FEISHU_DOC_FOLDER", "").strip(),
        )


# ---------- 队列 ----------

class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT UNIQUE NOT NULL,
                chat_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',  -- queued / running / done / failed / cancelled
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            -- 外发草稿：key = 文件名+内容指纹；status: pending / sending / sent / cancelled / failed / invalid
            CREATE TABLE IF NOT EXISTS drafts (
                key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                decided_at REAL,
                detail TEXT
            );
        """)

    def draft_status(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT status FROM drafts WHERE key=?", (key,)).fetchone()
            return row["status"] if row else None

    def add_draft(self, key: str, name: str, status: str, detail: str | None = None) -> None:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO drafts (key, name, status, created_at, detail) VALUES (?, ?, ?, ?, ?)",
                            (key, name, status, time.time(), detail))

    def set_draft(self, key: str, status: str, detail: str | None = None, expect: str | None = None) -> bool:
        """改状态；给了 expect 就只在当前状态等于它时才改（防止连点两次发两封）。"""
        with self.lock:
            sql = "UPDATE drafts SET status=?, decided_at=?, detail=? WHERE key=?"
            args = [status, time.time(), detail, key]
            if expect:
                sql += " AND status=?"
                args.append(expect)
            return self.db.execute(sql, args).rowcount == 1

    def enqueue(self, message_id: str, chat_id: str, sender: str, text: str) -> bool:
        """返回 False 表示这条消息已经收过（飞书超时重推）。"""
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO tasks (message_id, chat_id, sender, text, created_at)"
                " VALUES (?, ?, ?, ?, ?)", (message_id, chat_id, sender, text, time.time()))
            return cur.rowcount == 1

    def claim_next(self, busy_chats: set[str]) -> sqlite3.Row | None:
        """取最早的排队任务；同一对话一次只跑一个，保证会话上下文按顺序接续。"""
        with self.lock:
            for row in self.db.execute(
                    "SELECT * FROM tasks WHERE status='queued' ORDER BY id").fetchall():
                if row["chat_id"] in busy_chats:
                    continue
                self.db.execute("UPDATE tasks SET status='running', started_at=? WHERE id=?",
                                (time.time(), row["id"]))
                return row
            return None

    def finish(self, task_id: int, ok: bool, error: str | None = None) -> None:
        with self.lock:
            self.db.execute("UPDATE tasks SET status=?, finished_at=?, error=? WHERE id=?",
                            ("done" if ok else "failed", time.time(), error, task_id))

    def seen(self, message_id: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM tasks WHERE message_id=?", (message_id,)).fetchone() is not None

    def cancel_queued(self, chat_id: str) -> int:
        with self.lock:
            return self.db.execute(
                "UPDATE tasks SET status='cancelled', finished_at=? WHERE chat_id=? AND status='queued'",
                (time.time(), chat_id)).rowcount

    def count(self, chat_id: str, status: str) -> int:
        with self.lock:
            return self.db.execute("SELECT count(*) FROM tasks WHERE chat_id=? AND status=?",
                                   (chat_id, status)).fetchone()[0]

    def recover_interrupted(self) -> list[sqlite3.Row]:
        """服务重启时还在 running 的任务不自动重跑（可能已做了一半），标失败并通知。"""
        with self.lock:
            rows = self.db.execute("SELECT * FROM tasks WHERE status='running'").fetchall()
            self.db.execute("UPDATE tasks SET status='failed', finished_at=?, error='interrupted'"
                            " WHERE status='running'", (time.time(),))
            return rows

    def get_session(self, chat_id: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT session_id FROM sessions WHERE chat_id=?",
                                  (chat_id,)).fetchone()
            return row["session_id"] if row else None

    def set_session(self, chat_id: str, session_id: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE"
                            " SET session_id=excluded.session_id, updated_at=excluded.updated_at",
                            (chat_id, session_id, time.time()))

    def clear_session(self, chat_id: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))


# ---------- 调用 Claude ----------

@dataclass
class AgentResult:
    text: str
    session_id: str | None
    is_error: bool
    stderr: str = ""


def describe_step(block: dict) -> str:
    """把一次工具调用压成一句话，用于「卡在哪一步」。"""
    name = block.get("name", "?")
    inp = block.get("input") or {}
    for key in ("description", "command", "file_path", "pattern", "url", "query", "skill"):
        if inp.get(key):
            val = str(inp[key]).replace("\n", " ")
            return f"{name}: {val[:80]}"
    return name


def run_agent(cfg: Config, prompt: str, session_id: str | None,
              on_stuck=None, on_start=None) -> AgentResult:
    """跑一轮 claude -p（stream-json），边读边记最后一步；超时先提醒，再强杀。
    on_start(proc) 拿到进程句柄，供「停止」命令中止。"""
    sid_arg = session_id if session_id and SESSION_ID_RE.match(session_id) else "-"
    # 指定 UTF-8：中文 Windows 默认按 GBK 解码，Agent 输出的中文会乱码；不弹黑窗口
    proc = subprocess.Popen(cfg.claude_cmd + [sid_arg], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if on_start:
        on_start(proc)
    with proc:  # 退出时关闭管道，常驻服务不能泄漏文件句柄
        return _drive(cfg, proc, prompt, on_stuck)


def stop_process(proc: subprocess.Popen) -> None:
    """连同 Agent 启动的子进程一起结束（Windows 上只结束外层，里面的 Agent 会一直挂着）。"""
    kill_tree(proc)


def _drive(cfg: Config, proc: subprocess.Popen, prompt: str, on_stuck) -> AgentResult:
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError:
        pass  # 进程秒退（如会话不存在），下面照常读 stderr

    state = {"last_step": "启动中", "result": None, "session_id": None}
    stderr_buf: list[str] = []

    def read_stdout():
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("session_id"):
                state["session_id"] = ev["session_id"]
            if ev.get("type") == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        state["last_step"] = describe_step(block)
            elif ev.get("type") == "result":
                state["result"] = ev

    def read_stderr():
        for line in proc.stderr:
            stderr_buf.append(line)

    readers = [threading.Thread(target=read_stdout, daemon=True),
               threading.Thread(target=read_stderr, daemon=True)]
    for t in readers:
        t.start()

    start = time.time()
    noticed = False
    while proc.poll() is None:
        elapsed = time.time() - start
        if not noticed and elapsed > cfg.stuck_notice_sec:
            noticed = True
            if on_stuck:
                on_stuck(int(elapsed // 60), state["last_step"])
        if elapsed > cfg.hard_timeout_sec:
            stop_process(proc)
            for t in readers:
                t.join(2)
            return AgentResult(f"超时（{cfg.hard_timeout_sec // 60} 分钟）已中止。最后一步：{state['last_step']}",
                               state["session_id"], True, "".join(stderr_buf))
        time.sleep(1)
    for t in readers:
        t.join(5)

    res = state["result"]
    stderr = "".join(stderr_buf)
    if res is None:
        return AgentResult(f"Agent 异常退出（code {proc.returncode}）：{stderr.strip()[-500:] or '无输出'}",
                           state["session_id"], True, stderr)
    text = res.get("result") or ""
    is_error = bool(res.get("is_error")) or res.get("subtype", "success") != "success"
    if is_error and not text:
        text = f"Agent 出错：{res.get('subtype')}"
    return AgentResult(text, res.get("session_id") or state["session_id"], is_error, stderr)


# ---------- 飞书 ----------

NET_RETRY_WAITS = (2, 5)  # 网络断了（10054 之类）等几秒再试；三次都失败才报错


def with_retry(call, what: str):
    """调飞书接口，遇到网络错误（连接被重置、超时……）自动重试，免得 Agent 已经写好的回答发不出去就丢了。
    requests 的网络异常都是 OSError 的子类；飞书返回的业务错误（权限不够等）不在这里重试。"""
    for wait in NET_RETRY_WAITS + (None,):
        try:
            return call()
        except OSError as e:
            if wait is None:
                raise
            log.warning("feishu %s 网络出错，%s 秒后重试：%r", what, wait, e)
            time.sleep(wait)


class Feishu:
    def __init__(self, cfg: Config):
        import lark_oapi as lark
        self.lark = lark
        self.client = lark.Client.builder().app_id(cfg.app_id).app_secret(cfg.app_secret) \
            .log_level(lark.LogLevel.WARNING).build()

    def reply_text(self, message_id: str, text: str) -> None:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        req = ReplyMessageRequest.builder().message_id(message_id).request_body(
            ReplyMessageRequestBody.builder().msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False)).build()).build()
        self._check(with_retry(lambda: self.client.im.v1.message.reply(req), "reply"), "reply")

    def send(self, chat_id: str, msg_type: str, content: str) -> bool:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
            CreateMessageRequestBody.builder().receive_id(chat_id).msg_type(msg_type)
            .content(content).build()).build()
        return self._check(with_retry(lambda: self.client.im.v1.message.create(req), "send"), "send")

    def send_text(self, chat_id: str, text: str) -> None:
        self.send(chat_id, "text", json.dumps({"text": text}, ensure_ascii=False))

    def send_to_user(self, open_id: str, msg_type: str, content: str) -> bool:
        """按 open_id 私聊发（确认卡片、发信结果只发给你本人）。"""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = CreateMessageRequest.builder().receive_id_type("open_id").request_body(
            CreateMessageRequestBody.builder().receive_id(open_id).msg_type(msg_type)
            .content(content).build()).build()
        return self._check(with_retry(lambda: self.client.im.v1.message.create(req), "send to user"), "send to user")

    def send_markdown(self, chat_id: str, md: str) -> None:
        """Claude 的回复是 Markdown，用卡片渲染；太长就分几张，卡片失败退回纯文本。"""
        for chunk in split_chunks(md, CARD_CHUNK):
            card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": chunk}]}}
            if not self.send(chat_id, "interactive", json.dumps(card, ensure_ascii=False)):
                self.send_text(chat_id, chunk)

    def download(self, message_id: str, key: str, rtype: str) -> tuple[bytes, str]:
        """下载用户发来的消息附件，返回 (内容, 文件名)。"""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        req = GetMessageResourceRequest.builder().message_id(message_id).file_key(key).type(rtype).build()
        resp = with_retry(lambda: self.client.im.v1.message_resource.get(req), "download")
        if not self._check(resp, "download"):
            raise RuntimeError(f"下载附件失败：{resp.code} {resp.msg}")
        return resp.file.read(), resp.file_name or ""

    def send_file(self, chat_id: str, path: Path) -> bool:
        """上传本地文件并作为文件消息发出；图片作为图片消息。"""
        from lark_oapi.api.im.v1 import (CreateFileRequest, CreateFileRequestBody,
                                         CreateImageRequest, CreateImageRequestBody)
        if path.suffix.lower() in IMAGE_EXTS:
            def upload_image():
                with open(path, "rb") as f:  # 每次重试都重新打开，上一次可能已经读到文件末尾
                    req = CreateImageRequest.builder().request_body(
                        CreateImageRequestBody.builder().image_type("message").image(f).build()).build()
                    return self.client.im.v1.image.create(req)
            resp = with_retry(upload_image, "upload image")
            if not self._check(resp, "upload image"):
                return False
            return self.send(chat_id, "image", json.dumps({"image_key": resp.data.image_key}))

        def upload_file():
            with open(path, "rb") as f:
                req = CreateFileRequest.builder().request_body(
                    CreateFileRequestBody.builder().file_type(FILE_TYPES.get(path.suffix.lower(), "stream"))
                    .file_name(path.name).file(f).build()).build()
                return self.client.im.v1.file.create(req)
        resp = with_retry(upload_file, "upload file")
        if not self._check(resp, "upload file"):
            return False
        return self.send(chat_id, "file", json.dumps({"file_key": resp.data.file_key}))

    @staticmethod
    def _check(resp, what: str) -> bool:
        if not resp.success():
            log.error("feishu %s failed: code=%s msg=%s log_id=%s",
                      what, resp.code, resp.msg, resp.get_log_id())
            return False
        return True


def split_chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text or "（空回复）"]
    chunks, cur = [], ""
    for para in text.split("\n"):
        while len(para) > size:  # 单段超长就硬切
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(para[:size])
            para = para[size:]
        if len(cur) + len(para) + 1 > size:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


# ---------- 调度 ----------

class Bridge:
    def __init__(self, cfg: Config, store: Store, feishu):
        self.cfg, self.store, self.feishu = cfg, store, feishu
        self.busy: set[str] = set()
        self.busy_lock = threading.Lock()
        self.wake = threading.Event()
        # 以下三项受 busy_lock 保护
        self.running: dict[str, int] = {}  # chat_id → 正在跑的任务 id
        self.procs: dict[str, subprocess.Popen] = {}  # chat_id → 该任务的 Claude 进程
        self.stopped: set[int] = set()  # 被「停止」中止的任务 id
        self.doc_lock = threading.Lock()  # 同一文件夹并发建文档飞书会报「文件夹被锁」，一次只写一份

    # 飞书事件回调：必须 3 秒内返回，只做校验、入队和回「收到」
    def on_message(self, message_id: str, chat_id: str, chat_type: str, sender: str,
                   msg_type: str, content: str) -> None:
        if chat_type != "p2p":
            return
        if not self.cfg.owner_open_id:
            self._async(self.feishu.reply_text, message_id,
                        f"还没绑定主人。你的 open_id 是：\n{sender}\n"
                        "把它写进 ~/.agentos/bridge.env 的 OWNER_OPEN_ID，然后重启桥接服务。")
            return
        if sender != self.cfg.owner_open_id:
            log.warning("ignored message from non-owner %s", sender)
            return
        if msg_type in ATTACH_TYPES:
            if not self.store.seen(message_id):  # 飞书重推的重复事件不再下载
                self._async(self._save_attachment, message_id, chat_id, sender, msg_type, content)
            return
        text = extract_text(msg_type, content)
        if text is None:
            self._async(self.feishu.reply_text, message_id, "这类消息还不支持，先发文字、图片或文件。")
            return
        word = text.strip().lower()
        if word in NEW_SESSION_WORDS:
            self.store.clear_session(chat_id)
            self._async(self.feishu.reply_text, message_id, "已开新对话。")
            return
        if word in STOP_WORDS:
            self._async(self.feishu.reply_text, message_id, self.stop_chat(chat_id))
            return
        if word in STATUS_WORDS:
            self._async(self.feishu.reply_text, message_id, self.status_text(chat_id))
            return
        if word in HELP_WORDS:
            self._async(self.feishu.send_markdown, chat_id, HELP_TEXT)
            return
        if not self.store.enqueue(message_id, chat_id, sender, text):
            return  # 飞书重推的重复事件
        self._async(self.feishu.reply_text, message_id, "收到，处理中…")
        self.wake.set()

    def _save_attachment(self, message_id: str, chat_id: str, sender: str, msg_type: str, content: str) -> None:
        """文件收件箱：下载附件存进 raw/inbox/<日期>/（只追加），再排一个任务让 Agent 看看是什么。"""
        label = ATTACH_LABEL[msg_type]
        try:
            data = json.loads(content)
            key = data.get("image_key") if msg_type == "image" else data.get("file_key")
            if not key:
                raise ValueError("消息里没有附件 key")
            blob, name = self.feishu.download(message_id, key, ATTACH_TYPES[msg_type])
            name = safe_filename(data.get("file_name") or name or f"{label}.{'png' if msg_type == 'image' else 'bin'}")
            now = time.localtime()
            rel = Path("spaces/me/raw/inbox") / time.strftime("%Y-%m-%d", now) / f"{time.strftime('%H%M%S', now)}-{name}"
            path = Path(self.cfg.runtime_dir) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "xb") as f:  # x：已存在就报错，原文不覆盖
                f.write(blob)
        except Exception as e:  # noqa: BLE001 — 失败要告诉用户
            log.exception("save attachment %s failed", message_id)
            self.feishu.reply_text(message_id, f"⚠️ {label}没存下来：{e}")
            return
        prompt = (f"用户发来一个{label}，已存进文件收件箱：{rel.as_posix()}（{len(blob)} 字节）。\n"
                  "读一下（图片、PDF 直接用 Read 看），用两三句话告诉用户这是什么、要点是什么，再问他要怎么处理。")
        if self.store.enqueue(message_id, chat_id, sender, prompt):
            self.feishu.reply_text(message_id, f"已存进收件箱：{rel.name}，正在看…")
            self.wake.set()

    def _send_outbox(self, chat_id: str, task_id: int) -> None:
        """文件回传：Agent 放进 outbox/<任务号>/ 的文件，逐个发到飞书。"""
        out = Path(self.cfg.runtime_dir) / "spaces/me/outbox" / str(task_id)
        if not out.is_dir():
            return
        for path in sorted(p for p in out.iterdir() if p.is_file() and not p.name.startswith(".")):
            if path.stat().st_size > MAX_UPLOAD_BYTES:
                self.feishu.send_text(chat_id, f"⚠️ {path.name} 超过 30MB，没法发到飞书，文件在 {path}")
            elif not self.feishu.send_file(chat_id, path):
                self.feishu.send_text(chat_id, f"⚠️ {path.name} 发送失败，文件在 {path}")

    def _write_docs(self, chat_id: str, task_id: int) -> None:
        """写飞书文档：Agent 放进 outbox/feishu-docs/<任务号>/ 的请求，以你本人身份建文档或补内容。"""
        d = Path(self.cfg.runtime_dir) / "spaces/me/outbox/feishu-docs" / str(task_id)
        if not d.is_dir():
            return
        import feishu_write  # 用到才导入：没开写文档也不影响桥接服务
        for path in sorted(d.glob("*.json")):
            try:
                with self.doc_lock:
                    text = feishu_write.process(path, Path(self.cfg.runtime_dir),
                                                STATE_DIR / "feishu_docs_written.json", self.cfg.doc_folder)
            except Exception as e:  # noqa: BLE001 — 失败要告诉你，不能静默
                log.exception("write doc %s failed", path)
                hint = ""
                if "99991679" in str(e) or "99991672" in str(e) or "1770032" in str(e):
                    hint = "（像是缺写文档的权限：开放平台开通 docx:document、docx:document.block:convert、docs:document.media:upload 后重新运行 ops/feishu_auth.py）"
                text = f"⚠️ 飞书文档没写成（{path.name}）：{e}{hint}"
            self.feishu.send_text(chat_id, text)

    # ---------- 两步确认：草稿 → 确认卡片 → 你点「发送」才发 ----------

    def pending_dir(self) -> Path:
        return Path(self.cfg.runtime_dir) / "spaces/me/outbox/pending"

    def scan_drafts(self, settle_sec: float = 3.0) -> None:
        """看一眼 outbox/pending/ 有没有新草稿；新的就推一张确认卡片给你。"""
        d = self.pending_dir()
        if not d.is_dir():
            return
        for path in sorted(d.glob("*.json")):
            try:
                if time.time() - path.stat().st_mtime < settle_sec:
                    continue  # Agent 可能还在写，等它写完
                key = outbox.draft_key(path)
            except OSError:
                continue
            if self.store.draft_status(key) is not None:
                continue
            try:
                draft = outbox.load_draft(path, Path(self.cfg.runtime_dir))
            except outbox.DraftError as e:
                self.store.add_draft(key, path.name, "invalid", str(e))
                self.feishu.send_to_user(self.cfg.owner_open_id, "text", json.dumps(
                    {"text": f"⚠️ 外发草稿 {path.name} 有问题，没发卡片：{e}"}, ensure_ascii=False))
                continue
            self.store.add_draft(key, path.name, "pending")
            card = outbox.confirm_card(key, draft)
            if not self.feishu.send_to_user(self.cfg.owner_open_id, "interactive", json.dumps(card, ensure_ascii=False)):
                self.store.set_draft(key, "failed", "确认卡片发送失败")

    def scan_drafts_forever(self, interval: float = 5.0) -> None:
        while True:
            try:
                self.scan_drafts()
            except Exception:  # noqa: BLE001 — 扫描出错不能让线程死掉
                log.exception("scan drafts failed")
            time.sleep(interval)

    def on_card_action(self, operator: str, value: dict) -> dict:
        """卡片按钮回调（3 秒内必须返回）：只认你本人；发送放到后台做。返回飞书要的 toast/card。"""
        if operator != self.cfg.owner_open_id:
            log.warning("ignored card action from non-owner %s", operator)
            return {"toast": {"type": "error", "content": "只有本人能操作"}}
        key, act = str(value.get("draft", "")), value.get("act")
        status = self.store.draft_status(key)
        if status != "pending":
            return {"toast": {"type": "info", "content": f"这封已经处理过了（{status or '找不到'}）"}}
        if act == "cancel":
            self.store.set_draft(key, "cancelled", expect="pending")
            return {"toast": {"type": "info", "content": "好，先不发"},
                    "card": {"type": "raw", "data": outbox.decided_card(None, "已取消，没有发送")}}
        if act != "send" or not self.store.set_draft(key, "sending", expect="pending"):
            return {"toast": {"type": "info", "content": "这封已经在处理了"}}
        self._async(self._send_draft, key)
        return {"toast": {"type": "success", "content": "正在发送…"},
                "card": {"type": "raw", "data": outbox.decided_card(None, "发送中…", "blue")}}

    def _send_draft(self, key: str) -> None:
        name = key.split(":", 1)[0]
        path = self.pending_dir() / name

        def tell(text: str):
            self.feishu.send_to_user(self.cfg.owner_open_id, "text", json.dumps({"text": text}, ensure_ascii=False))
        try:
            if not path.is_file() or outbox.draft_key(path) != key:
                self.store.set_draft(key, "failed", "确认后草稿被改过或删掉了")
                tell(f"⚠️ {name} 在你确认之后被改过或删掉了，没有发送。如果是改过，会重新弹一张卡片。")
                return
            if not (self.cfg.mail_user and self.cfg.mail_password and self.cfg.smtp_host):
                raise RuntimeError("~/.agentos/mail.env 缺 MAIL_USER / MAIL_PASSWORD / 发信服务器")
            draft = outbox.load_draft(path, Path(self.cfg.runtime_dir))
            self.smtp_send(draft)
        except Exception as e:  # noqa: BLE001 — 失败要告诉你，不能静默
            log.exception("send draft %s failed", name)
            self.store.set_draft(key, "failed", repr(e))
            tell(f"⚠️ 邮件没发出去：{e}")
            return
        self.store.set_draft(key, "sent")
        log.info("draft %s sent to %s", name, draft["to"])
        tell(f"✅ 邮件已发出：《{draft['subject']}》→ {', '.join(draft['to'] + draft['cc'])}")

    def smtp_send(self, draft: dict) -> None:
        outbox.send_mail(draft, self.cfg.mail_user, self.cfg.mail_password, self.cfg.smtp_host, self.cfg.smtp_port)

    def stop_chat(self, chat_id: str) -> str:
        # 先清排队：否则当前任务一结束，下一条排队消息马上开跑，「停止」等于没停
        cancelled = self.store.cancel_queued(chat_id)
        with self.busy_lock:
            task_id = self.running.get(chat_id)
            if task_id is not None:
                self.stopped.add(task_id)
            proc = self.procs.get(chat_id)  # 进程还没起来时为空，on_start 会补上中止
        if proc:
            self._async(stop_process, proc)
        parts = ["已中止当前任务。" if task_id is not None else "现在没有在跑的任务。"]
        if cancelled:
            parts.append(f"清掉了 {cancelled} 条排队消息。")
        return "".join(parts)

    def status_text(self, chat_id: str) -> str:
        with self.busy_lock:
            running = chat_id in self.running
        queued = self.store.count(chat_id, "queued")
        session = self.store.get_session(chat_id)
        return "\n".join([
            f"正在干活：{'是' if running else '否'}",
            f"排队：{queued} 条",
            f"上下文：{'接着上次的对话' if session else '新对话'}",
        ])

    @staticmethod
    def _async(fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    def notify_interrupted(self) -> None:
        for row in self.store.recover_interrupted():
            self.feishu.send_text(row["chat_id"], f"桥接服务重启，这条任务中断了，需要的话请重发：\n{row['text'][:200]}")

    def dispatch_forever(self) -> None:
        while True:
            self.wake.wait(2)
            self.wake.clear()
            while True:
                with self.busy_lock:
                    if len(self.busy) >= self.cfg.max_concurrency:
                        break
                    task = self.store.claim_next(self.busy)
                    if task is None:
                        break
                    self.busy.add(task["chat_id"])
                    self.running[task["chat_id"]] = task["id"]
                threading.Thread(target=self._run_task, args=(task,), daemon=True).start()

    def _run_task(self, task) -> None:
        chat_id = task["chat_id"]
        started = time.time()
        try:
            session_id = self.store.get_session(chat_id)

            def on_stuck(minutes: int, step: str):
                self.feishu.send_text(chat_id, f"还没做完（已 {minutes} 分钟），当前卡在：{step}")

            def on_start(proc):
                with self.busy_lock:
                    self.procs[chat_id] = proc
                    stop_now = task["id"] in self.stopped
                if stop_now:
                    self._async(stop_process, proc)

            prompt = (f"{task['text']}\n\n（系统提示：本任务号 {task['id']}。要给用户发文件，把成品放进 "
                      f"spaces/me/outbox/{task['id']}/，做完会自动发到飞书。要建或补飞书文档，把请求放进 "
                      f"spaces/me/outbox/feishu-docs/{task['id']}/。）")
            res = run_agent(self.cfg, prompt, session_id, on_stuck, on_start)
            if (res.is_error and session_id and task["id"] not in self.stopped
                    and "No conversation found" in (res.stderr + res.text)):
                log.info("session %s gone, starting fresh", session_id)
                self.store.clear_session(chat_id)
                res = run_agent(self.cfg, prompt, None, on_stuck, on_start)
            if res.session_id:
                self.store.set_session(chat_id, res.session_id)
            if task["id"] in self.stopped:
                self.store.finish(task["id"], False, "stopped")  # 「停止」已经回过话了，不再推结果
                log.info("task %s stopped by owner", task["id"])
                return
            self.feishu.send_markdown(chat_id, ("⚠️ " if res.is_error else "") + res.text)
            self._send_outbox(chat_id, task["id"])
            self._write_docs(chat_id, task["id"])
            self.store.finish(task["id"], not res.is_error, res.text[:500] if res.is_error else None)
            log.info("task %s %s in %.0fs", task["id"], "failed" if res.is_error else "done",
                     time.time() - started)
        except Exception as e:  # noqa: BLE001 — 任何异常都要回告用户，不能静默
            log.exception("task %s crashed", task["id"])
            self.store.finish(task["id"], False, repr(e))
            try:
                self.feishu.send_text(chat_id, f"⚠️ 桥接服务出错：{e!r}")
            except Exception:  # noqa: BLE001
                log.exception("failed to report error")
        finally:
            with self.busy_lock:
                self.busy.discard(chat_id)
                self.running.pop(chat_id, None)
                self.procs.pop(chat_id, None)
                self.stopped.discard(task["id"])
            self.wake.set()


def safe_filename(name: str) -> str:
    """去掉路径和怪字符，防止文件名带 ../ 写到别处。"""
    name = Path(name).name.strip().replace("\x00", "")
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name)
    return name[:80] or "附件"


def extract_text(msg_type: str, content: str) -> str | None:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if msg_type == "text":
        return data.get("text", "")
    if msg_type == "post":  # 富文本：拼出所有文字段
        body = data if "content" in data else next(iter(data.values()), {})
        parts = [body.get("title", "")]
        for line in body.get("content", []):
            parts.append("".join(el.get("text", "") for el in line if el.get("tag") in ("text", "a")))
        return "\n".join(p for p in parts if p)
    return None


# ---------- 入口 ----------

def _log_to_file() -> None:
    """任务计划程序用 pythonw 启动（不弹黑窗口），它没有屏幕输出：把输出都写进 logs/bridge.log。"""
    if sys.stdout is not None and sys.stdout.isatty():
        return  # 你在终端里手动运行时，照常打在屏幕上
    logs = STATE_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    f = open(logs / "bridge.log", "a", encoding="utf-8", buffering=1)  # 常驻服务一直写，不关闭
    sys.stdout = sys.stderr = f


def main() -> None:
    _log_to_file()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env(ENV_FILE)
    load_env(STATE_DIR / "mail.env")  # 两步确认后发信用
    cfg = Config.from_env()
    if not cfg.app_id or not cfg.app_secret:
        sys.exit(f"缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请写进 {ENV_FILE}")

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from lark_oapi.event.callback.model.p2_card_action_trigger import (P2CardActionTrigger,
                                                                       P2CardActionTriggerResponse)

    store = Store(STATE_DIR / "bridge.db")
    feishu = Feishu(cfg)
    bridge = Bridge(cfg, store, feishu)
    bridge.notify_interrupted()
    threading.Thread(target=bridge.dispatch_forever, daemon=True).start()
    if cfg.owner_open_id:
        threading.Thread(target=bridge.scan_drafts_forever, daemon=True).start()

    def handle(data: P2ImMessageReceiveV1) -> None:
        ev = data.event
        m = ev.message
        bridge.on_message(m.message_id, m.chat_id, m.chat_type, ev.sender.sender_id.open_id,
                          m.message_type, m.content)

    def handle_card(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        ev = data.event
        return P2CardActionTriggerResponse(bridge.on_card_action(
            ev.operator.open_id if ev.operator else "", (ev.action.value if ev.action else None) or {}))

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(handle) \
        .register_p2_card_action_trigger(handle_card).build()
    log.info("bridge starting, owner=%s, agent=%s", cfg.owner_open_id or "(未绑定)", " ".join(cfg.claude_cmd))
    lark.ws.Client(cfg.app_id, cfg.app_secret, event_handler=handler,
                   log_level=lark.LogLevel.INFO).start()


if __name__ == "__main__":
    main()
