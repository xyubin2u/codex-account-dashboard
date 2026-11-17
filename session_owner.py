"""Resolve a single CLI root among IDs observed in one process's own log."""
import json
from pathlib import Path
import sqlite3
import sys

from account_display import UUID, _session_source


def rollout_path(base, session_id):
    """Resolve only this exact thread's original local transcript, read-only."""
    base = base.resolve()
    roots = [(base/name).resolve() for name in ('sessions', 'archived_sessions')]
    for database in sorted(base.glob('state_*.sqlite'), reverse=True):
        try:
            db = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=.1)
            try:
                row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (session_id,)).fetchone()
            finally:
                db.close()
            if not row:
                continue
            file = Path(row[0]).resolve()
            if not any(file.is_relative_to(root) for root in roots):
                continue
            with file.open('rb') as stream:
                meta = json.loads(stream.readline(1024*1024))
            if (isinstance(meta, dict) and meta.get('type') == 'session_meta' and
                    isinstance(meta.get('payload'), dict) and meta['payload'].get('id') == session_id):
                return file
        except (OSError, ValueError, TypeError, sqlite3.Error):
            continue
    return None


def main():
    base = Path(sys.argv[1])
    if len(sys.argv) == 4 and sys.argv[2] == '--rollout':
        if not UUID.fullmatch(sys.argv[3]):
            return 1
        file = rollout_path(base, sys.argv[3])
        if file:
            print(file)
        return 0
    candidates = json.loads(sys.argv[2])
    if not isinstance(candidates, list) or not all(isinstance(s, str) and UUID.fullmatch(s) for s in candidates):
        return 1
    roots = [sid for sid in dict.fromkeys(candidates) if _session_source(base, sid) == 'main']
    if len(roots) == 1:
        print(roots[0])
    return 0


if __name__ == '__main__':
    sys.exit(main())
