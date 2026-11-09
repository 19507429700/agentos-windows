#!/usr/bin/env python3
"""实测：以你本人身份能读到哪些飞书会话、能往回读多久（D2 第 1 步）。以你自己的账号运行：

  python3 ops/feishu_probe.py

只统计，不保存、不打印任何消息内容。对每个会话输出：最早一条、最近一条的时间，近 7 天条数；
读不了的写明飞书错误码（如 231203 保密群）。看完结果再决定 collect_feishu.py 的采集范围。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feishu_user as fu  # noqa: E402

RECENT_DAYS = 7
COUNT_CAP = 1000  # 近 7 天最多数到这么多条就停


def ts(ms: str) -> str:
    return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")


def first_message(chat_id: str, order: str, http=fu.http_json) -> dict | None:
    data = fu.api_get("/im/v1/messages", {"container_id_type": "chat", "container_id": chat_id,
                                          "sort_type": order, "page_size": 1}, http)
    items = data.get("items") or []
    return items[0] if items else None


def probe_chat(chat: dict, now: float, http=fu.http_json) -> dict:
    cid = chat["chat_id"]
    row = {"name": chat.get("name") or "（无名称）", "chat_id": cid, "external": chat.get("external", False)}
    try:
        oldest = first_message(cid, "ByCreateTimeAsc", http)
        newest = first_message(cid, "ByCreateTimeDesc", http)
        recent = sum(1 for _ in fu.paginate(
            "/im/v1/messages", {"container_id_type": "chat", "container_id": cid,
                                "start_time": str(int(now - RECENT_DAYS * 86400)), "page_size": 50},
            http, limit=COUNT_CAP))
    except fu.FeishuError as e:
        row["error"] = str(e)
        return row
    row.update(oldest=ts(oldest["create_time"]) if oldest else "-",
               newest=ts(newest["create_time"]) if newest else "-",
               recent=f"{recent}+" if recent >= COUNT_CAP else str(recent))
    return row


def main() -> int:
    try:
        me = fu.api_get("/authen/v1/user_info")
        chats = list(fu.paginate("/im/v1/chats", {"page_size": 100}))
    except fu.FeishuError as e:
        print(f"❌ {e}")
        return 1
    print(f"以 {me.get('name', '你')} 的身份，共找到 {len(chats)} 个会话\n")
    now = time.time()
    ok = bad = 0
    print(f"{'会话':<24} {'最早':<11} {'最近':<11} {'近7天':>6}  备注")
    for chat in chats:
        r = probe_chat(chat, now)
        note = "外部群" if r["external"] else ""
        name = r["name"][:22]
        if "error" in r:
            bad += 1
            print(f"{name:<24} {'读不了':<11} {'':<11} {'':>6}  {r['error']} {note}")
        else:
            ok += 1
            print(f"{name:<24} {r['oldest']:<11} {r['newest']:<11} {r['recent']:>6}  {note}")
    print(f"\n能读 {ok} 个，读不了 {bad} 个。消息内容没有保存。")
    if not chats:
        print("一个会话都没列出来：可能是 im:chat:readonly 没以用户身份开通，或还没发布版本。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
