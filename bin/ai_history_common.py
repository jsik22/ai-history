"""Shared local archive storage; callers hold locked() around state transitions."""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

def atomic(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        if default is not None:
            return default
        raise

def key(sid, tid):
    if not isinstance(sid, str) or not sid or not isinstance(tid, str) or not tid:
        raise ValueError('missing session/turn identity; event retained')
    return hashlib.sha256((sid + '\0' + tid).encode()).hexdigest()

def local_timestamp(value=None):
    stamp = dt.datetime.now().astimezone() if value is None else dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()
    return stamp.strftime('%Y-%m-%d %H:%M:%S')

def error(root, message):
    with (Path(root) / '.errors').open('a', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(f'{dt.datetime.now().isoformat()} {message}\n')

def block(row, tool='codex'):
    timestamp = row['ts']
    if not re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', timestamp):
        raise ValueError('invalid archive timestamp')
    cwd = row['cwd']
    home = str(Path.home())
    if cwd == home or cwd.startswith(home + '/'):
        cwd = '~' + cwd[len(home):]
    prompt = row.get('prompt')
    if prompt is None:
        prompt = '(이어서)'
    quoted = '\n'.join('> ' + line for line in prompt.split('\n'))
    return (f"===== {timestamp} | {tool} | {cwd} | s:{row['sid'][:8]} =====\n"
            + quoted + '\n' + row['answer'] + '\n\n').encode('utf-8')

class Archive:
    def __init__(self, root, tool):
        if tool not in ('claude', 'codex'):
            raise ValueError('unknown archive tool')
        self.root, self.tool = Path(root), tool
        self.state = self.root / ('.' + tool + '-state')
        self.pending = self.root / '.pending' / tool
        self.queue = self.root / '.pending' / (tool + '-events')

    def setup(self):
        for path in (self.root, self.root / '.pending', self.state, self.state / 'done', self.pending, self.queue):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.chmod(0o700)

    atomic = staticmethod(atomic)
    read = staticmethod(read)
    key = staticmethod(key)
    local_timestamp = staticmethod(local_timestamp)

    def error(self, message):
        error(self.root, self.tool + ' ' + message)

    def block(self, row):
        return block(row, self.tool)

    def recover_all(self):
        # Every writer repairs an earlier writer BEFORE appending anything.
        # This prevents Claude from occupying a pending Codex journal offset.
        for tool in ('codex', 'claude'):
            Archive(self.root, tool).recover()

    @contextlib.contextmanager
    def locked(self, timeout=2.0):
        lock = self.root / '.lock'
        deadline = time.monotonic() + timeout
        while True:
            try:
                lock.mkdir(mode=0o700)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('shared history lock timeout; event retained')
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
        try:
            self.recover_all()
            yield
        finally:
            lock.rmdir()

    def recover(self):
        journal = self.state / 'transaction.json'
        if not journal.exists():
            return
        txn = read(journal)
        target = self.root / txn['relative_path']
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('archive transaction path outside root')
        expected = txn['block'].encode('utf-8')
        offset = txn['offset']
        with target.open('rb') as stream:
            stream.seek(offset)
            actual = stream.read(len(expected))
        if actual != expected:
            # A short exact prefix at EOF can only be our unfinished append
            # under the shared-writer protocol; finish it without truncation.
            if target.stat().st_size != offset + len(actual) or not expected.startswith(actual):
                raise RuntimeError('ambiguous append requires inspection; transaction preserved')
            with target.open('ab') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(expected[len(actual):])
                stream.flush()
                os.fsync(stream.fileno())
        atomic(self.state / 'done' / txn['key'], {'sid': txn['sid'], 'tid': txn['tid']})
        journal.unlink()

    def commit(self, row, adopt_existing=False):
        """Append once by identity. Legacy adoption requires an unambiguous caller mapping."""
        # Never replace a journal after any earlier append/marker failure.
        self.recover_all()
        ident = key(row['sid'], row['tid'])
        if (self.state / 'done' / ident).exists():
            return False
        folder = self.root / row['ts'][:7]
        content = self.block(row)
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / (row['ts'][:10] + '.md')
        if adopt_existing and path.exists() and content in path.read_bytes():
            atomic(self.state / 'done' / ident, {'sid': row['sid'], 'tid': row['tid']})
            return False
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            os.fchmod(stream.fileno(), 0o600)
            offset = os.fstat(stream.fileno()).st_size
        atomic(self.state / 'transaction.json', {
            'relative_path': str(path.relative_to(self.root)), 'offset': offset,
            'block': content.decode('utf-8'), 'key': ident, 'sid': row['sid'], 'tid': row['tid'],
        })
        self.recover()
        return True
