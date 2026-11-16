"""下载微信语音转文字用的 whisper small 模型（约 460MB）到 %USERPROFILE%\\.agentos\\whisper\\small。

安装脚本（ops\\windows\\install.ps1）在微信采集装好之后调用；已经下好就跳过，中断了重跑会接着下。
国内默认走 hf-mirror.com；要换来源先设环境变量 HF_ENDPOINT。设过代理（%USERPROFILE%\\.agentos\\proxy）就走代理。

用法：python ops/download_whisper.py      成功退出码 0，失败非 0（安装脚本只提示，不中断）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import STATE, proxy  # noqa: E402

FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def main() -> int:
    target = Path(os.environ.get("AGENTOS_WHISPER_MODEL") or STATE / "whisper" / "small")
    if (target / "model.bin").exists():
        print(f"    语音模型已经有了：{target}")
        return 0
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"  # 进度条写在报错输出里，PowerShell 会当成出错
    p = proxy()
    if p:
        os.environ.setdefault("HTTPS_PROXY", p)
        os.environ.setdefault("HTTP_PROXY", p)
    print(f"    下载语音模型（约 460MB，第一次要几分钟）到 {target} ……", flush=True)
    try:
        from huggingface_hub import snapshot_download
        target.mkdir(parents=True, exist_ok=True)
        snapshot_download("Systran/faster-whisper-small", local_dir=str(target), allow_patterns=FILES)
    except Exception as e:  # noqa: BLE001 — 网络问题很常见，说清楚怎么办
        print(f"    下载没成功：{type(e).__name__}: {e}")
        print("    网络不好的话过一会儿重跑安装脚本，会接着下；国外网络可先设 HF_ENDPOINT=https://huggingface.co")
        return 1
    if not (target / "model.bin").exists():
        print("    下载不完整，重跑安装脚本会接着下")
        return 1
    print("    语音模型下好了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
