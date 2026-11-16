"""讯飞录音笔采集：把讯飞录音笔里的录音（讯飞生成的总结 + 带发言人的转写）增量存成 Markdown 原文。

取数据用讯飞官方的只读客户端（录音笔「Skill」里的 scripts/recording_assistant.py，只连 api.iflyrecorder.com，
只有读录音、读转写、读总结三项权限）。本脚本只调用它、不改它；授权凭据由它自己存在
%USERPROFILE%\.agentos\iflyrec-auth\prod\（Windows 上 AI 和你同一个账号，凭据要放在 AI 被禁止读的 .agentos 里），
本脚本不读凭据。

和飞书、邮件一样由任务计划程序每 10 分钟跑一次（AgentOS-collect-iflyrec）：
  - 每次查最近 IFLYREC_WINDOW_DAYS 天（默认 7 天）创建的录音；第一次往回查 IFLYREC_INITIAL_DAYS 天（默认 30 天）
  - 新录音读完总结和全部转写，存一份；已存过的不再重复
  - 刚录完、讯飞还在转写的，下一轮再试；2 天后还没有转写，就只存标题和总结，注明「暂无转写」

输出：spaces\me\raw\iflyrec\<录音日期>\录音-<标题>-<录音ID后6位>.md，日期按本机时区
状态：%USERPROFILE%\.agentos\iflyrec_state.json
配置（可选，%USERPROFILE%\.agentos\iflyrec.env）：IFLYREC_CLI 官方客户端路径，默认 .agentos\iflyrec-skill\scripts\recording_assistant.py；
  IFLYREC_INITIAL_DAYS、IFLYREC_WINDOW_DAYS、IFLYREC_MAX_PER_RUN（每轮最多存几条，默认 15）

底线：只读不写讯飞；原文只追加不修改；没装客户端时安静退出 0；授权失效、读取失败退出码非 0（供告警）。
只用标准库。装客户端、授权：py ops\install_iflyrec.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import NO_WINDOW, RAW, STATE, child_env  # noqa: E402

log = logging.getLogger("collect_iflyrec")

STATE_DIR = STATE
PENDING_GIVE_UP_SEC = 2 * 86400
AUTH_CODES = {"AUTH_REQUIRED", "AUTH_EXPIRED", "AUTH_INVALID", "AUTH_CANCELLED", "PERMISSION_DENIED",
              "CREDENTIAL_INVALID", "ENVIRONMENT_MISMATCH", "AUTH_SUPERSEDED"}


class CliError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(f"{code}: {message}")
        self.code, self.retryable = code, retryable


def load_env(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8-sig")  # 记事本存的文件开头可能有 BOM
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cli_env() -> dict:
    """讯飞客户端的凭据放进 .agentos\\iflyrec-auth（默认会放在用户文件夹的 .iflyrec-assistant）。"""
    return child_env(IFLYREC_ASSISTANT_HOME=str(STATE_DIR / "iflyrec-auth"))


def cli_path() -> Path:
    return Path(os.environ.get("IFLYREC_CLI") or STATE_DIR / "iflyrec-skill" / "scripts" / "recording_assistant.py").expanduser()


def run_cli(*args: str) -> dict:
    """调官方客户端，返回 data；出错抛 CliError。客户端只在 stdout 输出一个 JSON 对象。"""
    try:
        r = subprocess.run([sys.executable, "-B", str(cli_path()), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, env=cli_env(),
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise CliError("TIMEOUT", "讯飞客户端 120 秒没有返回", True)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise CliError("INVALID_OUTPUT", (r.stderr or r.stdout or "").strip()[-200:] or f"退出码 {r.returncode}")
    if not d.get("success"):
        raise CliError(str(d.get("errorCode") or "UNKNOWN"), str(d.get("message") or "")[:200], bool(d.get("retryable")))
    return d.get("data") or {}


# ---------- 读取 ----------

def list_recordings(since: datetime) -> list[dict]:
    """since 之后创建的全部录音（按服务的分页读完）。"""
    out, cursor, seen = [], None, set()
    for _ in range(40):
        args = ["search", "--created-from", since.strftime("%Y-%m-%dT%H:%M:%SZ"), "--page-size", "50"]
        if cursor:
            args += ["--cursor", cursor]
        data = run_cli(*args)
        for rec in data.get("recordings") or []:
            rid = str(rec.get("recordingId") or "")
            if rid and rid not in seen:
                seen.add(rid)
                out.append(rec)
        has_more = str(data.get("hasMore")).lower() == "true"
        nxt = data.get("nextCursor")
        if not has_more or not nxt or nxt == cursor:
            break
        cursor = nxt
    return out


def read_all(kind: str, rid: str) -> tuple[str | None, list[dict]]:
    """读完一条录音的总结（kind=summary，返回文本）或转写（kind=transcript，返回分段）。
    没有这项内容返回 (None, [])；内容中途变了从头重读一次。"""
    for _attempt in range(2):
        text, segs, cursor, version = "", [], None, None
        try:
            for _ in range(200):
                args = [kind, rid, "--max-chars", "12000"] + (["--cursor", cursor] if cursor else [])
                data = run_cli(*args)
                if version and data.get("contentVersion") != version:
                    raise CliError("CONTENT_CHANGED", "内容版本变了")
                version = data.get("contentVersion")
                text += data.get("text") or ""
                segs += data.get("segments") or []
                nxt = data.get("nextCursor")
                if str(data.get("hasMore")).lower() != "true" or not nxt or nxt == cursor:
                    break
                cursor = nxt
            return (text if kind == "summary" else None), segs
        except CliError as e:
            if e.code == "CONTENT_NOT_AVAILABLE":
                return None, []
            if e.code != "CONTENT_CHANGED":
                raise
    raise CliError("CONTENT_CHANGED", "内容一直在变，下一轮再读", True)


# ---------- 写文件 ----------

def local_time(iso: str) -> datetime:
    return datetime.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z").astimezone()


def fmt_duration(sec) -> str:
    try:
        s = int(round(float(sec)))
    except (TypeError, ValueError):
        return "未知"
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return (f"{h} 小时 " if h else "") + f"{m} 分 {s} 秒"


def safe_name(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_.")[:40] or "未命名"


def demote(markdown: str) -> str:
    """讯飞总结里的「# 标题」降两级，放在本文件的「## 讯飞生成的总结」下面。"""
    return re.sub(r"^(#{1,4}) ", lambda m: "##" + m.group(1) + " ", markdown, flags=re.M)


