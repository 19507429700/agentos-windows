"""Local snapshot adapters. No network, cookies, browser database or credentials.

Grok Bot schema 1 was observed in a real Windows desktop cache. The macOS
location is a candidate, not a claim that every release uses the same schema.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import sys

SNAPSHOT_SOURCES = {'grokbot'}
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024


def native_path(path: Path) -> Path:
    # Electron's base32 cache filenames can exceed MAX_PATH on Windows.
    if os.name == 'nt' and not str(path).startswith('\\\\?\\'):
        absolute = str(path.absolute())
        return Path('\\\\?\\UNC\\' + absolute[2:] if absolute.startswith('\\\\')
                    else '\\\\?\\' + absolute)
    return path


def default_snapshot_roots(home: Path, state: Path, platform=None):
    platform = platform or sys.platform
    if platform == 'win32':
        appdata = Path(os.environ.get('APPDATA', str(home/'AppData/Roaming')))
    elif platform == 'darwin':
        appdata = home/'Library/Application Support'
    else:
        appdata = Path(os.environ.get('XDG_CONFIG_HOME', str(home/'.config')))
    return {
        'grokbot': [str(appdata/'Grok Bot/sand-client-persistence')],
    }


def grok_key(path):
    try:
        return base64.b32decode(path.stem.upper() + '='*((-len(path.stem)) % 8)).decode('utf-8')
    except (ValueError, UnicodeError):
        return ''


def find_snapshots(src, root):
    root = native_path(root)
    if not root.is_dir():
        return []
    if src == 'grokbot':
        # Whitelist transcript replicas only; never open other account slices.
        return [p for p in root.glob('*.blob')
                if re.fullmatch(r'sand\.client\.slice\.account\..+\.transcript\.replicas\.[\w-]+', grok_key(p))]
    raise ValueError('unsupported local snapshot source')


def event(role, content, at, mid, session):
    return {'role': role, 'content': content, 'timestamp': at,
            'id': mid, 'session': session}


def snapshot_events(src, data, path):
    """Yield visible messages only; unknown snapshot schemas fail explicitly."""
    if src == 'grokbot':
        if not isinstance(data, dict) or type(data.get('schemaVersion')) is not int or data['schemaVersion'] != 1:
            raise ValueError('unsupported Grok Bot schema')
        value = data.get('value')
        if not isinstance(value, dict) or not isinstance(value.get('entries'), list):
            raise ValueError('Grok Bot entries missing')
        # Include the account in the opaque session ID to avoid multi-account collisions.
        session = grok_key(path)
        for entry in value['entries']:
            if not isinstance(entry, dict) or entry.get('isStreaming'):
                continue
            kind = entry.get('kind')
            if kind == 'message' and entry.get('role') in ('user', 'assistant'):
                yield event(entry['role'], entry.get('content'), entry.get('timestampMs'), entry.get('id'), session)
            elif kind == 'send-message':
                message = entry.get('message', {})
                if isinstance(message, dict) and message.get('type') == 'text':
                    yield event('assistant', message.get('content'), entry.get('timestampMs'), entry.get('id'), session)
            elif kind == 'voice-call':
                conversation = entry.get('conversation', {})
                turns = conversation.get('turns', []) if isinstance(conversation, dict) else []
                if not isinstance(turns, list):
                    raise ValueError('unsupported Grok Bot voice turns')
                for turn in turns:
                    if not isinstance(turn, dict) or turn.get('speaker') not in ('user', 'assistant'):
                        continue
                    at = turn.get('atMs')
                    # Only actual epoch timestamps. Never pretend relative offsets are dates.
                    if not isinstance(at, (int, float)) or at < 1e11:
                        continue
                    mid = str(entry.get('id', '')) + ':voice:' + str(turn['id']) if turn.get('id') else None
                    yield event(turn['speaker'], turn.get('text'), at, mid, session)
        return
    raise ValueError('unsupported local snapshot source')


def read_snapshot(path):
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError('snapshot exceeds 128 MiB; split export first')
    return json.loads(path.read_text(encoding='utf-8-sig'))
