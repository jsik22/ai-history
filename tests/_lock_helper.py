"""Hold the shared archive flock from a child process so tests can simulate another live writer."""
import errno
import fcntl
import os
import subprocess
import sys

HOLDER = r'''
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print('held', flush=True)
sys.stdin.read()
'''


def hold(root):
    """Start a process that owns the lock until release() or until it is killed."""
    root.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([sys.executable, '-c', HOLDER, str(root / '.lockfile')],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == 'held'
    return process


def release(process):
    process.stdin.close()
    process.wait()
    process.stdout.close()


def is_held(root):
    """True when some other process currently owns the lock."""
    fd = os.open(root / '.lockfile', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return True
        raise
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
