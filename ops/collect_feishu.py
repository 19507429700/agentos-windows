#!/usr/bin/env python3
"""飞书消息采集：以你本人身份增量拉取群聊（和私聊）消息，存成 Markdown 原文。

以你自己的账号运行（授权凭证在 ~/.agentos/，agentos 读不到）：
  python3 ops/collect_feishu.py
先运行过 ops/feishu_auth.py。第一次拉最近 FEISHU_INITIAL_DAYS 天（默认 30）。

范围：你所在的全部群聊；私聊靠「搜索消息」接口找出会话，需要开通「获取消息搜索结果」权限并重新授权，
      没开通时跳过私聊，只采群聊。云文档也读下来：聊天、邮件里出现过链接的，加上「我的云盘」「我的文档库」里的全部；
      文档按块转 Markdown（表格保留、图片下载到同名「-图片」文件夹），电子表格、多维表格转成表格（见 feishu_docs.py）。
      聊天里出现的妙记存文字记录；另存未来 7 天日程。
输出：$AGENTOS_RAW/feishu/<日期>/<会话名>-<会话ID后6位>.md，每条一行「- 时:分 发送人：内容」，只追加。
状态：~/.agentos/feishu_state.json（每个会话采到哪条、已知私聊、发送人名字缓存）

底线：只读不发；原文只追加不修改；拉取失败退出码非 0（供告警）。只用标准库，兼容 /usr/bin/python3（3.9）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feishu_docs as fd  # noqa: E402
import feishu_user as fu  # noqa: E402

log = logging.getLogger("collect_feishu")

SEARCH_PAGE = 30  # 搜索接口单页上限


# ---------- 状态 ----------

def state_path() -> Path:
    return fu.STATE_DIR / "feishu_state.json"


def load_state() -> dict:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    for k in ("chats", "p2p", "names"):
        st.setdefault(k, {})
    return st


def save_state(st: dict) -> None:
    path = state_path()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# ---------- 消息内容 ----------

def message_text(item: dict, names: dict) -> str | None:
    """把一条消息转成一行可读文字；已删除的返回 None。"""
    if item.get("deleted"):
        return None
    mtype = item.get("msg_type", "")
    try:
        c = json.loads((item.get("body") or {}).get("content") or "{}")
    except ValueError:
        c = {}
    if mtype == "text":
        text = c.get("text", "")
        for m in item.get("mentions") or []:  # @_user_1 → @名字
            text = text.replace(m.get("key", "\0"), "@" + (m.get("name") or names.get(m.get("id"), "某人")))
        return text
    if mtype == "post":
        body = c if "content" in c else next(iter(c.values()), {}) if c else {}
        lines = [body.get("title", "")] if body.get("title") else []
        for para in body.get("content") or []:
            parts = []
            for el in para:
                tag = el.get("tag")
                if tag in ("text", "a"):
                    parts.append(el.get("text", ""))
                elif tag == "at":
                    parts.append("@" + (el.get("user_name") or "某人"))
                elif tag == "img":
                    parts.append("[图片]")
            lines.append("".join(parts))
        return "\n".join(lines)
    simple = {"image": "[图片]", "audio": "[语音]", "media": "[视频]", "sticker": "[表情]",
              "merge_forward": "[合并转发]", "share_chat": "[分享群名片]", "share_user": "[分享个人名片]",
              "system": "[系统消息]", "location": "[位置]", "video_chat": "[视频会议]"}
    if mtype == "file":
        return f"[文件] {c.get('file_name', '')}".strip()
    if mtype == "interactive":
        title = c.get("title") or ((c.get("header") or {}).get("title") or {}).get("content", "")
        return f"[卡片] {title}".strip()
    return simple.get(mtype, f"[{mtype}]")


def sender_name(item: dict, names: dict, http=fu.http_json) -> str:
    s = item.get("sender") or {}
    sid = s.get("id", "")
    if s.get("sender_type") != "user":
        if s.get("sender_type") == "app":
            return "agentos" if sid == os.environ.get("FEISHU_APP_ID") else "机器人"
        return s.get("sender_type") or "系统"
    if sid not in names:
        try:
            user = fu.api_get(f"/contact/v3/users/{sid}", {"user_id_type": "open_id"}, http).get("user") or {}
            names[sid] = user.get("name") or sid
        except fu.FeishuError:
            names[sid] = sid  # 外部联系人等查不到名字，就用 ID，下次不再重查
    return names[sid]


def safe_name(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_")[:40] or "未命名"


# ---------- 采集 ----------

def collect_chat(chat_id: str, title: str, kind: str, st: dict, raw_dir: Path,
                 initial_days: int, http=fu.http_json, now: float | None = None, my_id: str = "") -> int:
    """拉一个会话的新消息，按日期追加到原文文件，返回新存条数。st 就地更新。
    私聊没有群名：title 传空时，用这批消息里不是你（my_id）的那位发送人命名。"""
    now = time.time() if now is None else now
    cs = st["chats"].setdefault(chat_id, {})
    last_ms, last_ids = cs.get("last_ms", 0), set(cs.get("last_ids", []))
    start = last_ms // 1000 if last_ms else int(now - initial_days * 86400)
    by_day: dict[str, list[str]] = {}
    newest_ms, newest_ids, n = last_ms, set(last_ids), 0
    other = ""
    for item in fu.paginate("/im/v1/messages", {"container_id_type": "chat", "container_id": chat_id,
                                                "start_time": str(start), "sort_type": "ByCreateTimeAsc",
                                                "page_size": 50}, http):
        ms, mid = int(item.get("create_time", 0)), item.get("message_id", "")
        if ms < last_ms or (ms == last_ms and mid in last_ids):
            continue  # 上次已经存过
        if ms > newest_ms:
            newest_ms, newest_ids = ms, set()
        newest_ids.add(mid)
        text = message_text(item, st["names"])
        if text is None:
            continue
        dt = datetime.fromtimestamp(ms / 1000)
        first, *rest = (text or "（空）").split("\n")
        who = sender_name(item, st["names"], http)
        if not other and (item.get("sender") or {}).get("id") not in ("", my_id):
            other = who
        line = f"- {dt:%H:%M} {who}：{first}"
        by_day.setdefault(dt.strftime("%Y-%m-%d"), []).append("\n".join([line] + ["  " + r for r in rest]))
        n += 1
    if not title:
        title = f"私聊-{other}" if other else "私聊"
        if other:
            st["p2p"][chat_id] = title
    for day, lines in by_day.items():
        path = raw_dir / "feishu" / day / f"{safe_name(title)}-{chat_id[-6:]}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        with open(path, "a", encoding="utf-8") as f:  # 原文只追加
            if new_file:
                f.write(f"# {title}（{day}）\n\n- 会话: {chat_id}（{kind}）\n\n")
            f.write("\n".join(lines) + "\n")
    cs.update(last_ms=newest_ms, last_ids=sorted(newest_ids), title=title)
    return n


def discover_p2p(st: dict, initial_days: int, http=fu.http_json, now: float | None = None) -> None:
    """用「搜索消息」找出新出现的私聊会话 ID，记进 st["p2p"]。"""
    now = time.time() if now is None else now
    since = st.get("p2p_search_since") or (datetime.fromtimestamp(now) - timedelta(days=initial_days)) \
        .astimezone().isoformat(timespec="seconds")
    params = {"page_size": SEARCH_PAGE}
    while True:
        data = fu.api_post("/im/v1/messages/search", params,
                           {"query": "", "filter": {"chat_type": "p2p", "time_range": {"start_time": since}}}, http)
        for item in data.get("items") or []:
            meta = item.get("meta_data") or {}
            if meta.get("chat_id"):
                st["p2p"].setdefault(meta["chat_id"], "")
        if not data.get("has_more") or not data.get("page_token"):
            break
        params["page_token"] = data["page_token"]
    st["p2p_search_since"] = datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raw_dir = Path(os.environ.get("AGENTOS_RAW", "") or Path(__file__).resolve().parent.parent / "spaces" / "me" / "raw")
    initial_days = int(os.environ.get("FEISHU_INITIAL_DAYS", "30"))
    st = load_state()
    try:
        me = fu.api_get("/authen/v1/user_info")
        groups = list(fu.paginate("/im/v1/chats", {"page_size": 100}))
    except fu.FeishuError as e:
        log.error("飞书接口不可用：%s", e)
        return 1
    failed = False
    try:
        discover_p2p(st, initial_days)
    except fu.FeishuError as e:
        log.info("私聊跳过（需开通「获取消息搜索结果」并重新授权）：%s", e)
    targets = [(g["chat_id"], g.get("name") or "未命名群", "群聊") for g in groups]
    targets += [(cid, name, "私聊") for cid, name in st["p2p"].items()]
    for chat_id, title, kind in targets:
        try:
            n = collect_chat(chat_id, title, kind, st, raw_dir, initial_days, my_id=me.get("open_id", ""))
            log.info("%s「%s」：新存 %d 条", kind, title or st["p2p"].get(chat_id) or "私聊", n)
        except fu.FeishuError as e:
            failed = True
            log.error("%s「%s」采集失败：%s", kind, title, e)
        finally:
            save_state(st)
    for name, step in (("文档", lambda: collect_docs(st, raw_dir)), ("妙记", lambda: collect_minutes(st, raw_dir)),
                       ("日程", lambda: collect_calendar(raw_dir))):
        try:
            n = step()
            log.info("%s：%s", name, n)
        except fu.FeishuError as e:
            failed = True
            log.error("%s采集失败：%s", name, e)
        finally:
            save_state(st)
    return 1 if failed else 0


# ---------- 云文档：聊天和邮件里出现过链接的，加上我的云盘、我的文档库里的全部 ----------

DOC_LINK_RE = re.compile(r"https?://[\w.-]*(?:feishu\.cn|larksuite\.com)/(wiki|docx|sheets|base)/([A-Za-z0-9]{20,})")
LINK_KIND = {"wiki": "wiki", "docx": "docx", "sheets": "sheet", "base": "bitable"}
DOC_RECHECK_SEC = 3600       # 已存过的每小时看一次有没有新版本
DOC_FAIL_RETRY_SEC = 86400   # 读不了的（没权限、类型不支持）一天后再试
DOC_LIST_EVERY_SEC = 3600    # 每小时列一次我的云盘和文档库
DOC_TIME_BUDGET_SEC = 240    # 每轮最多花 4 分钟读文档，剩下的下一轮接着读（整个采集任务 8 分钟超时）
DOC_FMT = 2                  # 存法的版本：2 = 按块读、表格保留、图片下载。旧版存的会按新存法重读一次
SAVED_PREFIXES = ("文档-", "妙记-", "表格-", "多维表格-")
KIND_PREFIX = {"docx": "文档", "sheet": "表格", "bitable": "多维表格"}
KIND_NAME = {"docx": "文档", "sheet": "电子表格", "bitable": "多维表格"}


def scan_links(raw_dir: Path, pattern: re.Pattern):
    """扫 raw/feishu、raw/mail 里的链接（跳过已存下的文档、表格、妙记本身），逐个产出 (匹配, 所在文件)。"""
    for sub in ("feishu", "mail"):
        base = raw_dir / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            if p.name.startswith(SAVED_PREFIXES):
                continue
            try:
                text = p.read_text("utf-8", errors="replace")
            except OSError:
                continue
            for m in pattern.findall(text):
                yield m, p


def find_doc_links(raw_dir: Path) -> dict[str, str]:
    """返回聊天、邮件里出现过的云文档链接 {token: wiki|docx|sheet|bitable}。"""
    links: dict[str, str] = {}
    for (kind, token), _ in scan_links(raw_dir, DOC_LINK_RE):
        links.setdefault(token, LINK_KIND[kind])
    return links


def refresh_my_docs(st: dict, http=fu.http_json, now: float | None = None) -> str:
    """每小时列一次「我的云盘」「我的文档库」，结果存在 st["my_docs"]。返回出错说明（没有就是空串）。"""
    now = time.time() if now is None else now
    if now - st.get("my_docs_at", 0) < DOC_LIST_EVERY_SEC:
        return ""
    old = st.get("my_docs") or {}
    new, errs = {}, []
    for where, lister in (("云盘", fd.list_drive), ("文档库", fd.list_my_library)):
        try:
            for it in lister(http):
                new[it["token"]] = {k: it.get(k, "") for k in ("kind", "title", "obj_type", "obj_token", "where")}
        except fu.FeishuError as e:
            errs.append(f"{where}：{e}")
            new.update({t: v for t, v in old.items() if v.get("where") == where})  # 这次没列成，沿用上次的
    st["my_docs"], st["my_docs_at"] = new, now
    return "；".join(errs)


class ImageSaver:
    """把文档里的图片下载到 <文件名>-图片/ 下，返回放在原位置的 ![图N](路径)。路径从项目文件夹根目录算起。"""

    def __init__(self, img_dir: Path, raw_dir: Path, http, fetch):
        self.img_dir, self.http, self.fetch = img_dir, http, fetch
        self.rel = "spaces/me/raw/" + img_dir.relative_to(raw_dir).as_posix()
        self.n = self.saved = self.failed = 0

    def __call__(self, token_or_url: str, label: str = "图") -> str:
        self.n += 1
        i = self.n
        if i > fd.MAX_IMAGES:
            return f"[{label}{i}：图片太多，没有下载]"
        try:
            data, ctype = fd.media_download(token_or_url, self.http, self.fetch)
        except fu.FeishuError as e:
            self.failed += 1
            return f"[{label}{i}：没下载下来（{str(e)[:80]}）]"
        if len(data) > fd.MAX_IMAGE_BYTES:
            return f"[{label}{i}：图片超过 20MB，没有保存]"
        self.img_dir.mkdir(parents=True, exist_ok=True)
        name = f"{i:02d}{fd.sniff_image_ext(data, ctype) or '.bin'}"
        (self.img_dir / name).write_bytes(data)
        self.saved += 1
        return f"![{label}{i}]({self.rel}/{name})"

    def attachment(self, a: dict) -> str:
        """多维表格的附件：图片下载，其他文件只记名字。"""
        name = a.get("name") or ""
        if (a.get("type") or "").lower().startswith("image/") or name.lower().endswith(fd.IMAGE_EXTS):
            return self(a.get("url") or a.get("file_token", ""))
        return f"[附件] {name}".strip()


def _granted_scope() -> str:
    """当前授权里开了哪些权限（重新授权后会变）。"""
    try:
        return " ".join(sorted(json.loads(fu.token_file().read_text(encoding="utf-8")).get("scope", "").split()))
    except (OSError, ValueError):
        return ""


def _modified_time(token: str, doc_type: str, http) -> str | None:
    """电子表格没有版本号，用「最后修改时间」判断改没改过；查不到返回 None。"""
    try:
        metas = fu.api_post("/drive/v1/metas/batch_query", None,
                            {"request_docs": [{"doc_token": token, "doc_type": doc_type}]}, http).get("metas") or []
    except fu.FeishuError:
        return None
    return str(metas[0].get("latest_modify_time")) if metas and metas[0].get("latest_modify_time") else None


def _out_stem(raw_dir: Path, day: str, prefix: str, title: str, token: str, now: float) -> str:
    """文件名主干；同一天又存一版就加时间，原文不覆盖。"""
    base = f"{prefix}-{safe_name(title)}-{token[-6:]}"
    folder = raw_dir / "feishu" / day
    for stem in (base, f"{base}-{datetime.fromtimestamp(now):%H%M}", f"{base}-{datetime.fromtimestamp(now):%H%M%S}"):
        if not (folder / f"{stem}.md").exists():
            return stem
    return f"{base}-{int(now * 1000)}"


def _read_one(kind: str, obj_token: str, token: str, title: str, d: dict, st: dict, raw_dir: Path,
              now: float, http, fetch) -> int | None:
    """读一份云文档并存下来，返回下载的图片数；没改过返回 None。读不了抛 FeishuError。"""
    if kind == "docx":
        doc = fu.api_get(f"/docx/v1/documents/{obj_token}", None, http).get("document") or {}
        rev, title = doc.get("revision_id"), doc.get("title") or title or "未命名文档"
    elif kind == "bitable":
        app = fu.api_get(f"/bitable/v1/apps/{obj_token}", None, http).get("app") or {}
        rev, title = app.get("revision"), app.get("name") or title or "未命名多维表格"
    else:  # sheet
        rev = _modified_time(obj_token, "sheet", http) or datetime.fromtimestamp(now).strftime("日-%Y-%m-%d")
    d.pop("error", None)
    if title:
        d["title"] = title
    if rev is not None and str(rev) == str(d.get("rev")) and d.get("fmt") == DOC_FMT:
        return None  # 没改过
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    if kind == "sheet":
        title, body = fd.read_sheet(obj_token, http)
        d["title"] = title
    stem = _out_stem(raw_dir, day, KIND_PREFIX[kind], title or "未命名", token, now)
    saver = ImageSaver(raw_dir / "feishu" / day / f"{stem}-图片", raw_dir, http, fetch)
    if kind == "docx":
        body, embedded = fd.read_docx(obj_token, saver, http)
        for ekind, etok in embedded:  # 文档里内嵌的表格，下一轮单独读
            if etok:
                st.setdefault("doc_embedded", {})[etok] = ekind
    elif kind == "bitable":
        _, _, body = fd.read_bitable(obj_token, saver.attachment, http)
    path = raw_dir / "feishu" / day / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    pics = f"；图片 {saver.saved} 张，在同名「-图片」文件夹里" if saver.saved else ""
    path.write_text(f"# {title}\n\n- 飞书{KIND_NAME[kind]}：{token}（版本 {rev}，"
                    f"{datetime.fromtimestamp(now):%Y-%m-%d %H:%M} 读取{pics}）\n\n---\n\n{body}\n", "utf-8")
    d.update(rev=rev, fmt=DOC_FMT, saved=path.relative_to(raw_dir).as_posix(), img_failed=saver.failed)
    return saver.saved


def collect_docs(st: dict, raw_dir: Path, http=fu.http_json, now: float | None = None, fetch=None,
                 budget_sec: float = DOC_TIME_BUDGET_SEC) -> str:
    """把云文档存成 raw/feishu/<日期>/文档-|表格-|多维表格-<标题>-<ID后6位>.md，图片存在同名「-图片」文件夹。
    来源：聊天和邮件里出现过的链接 + 我的云盘 + 我的文档库 + 文档里内嵌的表格。改过就另存一份新版本。"""
    now = time.time() if now is None else now
    started = time.monotonic()
    docs = st.setdefault("docs", {})
    scope = _granted_scope()
    if scope != st.get("docs_scope"):  # 重新授权、权限变了：读不了的、图片没下载下来的马上重读，云盘和文档库马上重列
        for d in docs.values():
            if d.get("error"):
                d["checked"] = 0
            if d.get("img_failed"):
                d["checked"], d["rev"] = 0, None
        st["my_docs_at"], st["docs_scope"] = 0, scope
    list_err = refresh_my_docs(st, http, now)
    targets: dict[str, dict] = {tok: {"kind": k} for tok, k in find_doc_links(raw_dir).items()}
    for tok, it in (st.get("my_docs") or {}).items():
        targets.setdefault(tok, it)
    for tok, k in (st.get("doc_embedded") or {}).items():
        targets.setdefault(tok, {"kind": k})
    saved = skipped = images = later = 0
    for token, it in targets.items():
        d = docs.setdefault(token, {})
        if d.get("fmt") == DOC_FMT:
            wait = DOC_FAIL_RETRY_SEC if d.get("error") else DOC_RECHECK_SEC
            if now - d.get("checked", 0) < wait:
                continue
        if time.monotonic() - started > budget_sec:
            later += 1
            continue
        d["checked"] = now
        kind, obj_token, title = it.get("kind", ""), token, it.get("title", "")
        try:
            if kind == "wiki":
                if it.get("obj_token"):
                    kind, obj_token = it.get("obj_type", ""), it["obj_token"]
                else:
                    node = fu.api_get("/wiki/v2/spaces/get_node", {"token": token}, http).get("node") or {}
                    kind, obj_token = node.get("obj_type", ""), node.get("obj_token", "")
                    title = node.get("title", "") or title
            if kind not in KIND_PREFIX:
                d.update(title=title, error=f"类型 {kind or '未知'} 暂不支持读取", fmt=DOC_FMT)
                skipped += 1
                continue
            n = _read_one(kind, obj_token, token, title, d, st, raw_dir, now, http, fetch)
        except fu.FeishuError as e:
            d.update(error=str(e)[:200], fmt=DOC_FMT)
            skipped += 1
            continue
        if n is not None:
            saved += 1
            images += n
    msg = f"新存 {saved} 份（图片 {images} 张），读不了 {skipped} 份，共 {len(targets)} 份"
    if later:
        msg += f"，{later} 份留到下一轮"
    if list_err:
        msg += f"；列云文档出错：{list_err}"
    return msg


# ---------- 妙记：聊天和邮件里出现过链接的妙记，存文字记录 ----------

MINUTES_LINK_RE = re.compile(r"https?://[\w.-]*(?:feishu\.cn|larksuite\.com)/minutes/([A-Za-z0-9]{16,})")
MINUTES_NOT_READY = "2091003"  # 还在转写：下一轮（10 分钟后）再试，不等一天


def collect_minutes(st: dict, raw_dir: Path, http=fu.http_json, fetch=fu.http_text, now: float | None = None) -> str:
    """把妙记的文字记录存成 raw/feishu/<日期>/妙记-<ID后6位>.md。存过的不再读；还在转写的下一轮再试，没权限的一天后再试。"""
    now = time.time() if now is None else now
    mins = st.setdefault("minutes", {})
    saved = skipped = 0
    for token, src in scan_links(raw_dir, MINUTES_LINK_RE):
        m = mins.setdefault(token, {})
        wait = 0 if m.get("error", "").startswith(MINUTES_NOT_READY) else DOC_FAIL_RETRY_SEC
        if m.get("saved") or now - m.get("checked", 0) < wait:
            continue
        m["checked"] = now
        try:
            text = fu.api_get_text(f"/minutes/v1/minutes/{token}/transcript",
                                   {"need_speaker": "true", "need_timestamp": "true", "file_format": "txt"},
                                   http, fetch)
        except fu.FeishuError as e:
            m["error"] = str(e)[:200]
            skipped += 1
            continue
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        path = raw_dir / "feishu" / day / f"妙记-{token[-6:]}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# 妙记 {token}\n\n- 文字记录，{datetime.fromtimestamp(now):%Y-%m-%d %H:%M} 读取；"
                        f"链接最早出现在 spaces/me/raw/{src.relative_to(raw_dir).as_posix()}\n\n---\n\n{text.strip()}\n", "utf-8")
        m.pop("error", None)
        m["saved"] = path.relative_to(raw_dir).as_posix()
        saved += 1
    return f"新存 {saved} 篇，读不了 {skipped} 篇，共发现 {len(mins)} 个链接"


# ---------- 日程：未来 7 天 ----------

def collect_calendar(raw_dir: Path, http=fu.http_json, now: float | None = None, days: int = 7) -> str:
    """覆盖写 raw/feishu/日程/最近日程.md（日程是当前状态，不是聊天原文，每次以最新为准）。"""
    now = time.time() if now is None else now
    cals = fu.api_post("/calendar/v4/calendars/primary", None, {}, http).get("calendars") or []
    if not cals:
        return "没有主日历"
    cid = cals[0]["calendar"]["calendar_id"]
    start = int(datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0).timestamp())
    events = [e for e in fu.paginate(f"/calendar/v4/calendars/{cid}/events",
                                     {"start_time": str(start), "end_time": str(start + days * 86400), "page_size": 50},
                                     http) if e.get("status") != "cancelled"]

    def when(t: dict) -> tuple[str, str]:
        if t.get("timestamp"):
            dt = datetime.fromtimestamp(int(t["timestamp"]))
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        return t.get("date", ""), "全天"
    rows = []
    for e in events:
        day, t0 = when(e.get("start_time") or {})
        _, t1 = when(e.get("end_time") or {})
        loc = (e.get("location") or {}).get("name", "")
        rows.append((day, t0, f"- {day} {t0}{'–' + t1 if t1 != '全天' else ''} {e.get('summary') or '（无标题）'}"
                              f"{'（' + loc + '）' if loc else ''}"))
    rows.sort()
    path = raw_dir / "feishu" / "日程" / "最近日程.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(r[2] for r in rows) or "（这 7 天没有日程）"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"# 最近 {days} 天日程（{datetime.fromtimestamp(now):%Y-%m-%d %H:%M} 更新）\n\n{body}\n", "utf-8")
    os.replace(tmp, path)
    return f"{len(rows)} 个"


if __name__ == "__main__":
    sys.exit(main())
