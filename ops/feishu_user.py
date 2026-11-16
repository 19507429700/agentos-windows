"""以你本人身份调用飞书 API：授权凭证的保存、自动续期和 GET 请求封装。

以你自己的账号运行：App Secret 和你的授权凭证都在 ~/.agentos/（权限 700），agentos 读不到。
凭证 ~/.agentos/feishu_user_token.json（600）由 ops/feishu_auth.py 生成。
接口按飞书 OAuth v3 文档：access_token 约 2 小时，refresh_token 约 7 天且只能用一次，
所以续期加文件锁，防止两个进程同时用同一个 refresh_token 把授权弄丢。
只用标准库，兼容 /usr/bin/python3（3.9）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

try:  # 文件锁：Mac、Linux 用 fcntl，Windows 用 msvcrt
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

STATE_DIR = Path(os.environ.get("AGENTOS_STATE_DIR", Path.home() / ".agentos"))
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
API_BASE = "https://open.feishu.cn/open-apis"
REDIRECT_URI = "http://localhost:18080/feishu/callback"
SCOPES = [
    "offline_access",                     # 返回 refresh_token，授权才能长期有效
    "im:message:readonly",
    "im:message.p2p_msg:get_as_user",     # 读私聊
    "im:message.group_msg:get_as_user",   # 读群聊
    "im:chat:readonly",                   # 列出你在哪些群
    "search:message",                     # 搜索消息：找出私聊会话（群列表接口不含私聊）
    "contact:user.base:readonly",         # 把发送人 open_id 换成名字……
    "contact:contact.base:readonly",      # ……「获取用户信息」接口还要求这一项
    "docx:document:readonly",             # 读聊天、邮件里出现的飞书文档正文
    "wiki:wiki:readonly",                 # /wiki/ 链接要先查知识库节点，才知道对应哪篇文档
    "calendar:calendar:readonly",         # 读日程，复盘和会前材料参考
    "minutes:minutes.transcript:export",  # 读聊天里出现的妙记的文字记录
    "docs:document.media:download",       # 下载文档里的图片、多维表格里的附件
    "sheets:spreadsheet:readonly",        # 读电子表格
    "bitable:app:readonly",               # 读多维表格
    "drive:drive:readonly",               # 列出「我的空间」里的全部文件
    "docx:document",                      # 写飞书文档：建新文档、往 AgentOS 建的文档里补内容
    "docx:document.block:convert",        # 写飞书文档：把 Markdown 转成文档块
    "docs:document.media:upload",         # 写飞书文档：把图片传进文档
]
REFRESH_AHEAD_SEC = 300  # 过期前 5 分钟就续期
# 飞书是国内服务，直连；urllib 在 macOS 上默认会走系统代理，绕一圈反而容易超时
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FeishuError(RuntimeError):
    pass


def token_file() -> Path:
    return STATE_DIR / "feishu_user_token.json"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def app_credentials() -> tuple[str, str]:
    load_env(STATE_DIR / "bridge.env")
    app_id, secret = os.environ.get("FEISHU_APP_ID", ""), os.environ.get("FEISHU_APP_SECRET", "")
    if not (app_id and secret):
        raise FeishuError(f"缺少 FEISHU_APP_ID / FEISHU_APP_SECRET（{STATE_DIR / 'bridge.env'}）")
    return app_id, secret


def http_json(method: str, url: str, form: dict | None = None, headers: dict | None = None,
              timeout: int = 30, json_body: dict | None = None) -> dict:
    """发请求，返回 JSON。飞书出错时 HTTP 状态码常是 4xx 但响应体仍是 JSON，一并返回。"""
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    h = {"Accept": "application/json"}
    if form is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if json_body is not None:
        body = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json; charset=utf-8"
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            raise FeishuError(f"HTTP {e.code}：{url}") from e
    except OSError as e:  # 超时、断网、DNS 失败等（URLError 也是 OSError）
        raise FeishuError(f"网络错误（{type(e).__name__}）：{url.split('?')[0]}") from e


def _save(resp: dict, now: float) -> dict:
    tok = {
        "access_token": resp["access_token"],
        "expires_at": now + int(resp.get("expires_in", 0)),
        "refresh_token": resp.get("refresh_token", ""),
        "refresh_expires_at": now + int(resp.get("refresh_token_expires_in", 0)),
        "scope": resp.get("scope", ""),
    }
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return tok


def _check(resp: dict, what: str) -> dict:
    if resp.get("code", 0) != 0 or "access_token" not in resp:
        raise FeishuError(f"{what}失败：{resp.get('error_description') or resp.get('msg') or resp}")
    return resp


def exchange_code(code: str, http=http_json, now: float | None = None) -> dict:
    app_id, secret = app_credentials()
    resp = http("POST", TOKEN_URL, {"grant_type": "authorization_code", "client_id": app_id,
                                    "client_secret": secret, "code": code, "redirect_uri": REDIRECT_URI})
    return _save(_check(resp, "换取授权凭证"), time.time() if now is None else now)


@contextmanager
def _locked(path: Path):
    """独占锁：同一时间只有一个进程能续期（refresh_token 只能用一次）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        else:
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # 锁不上会自己重试 10 秒，再不行抛错
                    break
                except OSError:
                    time.sleep(1)
        try:
            yield
        finally:
            if not fcntl:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def access_token(http=http_json, now: float | None = None) -> str:
    """返回可用的 user_access_token，快过期时自动续期。"""
    now = time.time() if now is None else now
    with _locked(token_file().with_suffix(".lock")):
        try:
            tok = json.loads(token_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise FeishuError("还没有以你本人身份授权，先运行 ops/feishu_auth.py") from None
        if now < tok["expires_at"] - REFRESH_AHEAD_SEC:
            return tok["access_token"]
        if not tok.get("refresh_token") or now >= tok.get("refresh_expires_at", 0):
            raise FeishuError("飞书授权已过期，重新运行 ops/feishu_auth.py")
        app_id, secret = app_credentials()
        resp = http("POST", TOKEN_URL, {"grant_type": "refresh_token", "client_id": app_id,
                                        "client_secret": secret, "refresh_token": tok["refresh_token"]})
        return _save(_check(resp, "续期授权凭证"), now)["access_token"]


def api_get(path: str, params: dict | None = None, http=http_json) -> dict:
    """以你本人身份 GET 一个飞书 API，返回 data；失败抛 FeishuError（带飞书错误码）。"""
    return _call("GET", path, params, None, http)


def api_post(path: str, params: dict | None, body: dict, http=http_json) -> dict:
    """以你本人身份 POST（搜索消息等查询接口；写飞书文档也走这里），返回 data。"""
    return _call("POST", path, params, body, http)


RETRY_CODES = {2200, 99991400}  # 飞书内部错误、限流：等一下重试一次就好
RETRY_DELAY_SEC = 2


def _call(method: str, path: str, params: dict | None, body: dict | None, http) -> dict:
    url = API_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in (1, 2):
        try:
            headers = {"Authorization": f"Bearer {access_token(http)}"}
            resp = http(method, url, headers=headers, **({"json_body": body} if body is not None else {}))
        except FeishuError as e:  # 网络错误
            if attempt == 1 and str(e).startswith("网络错误"):
                time.sleep(RETRY_DELAY_SEC)
                continue
            raise
        if resp.get("code") == 0:
            return resp.get("data") or {}
        if attempt == 1 and resp.get("code") in RETRY_CODES:
            time.sleep(RETRY_DELAY_SEC)
            continue
        raise FeishuError(f"{resp.get('code')} {resp.get('msg')}")
    raise AssertionError("unreachable")


def http_text(url: str, headers: dict, timeout: int = 60) -> str:
    """GET 一个返回文件（不是 JSON）的接口，如妙记文字记录。飞书出错时返回 JSON，转成 FeishuError。"""
    req = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            body, ctype = r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body, ctype = e.read(), "application/json"
    except OSError as e:
        raise FeishuError(f"网络错误（{type(e).__name__}）：{url.split('?')[0]}") from e
    if "json" in ctype:
        try:
            resp = json.loads(body)
        except ValueError:
            raise FeishuError(f"响应不是 JSON：{url.split('?')[0]}") from None
        if resp.get("code") != 0:
            raise FeishuError(f"{resp.get('code')} {resp.get('msg')}")
    return body.decode("utf-8", errors="replace")


def api_get_text(path: str, params: dict | None = None, http=http_json, fetch=http_text) -> str:
    """以你本人身份下载一个文本文件；网络错误、内部错误、限流时重试一次。"""
    url = API_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in (1, 2):
        try:
            return fetch(url, {"Authorization": f"Bearer {access_token(http)}"})
        except FeishuError as e:
            retry = str(e).startswith("网络错误") or str(e).split(" ")[0] in {str(c) for c in RETRY_CODES}
            if attempt == 1 and retry:
                time.sleep(RETRY_DELAY_SEC)
                continue
            raise
    raise AssertionError("unreachable")


def http_bytes(url: str, headers: dict, timeout: int = 120) -> tuple[bytes, str]:
    """GET 一个二进制文件（图片、附件），返回 (内容, Content-Type)。飞书出错时返回 JSON，转成 FeishuError。"""
    req = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            body, ctype = r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body, ctype = e.read(), "application/json"
    except OSError as e:
        raise FeishuError(f"网络错误（{type(e).__name__}）：{url.split('?')[0]}") from e
    if "json" in ctype:
        try:
            resp = json.loads(body)
        except ValueError:
            raise FeishuError(f"响应不是 JSON：{url.split('?')[0]}") from None
        raise FeishuError(f"{resp.get('code')} {resp.get('msg')}")
    return body, ctype


def api_get_bytes(path: str, params: dict | None = None, http=http_json, fetch=http_bytes) -> tuple[bytes, str]:
    """以你本人身份下载一个二进制文件；path 也可以是完整地址（多维表格附件自带下载地址）。
    网络错误、内部错误、限流时重试一次。"""
    url = path if path.startswith("http") else API_BASE + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    for attempt in (1, 2):
        try:
            return fetch(url, {"Authorization": f"Bearer {access_token(http)}"})
        except FeishuError as e:
            retry = str(e).startswith("网络错误") or str(e).split(" ")[0] in {str(c) for c in RETRY_CODES}
            if attempt == 1 and retry:
                time.sleep(RETRY_DELAY_SEC)
                continue
            raise
    raise AssertionError("unreachable")


def paginate(path: str, params: dict, http=http_json, limit: int | None = None):
    """按 page_token 翻页，逐条产出 items；limit 为最多取多少条。"""
    params, n = dict(params), 0
    while True:
        data = api_get(path, params, http)
        for item in data.get("items") or []:
            yield item
            n += 1
            if limit is not None and n >= limit:
                return
        if not data.get("has_more") or not data.get("page_token"):
            return
        params["page_token"] = data["page_token"]
