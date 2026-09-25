"""Transaction-consistent, read-only ZCode session extraction by an isolated reader."""
import argparse
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
import time
from contextlib import contextmanager
from .zcode import DB


@contextmanager
def stable_copy(path):
    """Include WAL frames; a private copy lets SQLite build a missing SHM index.

    The original mounted home remains read-only. Reject symlinks and retry any
    concurrent replacement/write rather than reading a torn database snapshot.
    """
    sources = [path, Path(str(path) + '-wal')]
    def signature():
        values = []
        for source in sources:
            if source.is_symlink():
                raise ValueError('Symlinked native ZCode store')
            try:
                stat = source.stat()
                if stat.st_size > 128 * 1024 * 1024:
                    raise ValueError('Native ZCode store exceeds reader limit')
                values.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            except FileNotFoundError:
                values.append(None)
        return values
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / path.name
        for _ in range(20):
            before = signature()
            try:
                for source, exists in zip(sources, before):
                    copy = Path(directory) / source.name
                    copy.unlink(missing_ok=True)
                    if exists:
                        shutil.copyfile(source, copy)
                if before == signature():
                    yield target
                    return
            except FileNotFoundError:
                pass
            time.sleep(.05)
        raise RuntimeError('Native ZCode store changed during capture')


def inspect(session_id, home='/home/agent'):
    path = Path(home) / DB
    if path.is_symlink() or Path(str(path) + '-wal').is_symlink():
        raise ValueError('Symlinked native ZCode store')
    if not path.exists() and Path(str(path) + '-wal').exists():
        raise ValueError('Native WAL remains without its database')
    if not path.exists():
        return {'verified': True, 'session_id': session_id, 'present': False, 'records': []}
    with stable_copy(path) as copied:
        return read_copy(copied, session_id)


def read_copy(path, session_id):
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        records = []
        for table, column in [('session', 'id'), ('message', 'session_id'),
                              ('part', 'session_id'), ('session_entry', 'session_id')]:
            for row in connection.execute(f'SELECT * FROM {table} WHERE {column}=? ORDER BY rowid', (session_id,)):
                row = dict(row)
                if isinstance(row.get('data'), str):
                    row['data'] = json.loads(row['data'])
                records.append({'table': table, 'row': row})
        return {'verified': True, 'session_id': session_id, 'present': True, 'records': records}
    finally:
        connection.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--session-id', required=True)
    args = p.parse_args()
    try:
        print(json.dumps(inspect(args.session_id)))
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'verified': False, 'session_id': args.session_id, 'error': str(exc)}))
        raise SystemExit(1)
