"""Simple PID-based lockfile to prevent overlapping runs of the same script.

Locks live under state_dir (already gitignored) — one lock per script name,
e.g. state/pickup.lock, state/monitor.lock. A stale lock (PID no longer
alive) is detected and overwritten automatically.
"""
import os
import errno

from . import config


def _lock_path(name):
    cfg = config.load()
    state_dir = os.path.dirname(cfg["state_db"])
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, f"{name}.lock")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM  # exists but owned by someone else
    else:
        return True


class LockHeld(Exception):
    pass


class Lock:
    """Context manager: `with Lock("pickup"): ...` raises LockHeld if another
    live process already holds the same-named lock."""

    def __init__(self, name):
        self.name = name
        self.path = _lock_path(name)
        self._acquired = False

    def __enter__(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    existing_pid = int(f.read().strip())
            except (ValueError, OSError):
                existing_pid = None
            if existing_pid and _pid_alive(existing_pid):
                raise LockHeld(f"{self.name} already running (PID {existing_pid})")
            # stale lock — fall through and overwrite

        with open(self.path, "w") as f:
            f.write(str(os.getpid()))
        self._acquired = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._acquired:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False
