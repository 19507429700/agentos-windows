#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信 4.x 取密钥：一键生成 all_keys.json，供 ops/collect_wechat.py 直接使用。

替代「在 WorkBuddy 里手动跑一次 wechat-cli」这一步。原理（微信 4.1+）：
内存里没有明文密钥，只有 com.Tencent.WCDB.Config.Cipher 对象；本工具定位该对象
→ 沿对象链取 blob → XOR 解码 → HMAC-SHA512 校验，得到每个数据库的 32 字节密钥。

前提（两条，缺一不可）：
  1. 微信 4.x 已安装、已登录、正在运行（Weixin.exe 在跑）
  2. 以管理员身份运行（要读微信进程内存）

用法：
  python .claude/skills/wechat-key/scripts/get_key.py            # 自动找数据目录
  python .claude/skills/wechat-key/scripts/get_key.py <db_storage路径>   # 手动指定

输出：%USERPROFILE%\\.wechat-cli\\all_keys.json（collect_wechat.py 默认读这里）
成功后，ops/collect_wechat.py --check 应能列出会话；此后无需再跑本脚本、无需微信运行。

只读不改、只在本机处理，不上传任何数据。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TOOL = SCRIPT_DIR / "wcdb_key_tool_windows.py"
OUT_DIR = Path.home() / ".wechat-cli"
OUT_KEYS = OUT_DIR / "all_keys.json"


def find_db_root() -> str | None:
    """读 %APPDATA%\\Tencent\\xwechat\\config\\*.ini 定位数据根，再找 xwechat_files\\*\\db_storage。"""
    import os
    appdata = os.environ.get("APPDATA", "")
    cfg = Path(appdata) / "Tencent" / "xwechat" / "config"
    if not cfg.exists():
        return None
    for ini in cfg.glob("*.ini"):
        try:
            first = ini.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0]
        except (OSError, IndexError):
            continue
        base = Path(first.strip())
        xdir = base / "xwechat_files"
        if xdir.exists():
            for acc in xdir.iterdir():
                db = acc / "db_storage"
                if db.exists():
                    return str(db)
    return None


def is_weixin_running() -> bool:
    r = subprocess.run(["tasklist"], capture_output=True, text=True, errors="replace")
    return "Weixin.exe" in r.stdout


def main() -> int:
    if sys.platform != "win32":
        print("[X] 本脚本只支持 Windows 微信 4.x。macOS/Linux 请用对应平台工具。")
        return 2
    if not is_weixin_running():
        print("[X] 未检测到 Weixin.exe。请先启动并登录微信，再重跑本脚本。")
        return 2

    db_root = sys.argv[1] if len(sys.argv) > 1 else find_db_root()
    if not db_root:
        print("[X] 没找到微信数据目录。可手动指定：")
        print("    python .claude/skills/wechat-key/scripts/get_key.py <db_storage路径>")
        return 2
    print(f"[+] 微信数据目录：{db_root}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [sys.executable, str(TOOL), "extract", "--db-dir", db_root, "--output", str(OUT_KEYS)]
    )
    if r.returncode != 0:
        print("[X] 取密钥失败。请确认：微信在运行且已登录、本脚本以管理员身份运行。")
        return r.returncode
    print(f"\n[+] 密钥已写入：{OUT_KEYS}")
    print("    接下来可用  python ops/collect_wechat.py --check  验证是否读到会话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
