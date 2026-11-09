#!/usr/bin/env python3
"""以你本人身份授权飞书（一次性）。以你自己的账号运行：

  python3 ops/feishu_auth.py

浏览器打开飞书授权页 → 你点同意 → 飞书跳回 http://localhost:18080/feishu/callback →
本脚本换成凭证，存进 ~/.agentos/feishu_user_token.json（600），然后退出、关闭端口。
之后凭证自动续期；超过约 7 天没用、或在开放平台改了权限，再运行一次即可。
"""

from __future__ import annotations

import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feishu_user as fu  # noqa: E402

PORT = 18080
WAIT_SEC = 300  # 授权码 5 分钟内有效

PAGE = """<!doctype html><meta charset="utf-8"><title>AgentOS 飞书授权</title>
<body style="font-family:-apple-system,sans-serif;text-align:center;padding-top:80px">
<h2>{title}</h2><p>{body}</p></body>"""


def authorize_url(app_id: str, state: str) -> str:
    return fu.AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": app_id, "redirect_uri": fu.REDIRECT_URI,
        "scope": " ".join(fu.SCOPES), "state": state})


class Callback:
    """收飞书跳回来的请求：校验 state，拿到授权码或错误后通知主线程。"""

    def __init__(self, state: str):
        self.state, self.code, self.error = state, None, None
        self.done = threading.Event()

    def handle(self, path: str) -> tuple[int, str]:
        u = urllib.parse.urlparse(path)
        if u.path != "/feishu/callback":
            return 404, PAGE.format(title="找不到页面", body="")
        q = urllib.parse.parse_qs(u.query)
        if q.get("state", [""])[0] != self.state:
            return 400, PAGE.format(title="授权校验失败", body="state 不匹配，请回到终端重新运行授权脚本。")
        if "code" in q:
            self.code = q["code"][0]
            page = PAGE.format(title="✅ 授权完成", body="可以关掉这个页面，回到终端查看结果。")
        else:
            self.error = q.get("error", ["用户拒绝授权"])[0]
            page = PAGE.format(title="没有完成授权", body=f"原因：{self.error}。可以回到终端重新运行。")
        self.done.set()
        return 200, page


def serve(cb: Callback) -> list[HTTPServer]:
    """localhost 在浏览器里可能先解析成 IPv6，所以 127.0.0.1 和 ::1 都监听；只绑本机，不对外。"""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, page = cb.handle(self.path)
            data = page.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # 不打印访问日志（URL 里有授权码）
            pass

    class V6(HTTPServer):
        address_family = socket.AF_INET6

    servers = [HTTPServer(("127.0.0.1", PORT), Handler)]
    try:
        servers.append(V6(("::1", PORT), Handler))
    except OSError:
        pass
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return servers


def main() -> int:
    try:
        app_id, _ = fu.app_credentials()
    except fu.FeishuError as e:
        print(f"❌ {e}")
        return 2
    cb = Callback(secrets.token_urlsafe(24))
    try:
        servers = serve(cb)
    except OSError as e:
        print(f"❌ 端口 {PORT} 被占用（{e}），关掉占用它的程序后重试")
        return 1
    url = authorize_url(app_id, cb.state)
    print("浏览器将打开飞书授权页，登录后点「授权」。没有自动打开就复制下面的地址：\n")
    print(url + "\n")
    webbrowser.open(url)
    try:
        if not cb.done.wait(WAIT_SEC):
            print("❌ 5 分钟内没有完成授权，请重新运行")
            return 1
    finally:
        for s in servers:
            s.shutdown()
    if cb.error:
        print(f"❌ 没有完成授权：{cb.error}")
        return 1
    try:
        tok = fu.exchange_code(cb.code)
        me = fu.api_get("/authen/v1/user_info")
    except fu.FeishuError as e:
        print(f"❌ {e}")
        return 1
    granted = set(tok["scope"].split())
    print(f"✅ 授权成功：{me.get('name', '')}（{me.get('open_id', '')}）")
    print(f"   凭证已存到 {fu.token_file()}，权限 600")
    missing = [s for s in fu.SCOPES if s not in granted]
    if missing:
        print(f"⚠️ 以下权限没有授予，请到开放平台确认已开通并发布：{' '.join(missing)}")
    if not tok["refresh_token"]:
        print("⚠️ 没拿到续期凭证（offline_access），授权约 2 小时后失效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
