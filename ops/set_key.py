"""往配置文件里填一项，不用记事本：
  python ops/set_key.py <配置> <名字>          粘贴值后回车（屏幕上不显示，适合令牌、密码）
  python ops/set_key.py <配置> <名字> <值>     直接写（适合不保密的，比如 agent AGENT_ENGINE codebuddy）
<配置> 是 bridge、mail、agent、healthchecks 之一，对应 %USERPROFILE%\\.agentos\\<配置>.env；
也可以写完整路径。这一行已经有了就替换，没有就加在最后。最后显示「已填好」或「没填上」，不会把值打印出来。
"""

from __future__ import annotations

import getpass
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentos_env import STATE  # noqa: E402

NAMES = {"bridge", "mail", "agent", "healthchecks"}


def set_key(path: Path, name: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == name and not line.lstrip().startswith("#"):
            if not done:
                out.append(f"{name}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{name}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".set-key-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print(__doc__)
        return 2
    which, name = args[0], args[1]
    path = STATE / f"{which}.env" if which in NAMES else Path(which).expanduser()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
        print(f"名字只能是大写字母、数字、下划线：{name}", file=sys.stderr)
        return 2
    if len(args) >= 3:
        value = args[2]
    else:
        value = getpass.getpass(f"粘贴 {name} 的值，然后按回车（屏幕上不显示，是正常的）：")
    value = value.strip().replace("\r", "")
    if "\n" in value:
        print("值里不能有换行", file=sys.stderr)
        return 2
    set_key(path, name, value)
    print(f"✅ {name} 已填好（{path}）" if value else f"⚠️ {name} 没填上（值是空的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
