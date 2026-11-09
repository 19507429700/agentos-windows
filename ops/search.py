#!/usr/bin/env python3
"""全文检索：在采集原文（raw/）和知识库（kb/）里找关键词，返回「文件:行号: 原文」作为出处。

Agent 用法（以 agentos 身份，在项目文件夹根目录）：
  python ops/search.py 张总 合同
  python ops/search.py 定金 --source mail --since 2026-10-27
  python ops/search.py 展会 --limit 50

多个词之间是「且」。索引是 SQLite FTS5（trigram，中文不用分词），每次查询前自动增量更新。
只用标准库。
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".eml"}
MAX_FILE_BYTES = 5 * 1024 * 1024
DAY_RE = re.compile(r"/(\d{4}-\d{2}-\d{2})/")
SOURCES = ("mail", "feishu", "wechat", "inbox", "kb")


def space_dir(space: str) -> Path:
    return ROOT / "spaces" / space


def open_index(space: Path) -> sqlite3.Connection:
    idx = space / ".index"
    idx.mkdir(exist_ok=True)
    db = sqlite3.connect(idx / "search.db", timeout=30, isolation_level=None)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
        CREATE VIRTUAL TABLE IF NOT EXISTS lines USING fts5(
            text, path UNINDEXED, lineno UNINDEXED, day UNINDEXED, source UNINDEXED,
            tokenize='trigram');
    """)
    return db


def source_of(rel: str) -> str:
    """spaces/me 下的相对路径 → 来源：raw/mail/... → mail，kb/... → kb。"""
    parts = rel.split("/")
    if parts[0] == "raw" and len(parts) > 1:
        return parts[1]
    return parts[0]


def iter_files(space: Path):
    for top in ("raw", "kb"):
        base = space / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() in TEXT_SUFFIXES and not name.startswith("."):
                    yield p


def refresh(db: sqlite3.Connection, space: Path) -> int:
    """增量更新：只重建 mtime/大小变了的文件，删掉已不存在的文件。返回重建的文件数。"""
    known = {row[0]: (row[1], row[2]) for row in db.execute("SELECT path, mtime, size FROM files")}
    seen, changed = set(), 0
    db.execute("BEGIN")
    try:
        for p in iter_files(space):
            rel = p.relative_to(space).as_posix()
            seen.add(rel)
            try:
                st = p.stat()
            except OSError:
                continue
            if known.get(rel) == (st.st_mtime, st.st_size):
                continue
            db.execute("DELETE FROM lines WHERE path=?", (rel,))
            if st.st_size <= MAX_FILE_BYTES:
                m = DAY_RE.search("/" + rel)
                day, src = (m.group(1) if m else ""), source_of(rel)
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                db.executemany(
                    "INSERT INTO lines (text, path, lineno, day, source) VALUES (?, ?, ?, ?, ?)",
                    [(line, rel, i, day, src) for i, line in enumerate(text.splitlines(), 1)
                     if line.strip()])
            db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (rel, st.st_mtime, st.st_size))
            changed += 1
        for rel in set(known) - seen:
            db.execute("DELETE FROM lines WHERE path=?", (rel,))
            db.execute("DELETE FROM files WHERE path=?", (rel,))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return changed


def search(db: sqlite3.Connection, terms: list[str], source: str | None = None,
           since: str | None = None, limit: int = 20) -> tuple[list[tuple], int]:
    """所有词都要出现在同一行。≥3 个字的词走 FTS 索引，1–2 个字的词（如人名「张总」）走 LIKE。
    返回 (结果, 总命中数)；结果按「有日期的新在前」排序。"""
    where, args = [], []
    long_terms = [t for t in terms if len(t) >= 3]
    if long_terms:
        where.append("lines MATCH ?")
        args.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms))
    for t in terms:
        if len(t) < 3:
            where.append("text LIKE ? ESCAPE '\\'")
            args.append("%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if source:
        where.append("source = ?")
        args.append(source)
    if since:
        where.append("(day = '' OR day >= ?)")
        args.append(since)
    cond = " AND ".join(where) or "1"
    total = db.execute(f"SELECT count(*) FROM lines WHERE {cond}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT path, lineno, text FROM lines WHERE {cond}"
        " ORDER BY day DESC, path DESC, lineno LIMIT ?", args + [limit]).fetchall()
    return rows, total


def snippet(text: str, terms: list[str], width: int = 160) -> str:
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    pos = min((text.find(t) for t in terms if t in text), default=0)
    start = max(0, pos - width // 3)
    out = text[start:start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(text) else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="在 raw/ 和 kb/ 里全文检索，输出 文件:行号: 原文")
    ap.add_argument("terms", nargs="+", help="关键词，多个词表示同时出现")
    ap.add_argument("--source", choices=SOURCES, help="只搜某个来源")
    ap.add_argument("--since", help="只看这天及以后的原文，格式 YYYY-MM-DD（kb/ 不受影响）")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--space", default="me")
    a = ap.parse_args(argv)
    if a.since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.since):
        ap.error("--since 格式应为 YYYY-MM-DD")

    space = space_dir(a.space)
    db = open_index(space)
    refresh(db, space)
    terms = [t for t in a.terms if t.strip()]
    rows, total = search(db, terms, a.source, a.since, a.limit)
    prefix = f"spaces/{a.space}/"
    for path, lineno, text in rows:
        print(f"{prefix}{path}:{lineno}: {snippet(text, terms)}")
    if not rows:
        print("没有找到。试试少用几个词、换个说法，或去掉 --since / --source。")
    elif total > len(rows):
        print(f"（共 {total} 条，只显示最新 {len(rows)} 条；加 --limit 或 --since 缩小范围）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
