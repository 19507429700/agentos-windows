"""微信语音转文字（本机、离线）：把一批 SILK 语音解码，用本机的 whisper 模型转成文字。Windows 版。

由 collect_wechat.py 调用，不单独定时。它取出语音的音频字节、写成临时 .silk 文件，再用专用 Python 跑本脚本：
  python ops/transcribe_voice.py <放 .silk 文件的文件夹>
每个文件输出一行 JSON：{"name": 文件名, "ok": true, "text": 转出的文字, "sec": 语音秒数}，失败是 {"name", "ok": false, "error"}。

- 解码：silk-python（Windows 有现成安装包；Mac 版用的 pilk 在 Windows 上要装编译工具才装得上）。没有它就试 pilk。
- 转写：faster-whisper 的 small 模型，int8 量化，CPU。模型在 %USERPROFILE%\\.agentos\\whisper\\small，
  只读本地文件，不联网、不上传。包和模型都由安装脚本 ops\\windows\\install.ps1 装好、下好。

配置（环境变量）：AGENTOS_WHISPER_MODEL 模型文件夹，默认 %USERPROFILE%\\.agentos\\whisper\\small
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import STATE  # noqa: E402  （用 py 直接运行时也会自动换到专用环境）

SILK_MAGIC = b"#!SILK_V3"
RATE = 16000
PROMPT = "以下是普通话的句子。"  # 让 whisper 输出简体中文、带标点
# whisper 没听清时（语音太短、太安静）会「幻听」出视频字幕的套话。出现这些词就当没听出内容
HALLUCINATIONS = ("字幕", "点赞", "订阅", "打赏", "感谢观看", "谢谢观看", "明镜", "栏目", "Amara")


def model_dir() -> Path:
    return Path(os.environ.get("AGENTOS_WHISPER_MODEL") or STATE / "whisper" / "small")


def silk_to_pcm(body: bytes) -> bytes:
    """标准 SILK（从 #!SILK_V3 开始）→ 16k 单声道 16 位 PCM。"""
    try:
        import pysilk
        out = io.BytesIO()
        pysilk.decode(io.BytesIO(body), out, RATE)
        return out.getvalue()
    except ImportError:
        import pilk
        with tempfile.TemporaryDirectory() as tmp:
            silk, pcm = Path(tmp) / "a.silk", Path(tmp) / "a.pcm"
            silk.write_bytes(body)
            pilk.decode(str(silk), str(pcm), pcm_rate=RATE)
            return pcm.read_bytes()


def decode_silk(path: Path):
    """微信的语音前面多一个 \\x02，从 #!SILK_V3 开始才是标准 SILK。返回 float32 数组和秒数。"""
    import numpy as np
    raw = path.read_bytes()
    i = raw.find(SILK_MAGIC)
    if i < 0:
        raise ValueError("不是 SILK 语音")
    data = np.frombuffer(silk_to_pcm(raw[i:]), dtype=np.int16).astype(np.float32) / 32768.0
    return data, len(data) / RATE


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not Path(args[0]).is_dir():
        print("用法：transcribe_voice.py <放 .silk 文件的文件夹>", file=sys.stderr)
        return 2
    files = sorted(Path(args[0]).glob("*.silk"))
    if not files:
        return 0
    md = model_dir()
    if not (md / "model.bin").exists():
        print(f"没有语音模型：{md}（重跑一次安装脚本 ops\\windows\\install.ps1）", file=sys.stderr)
        return 2
    from faster_whisper import WhisperModel
    model = WhisperModel(str(md), device="cpu", compute_type="int8")
    for f in files:
        try:
            audio, sec = decode_silk(f)
            # temperature=0：不随机重猜，同一条语音每次结果一样；也少一些幻听
            segs, _info = model.transcribe(audio, language="zh", beam_size=5, vad_filter=True, temperature=0,
                                           condition_on_previous_text=False, initial_prompt=PROMPT)
            text = "".join(s.text for s in segs).strip()
            if any(w in text for w in HALLUCINATIONS):
                text = ""
            out = {"name": f.name, "ok": True, "text": text, "sec": round(sec, 1)}
        except Exception as e:  # noqa: BLE001 — 一条坏了不影响别的
            out = {"name": f.name, "ok": False, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
