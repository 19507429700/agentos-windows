"""接讯飞录音笔（Windows）：在项目文件夹运行 py ops\\install_iflyrec.py

下载讯飞官方的只读客户端（录音笔 Skill），核对指纹后放到 %USERPROFILE%\\.agentos\\iflyrec-skill\\，
再申请授权：给出一个讯飞的链接，你在浏览器里登录、点同意，这里自动等到授权成功。
授权凭据存在 %USERPROFILE%\\.agentos\\iflyrec-auth\\（AI 被禁止读 .agentos）。
授权好后重跑 install.ps1，就会装上每 10 分钟的讯飞录音采集（AgentOS-collect-iflyrec）。

为什么不用官方的 `curl … | bash`：那是给 Mac、Linux 的，而且会把客户端装进「当前 AI」的技能文件夹；
AgentOS 的采集要由任务计划程序定时跑，客户端要放在 AI 不能读的 .agentos 里。这里做的事和官方安装器一样
（取版本信息、下载、逐个核对指纹），只是换了位置。可重复运行：已经授权过的不会要求重新登录。
只用标准库。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import NO_WINDOW, STATE  # noqa: E402
from collect_iflyrec import cli_env  # noqa: E402

BASE = "https://open.iflyrecorder.com/skills/iflytek-recorder"
DEST = STATE / "iflyrec-skill"
CLI = DEST / "scripts" / "recording_assistant.py"
# 讯飞是国内服务，直连：系统代理没开时 urllib 照样会去连代理，反而连不上
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url: str) -> bytes:
    with _OPENER.open(url, timeout=120) as r:
        return r.read()


def download() -> str:
    print("==> 1/3 下载讯飞录音笔官方客户端")
    d = json.loads(fetch(BASE + "/latest.json"))
    a = d["artifact"]
    if not (d.get("name") == "iflytek-recorder" and d.get("skillDirectory") == "recording-assistant-prod"
            and a["url"].startswith(BASE + "/releases/") and re.fullmatch(r"[0-9a-f]{64}", a["sha256"])):
        sys.exit("版本信息不符合预期，停止")
    data = fetch(a["url"])
    if hashlib.sha256(data).hexdigest() != a["sha256"]:
        sys.exit("技能包指纹不对，停止")
    print(f"    版本 {d['version']}，指纹核对通过")

    print(f"==> 2/3 解压并逐个核对文件指纹，放到 {DEST}")
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():  # 不解压到目标文件夹以外的地方
                if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                    sys.exit(f"压缩包里有异常路径：{name}，停止")
            z.extractall(tmp)
        root = Path(tmp) / "recording-assistant-prod"
        files = json.loads((root / "release.json").read_text(encoding="utf-8"))["files"]
        for name, want in files.items():
            rel = name.split("/", 1)[1]
            if hashlib.sha256((root / rel).read_bytes()).hexdigest() != want:
                sys.exit("文件指纹不对：" + rel)
        if DEST.exists():
            shutil.rmtree(DEST)
        DEST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root, DEST)
    return d["version"]


def cli(*args: str) -> dict:
    r = subprocess.run([sys.executable, "-B", str(CLI), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=cli_env(), timeout=120,
                       stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        return json.loads(r.stdout)
    except ValueError:
        sys.exit("讯飞客户端没有正常返回：" + (r.stderr or r.stdout or "").strip()[-300:])


def authorize() -> int:
    print("==> 3/3 授权（用你录音笔绑定的讯飞账号）")
    d = cli("auth")
    if not d.get("success"):
        print(f"❌ 申请授权失败：{d.get('errorCode')} {d.get('message')}")
        return 1
    data = d.get("data") or {}
    if data.get("authenticated"):
        print("    已经授权过了，不用再登录")
        return 0
    print("    在浏览器里打开下面的链接，核对用户码后登录讯飞账号、点同意"
          f"（{int(data.get('expiresIn', 600)) // 60} 分钟内有效）：")
    print("    " + data["verificationUriComplete"])
    print("    用户码：" + data["userCode"])
    print("    这里会自动等你点完同意（按 Ctrl+C 可以先退出，之后重跑本脚本接着等）……")
    deadline = time.time() + int(data.get("expiresIn", 600)) + 30
    while time.time() < deadline:
        time.sleep(max(3, int(data.get("retryAfterSeconds") or data.get("interval") or 5)))
        d = cli("auth", "--check")
        if d.get("success"):
            print("✅ 讯飞录音笔授权成功")
            return 0
        if d.get("errorCode") != "AUTH_PENDING":
            print(f"❌ 授权没成功：{d.get('errorCode')} {d.get('message')}")
            return 1
        data = d.get("data") or data
    print("❌ 授权链接过期了，重跑本脚本会给一个新的")
    return 1


def main() -> int:
    version = download()
    rc = authorize()
    if rc == 0:
        print(f"完成（客户端版本 {version}）。现在重跑：powershell -ExecutionPolicy Bypass -File ops\\windows\\install.ps1")
        print("会装上每 10 分钟的讯飞录音采集；第一次往回取 30 天，每轮最多 15 条。")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n先退出了。之后重跑本脚本会接着等授权。")
        sys.exit(130)
