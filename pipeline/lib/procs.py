"""Shell/tmux helper utilities."""
import subprocess
import shlex
import time


def sh(cmd, check=True, cwd=None, timeout=60):
    res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"Command failed ({res.returncode}): {cmd}\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
    return res.stdout.strip()


def tmux_session_exists(name):
    res = subprocess.run(f"tmux has-session -t {shlex.quote(name)}", shell=True, capture_output=True)
    return res.returncode == 0


def tmux_new_session(name, width=200, height=50):
    sh(f"tmux new-session -d -s {shlex.quote(name)} -x {width} -y {height}")


def tmux_send(name, keys, enter=True):
    q = shlex.quote(keys)
    cmd = f"tmux send-keys -t {shlex.quote(name)} {q}"
    if enter:
        cmd += " Enter"
    sh(cmd)


def tmux_capture(name, lines=200):
    return sh(f"tmux capture-pane -t {shlex.quote(name)} -p -S -{lines}", check=False)


def tmux_kill(name):
    sh(f"tmux kill-session -t {shlex.quote(name)}", check=False)


def wait_for_idle(tmux_name, idle_seconds, poll_interval, max_wait_seconds):
    """Wait until the tmux pane content stops changing for `idle_seconds`,
    or the Claude Code prompt glyph is visible. Returns final pane text."""
    start = time.time()
    last_snapshot = None
    stable_since = None
    while time.time() - start < max_wait_seconds:
        snap = tmux_capture(tmux_name, lines=80)
        now = time.time()
        if snap != last_snapshot:
            last_snapshot = snap
            stable_since = now
        elif stable_since and (now - stable_since) >= idle_seconds:
            return snap
        time.sleep(poll_interval)
    return last_snapshot or ""
