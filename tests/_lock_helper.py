"""Hold the shared archive lock from a child process so tests can simulate another live writer."""
import os
from pathlib import Path
import subprocess
import sys

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
from ai_history_common import BINARY, try_lock, unlock

HOLDER = r'''
import os, sys, time
sys.path.insert(0, sys.argv[2])
from ai_history_common import BINARY, try_lock
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | BINARY, 0o600)
while not try_lock(fd):
    time.sleep(.01)
print('held', flush=True)
sys.stdin.read()
'''


def hold(root):
    """Start a process that owns the lock until release() or until it is killed."""
    root.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([sys.executable, '-c', HOLDER, str(root / '.lockfile'), str(BIN)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == 'held'
    return process


def release(process):
    process.stdin.close()
    process.wait()
    process.stdout.close()


def is_held(root):
    """True when some other process currently owns the lock."""
    fd = os.open(root / '.lockfile', os.O_RDWR | os.O_CREAT | BINARY, 0o600)
    try:
        if not try_lock(fd):
            return True
        unlock(fd)
        return False
    finally:
        os.close(fd)
