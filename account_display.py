"""Display the isolated account and this window's own session topic."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import unicodedata

from account_labels import valid_label

SESSION_CREATED = re.compile(
    r'\b(?:INFO|DEBUG)\s+session_loop\{thread_id[=:]"?([0-9a-f-]{36})"?\}:'
    r'\s+codex_core::session:\s+new\b', re.I)
UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)


def _session_source(base, session_id):
    for database in sorted(base.glob('state_*.sqlite'), reverse=True):
        try:
            connection = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=.1)
            try:
                row = connection.execute('SELECT source FROM threads WHERE id=?', (session_id,)).fetchone()
            finally:
                connection.close()
            if row:
                if row[0] == 'cli':
                    return 'main'
                try:
                    source = json.loads(row[0])
                    if isinstance(source, dict) and 'subagent' in source:
                        return 'subagent'
                except (ValueError, TypeError):
                    pass
        except (OSError, sqlite3.Error):
            continue
    return None


def _title_session(base, candidates, explicit=None):
    sources = [(sid, _session_source(base, sid)) for sid in candidates]
    mains = [sid for sid, source in sources if source == 'main']
    if mains:
        return mains[-1]
    if explicit:
        return explicit
    # Before metadata is persisted, keep the first session created by this
    # window. A later unclassified subagent must not displace it.
    return next((sid for sid, source in sources if source != 'subagent'), None)


def _clean_title(text):
    text = ''.join(' ' if c.isspace() else c for c in str(text))
    text = ''.join(c for c in text if not unicodedata.category(c).startswith('C'))
    return ' '.join(text.split())[:120]


def _topic(base, session_id):
    # Read-only lookup by the exact ID, never the newest global conversation.
    title = None
    try:
        with (base/'session_index.jsonl').open() as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                    if entry.get('id') == session_id and entry.get('thread_name'):
                        title = entry['thread_name']
                except (ValueError, AttributeError):
                    continue
    except OSError:
        pass
    if title:
        return title
    for database in sorted(base.glob('state_*.sqlite'), reverse=True):
        try:
            connection = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=.1)
            try:
                row = connection.execute('SELECT title FROM threads WHERE id=?', (session_id,)).fetchone()
                if row and row[0]:
                    return row[0]
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            continue
    return None


def _configured_log(args):
    for index, arg in enumerate(args):
        value = args[index+1] if arg in ('-c', '--config') and index+1 < len(args) else ''
        if value.startswith('log_dir='):
            try:
                return Path(json.loads(value.split('=', 1)[1]))/'codex-tui.log'
            except (ValueError, TypeError):
                continue
    return None


@contextmanager
def account_title(label, base, args, log_file=None):
    if not valid_label(label):
        raise ValueError('Invalid account display label')
    if not sys.stdout.isatty():
        yield []
        return
    temporary = None
    extra = ['-c', 'tui.terminal_title=[]']
    log_file = log_file or _configured_log(args)
    if log_file is None:
        temporary = tempfile.TemporaryDirectory(prefix='codex-session-title-')
        log_file = Path(temporary.name)/'codex-tui.log'
        extra += ['-c', 'log_dir='+json.dumps(temporary.name)]
    output = sys.stdout
    session_id = next((a for a in args if UUID.fullmatch(a)), None)
    explicit = session_id
    candidates = []
    offset = 0
    pending = b''
    previous = None
    stopped = threading.Event()

    def refresh():
        nonlocal session_id, offset, pending, previous
        try:
            with Path(log_file).open('rb') as stream:
                if Path(log_file).stat().st_size < offset:
                    offset = 0
                    pending = b''
                stream.seek(offset)
                chunk = stream.read(1024*1024)
                offset = stream.tell()
            lines = (pending+chunk).split(b'\n')
            pending = lines.pop()[-8192:]
            for line in lines:
                match = SESSION_CREATED.search(line.decode('utf-8', errors='replace'))
                if match and match[1] not in candidates:
                    candidates.append(match[1])
        except OSError:
            pass
        session_id = _title_session(Path(base), candidates, explicit)
        topic = _topic(Path(base), session_id) if session_id else None
        topic = topic or ('未命名会话' if session_id else ('选择会话' if 'resume' in args else '新会话'))
        title = f'账户 {label}｜{_clean_title(topic)}'
        if title != previous:
            output.write(f'\033]0;{title}\007')
            output.flush()
            previous = title

    def watch():
        while not stopped.wait(.5):
            refresh()

    thread = None
    try:
        refresh()
        thread = threading.Thread(target=watch, name='codex-session-title', daemon=True)
        thread.start()
        yield extra
    finally:
        stopped.set()
        if thread:
            thread.join(timeout=2)
        if temporary:
            temporary.cleanup()
