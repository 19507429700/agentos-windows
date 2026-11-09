#!/usr/bin/env python3
"""给你本人推一条通知：飞书（agentos 机器人私聊）+ 微信（PushPlus）。复盘和告警都走这里。

以你自己的账号运行（凭据在 ~/.agentos/bridge.env，agentos 读不到）：
  echo "正文" | python3 ops/notify.py "标题"
  python3 ops/notify.py "标题" "正文"
  python3 ops/notify.py --test          # 发一条测试消息

配置（~/.agentos/bridge.env）：FEISHU_APP_ID / FEISHU_APP_SECRET / OWNER_OPEN_ID（D1 已有），
PUSHPLUS_TOKEN（没填就只推飞书）。任一渠道发出即退出码 0；全部失败退出码 1。
只用标准库，兼容 /usr/bin/python3（3.9）。国内服务，直连不走系统代理。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path(os.environ.get("AGENTOS_STATE_DIR", Path.home() / ".agentos"))
FEISHU = "https://open.feishu.cn/open-apis"
PUSHPLUS_URL = "https://www.pushplus.plus/send"
MAX_CHARS = 4000  # 飞书文本和 PushPlus 都够用；太长截断
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def post_json(url: str, body: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})})
    try:
        with _OPENER.open(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"code": e.code, "msg": f"HTTP {e.code}"}
    except (OSError, ValueError) as e:
        return {"code": -1, "msg": f"{type(e).__name__}: {e}"}


def send_feishu(title: str, body: str, post=post_json) -> str | None:
    """以 agentos 机器人身份私聊你。成功返回 None，失败返回原因。"""
    app_id, secret, owner = (os.environ.get(k, "") for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "OWNER_OPEN_ID"))
    if not (app_id and secret and owner):
        return "bridge.env 缺 FEISHU_APP_ID / FEISHU_APP_SECRET / OWNER_OPEN_ID"
    tok = post(f"{FEISHU}/auth/v3/tenant_access_token/internal", {"app_id": app_id, "app_secret": secret})
    if tok.get("code") != 0:
        return f"飞书取令牌失败：{tok.get('msg')}"
    text = f"{title}\n{body}".strip() if body else title
    resp = post(f"{FEISHU}/im/v1/messages?receive_id_type=open_id",
                {"receive_id": owner, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
                {"Authorization": f"Bearer {tok['tenant_access_token']}"})
    return None if resp.get("code") == 0 else f"飞书发送失败：{resp.get('code')} {resp.get('msg')}"


def send_wechat(title: str, body: str, post=post_json) -> str | None:
    """PushPlus 推到微信。code 200 只代表 PushPlus 收到了，实际推送是异步的。"""
    token = os.environ.get("PUSHPLUS_TOKEN", "")
    if not token:
        return "没有配置 PUSHPLUS_TOKEN"
    resp = post(PUSHPLUS_URL, {"token": token, "title": title[:100], "content": body or title, "template": "txt"})
    return None if resp.get("code") == 200 else f"PushPlus 失败：{resp.get('code')} {resp.get('msg')}"


def notify(title: str, body: str = "", post=post_json) -> dict:
    """推两个渠道，返回 {渠道: None 或失败原因}。"""
    body = body if len(body) <= MAX_CHARS else body[:MAX_CHARS] + "\n…（过长已截断）"
    return {"飞书": send_feishu(title, body, post), "微信": send_wechat(title, body, post)}


def main(argv: list[str]) -> int:
    load_env(STATE_DIR / "bridge.env")
    if argv[:1] == ["--test"]:
        title, body = "AgentOS 测试通知", "收到这条说明推送通了。告警和每晚复盘都会从这里发给你。"
    elif argv:
        title = argv[0]
        body = argv[1] if len(argv) > 1 else ("" if sys.stdin.isatty() else sys.stdin.read())
    else:
        print(__doc__)
        return 2
    results = notify(title, body.strip())
    for ch, err in results.items():
        print(f"{ch}：{'✅ 已发出' if err is None else '❌ ' + err}")
    return 0 if any(err is None for err in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