def render(rec: dict, summary: str | None, segs: list[dict], note: str = "") -> str:
    t = local_time(rec["createdAt"])
    title = (rec.get("title") or "未命名录音").strip()
    lines = [f"# 录音：{title}（{t:%Y-%m-%d %H:%M}）", "",
             "- 来源：讯飞录音笔",
             f"- 录音 ID：{rec['recordingId']}",
             f"- 录音时间：{t:%Y-%m-%d %H:%M}（本机时区）",
             f"- 时长：{fmt_duration(rec.get('durationSeconds'))}",
             f"- 读取时间：{datetime.now():%Y-%m-%d %H:%M}", ""]
    if note:
        lines += [f"> {note}", ""]
    lines += ["## 讯飞生成的总结", "", demote(summary.strip()) if summary and summary.strip() else "（讯飞没有生成总结）", ""]
    lines += ["## 转写原文", ""]
    if segs:
        for s in segs:
            who = (s.get("speakerName") or (f"讲话人{s['speakerId']}" if s.get("speakerId") else "?")).strip()
            first, *rest = str(s.get("text") or "").strip().split("\n")
            lines.append(f"- {who}：{first}")
            lines += ["  " + r for r in rest]
    else:
        lines.append("（暂无转写）")
    return "\n".join(lines) + "\n"


def write_once(path: Path, text: str) -> bool:
    """原文只追加不修改：文件已存在就不写。"""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return True


# ---------- 状态 ----------

def state_path() -> Path:
    return STATE_DIR / "iflyrec_state.json"


def load_state() -> dict:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("done", {})     # 录音ID -> 存到的文件（相对原文根目录）
    st.setdefault("pending", {})  # 录音ID -> 第一次见到还没转写的时间
    return st


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, state_path())
    except BaseException:
        os.unlink(tmp)
        raise


# ---------- 主流程 ----------

def collect(st: dict, raw_dir: Path, now: float, initial_days: int, window_days: int, max_per_run: int) -> int:
    days = window_days if st.get("last_ok") else initial_days
    since = datetime.fromtimestamp(now, timezone.utc) - timedelta(days=days)
    recs = [r for r in list_recordings(since) if str(r["recordingId"]) not in st["done"]]
    recs.sort(key=lambda r: r.get("createdAt") or "")  # 旧的先存
    saved = 0
    for rec in recs[:max_per_run]:
        rid = str(rec["recordingId"])
        summary, _ = read_all("summary", rid)
        _, segs = read_all("transcript", rid)
        note = ""
        if not segs:
            first = st["pending"].setdefault(rid, now)
            if now - first < PENDING_GIVE_UP_SEC:
                log.info("「%s」还没有转写，下一轮再试", rec.get("title"))
                continue
            note = "这条录音读取了 2 天仍没有转写，只存了标题和总结。"
        t = local_time(rec["createdAt"])
        rel = f"iflyrec/{t:%Y-%m-%d}/录音-{safe_name(rec.get('title') or '')}-{rid[-6:]}.md"
        write_once(raw_dir / rel, render(rec, summary, segs, note))
        st["done"][rid] = rel
        st["pending"].pop(rid, None)
        saved += 1
        log.info("存好：%s（转写 %d 段）", rel, len(segs))
    if len(recs) > max_per_run:
        log.info("还有 %d 条留到下一轮", len(recs) - max_per_run)
    st["last_ok"] = int(now)
    return saved


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(STATE_DIR / "iflyrec.env")
    if not cli_path().exists():
        log.info("没装讯飞录音笔客户端（%s），跳过", cli_path())
        return 0
    raw_dir = Path(os.environ.get("AGENTOS_RAW") or RAW)
    st = load_state()
    try:
        n = collect(st, raw_dir, time.time(), int(os.environ.get("IFLYREC_INITIAL_DAYS") or 30),
                    int(os.environ.get("IFLYREC_WINDOW_DAYS") or 7), int(os.environ.get("IFLYREC_MAX_PER_RUN") or 15))
    except CliError as e:
        save_state(st)
        if e.code in AUTH_CODES:
            log.error("讯飞录音笔授权失效（%s），重新授权：在项目文件夹运行 py ops\\install_iflyrec.py", e.code)
            return 2
        if e.code == "CREDENTIAL_BUSY":
            log.info("讯飞凭据正被另一个进程刷新，下一轮再采")
            return 0
        log.error("讯飞录音采集失败：%s", e)
        return 1
    save_state(st)
    log.info("讯飞录音采集完成：新存 %d 条", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
