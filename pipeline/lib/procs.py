"""Shell/tmux helper utilities."""
import subprocess
import shlex
import time
import os
import glob
import json


def sh(cmd, check=True, cwd=None, timeout=60):
    res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"Command failed ({res.returncode}): {cmd}\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
    return res.stdout.strip()


def tmux_session_exists(name):
    res = subprocess.run(f"tmux has-session -t {shlex.quote(name)}", shell=True, capture_output=True)
    return res.returncode == 0


def tmux_new_session(name, width=200, height=50):
    # DISABLE_AUTO_UPDATE=true stops oh-my-zsh's own "Would you like to
    # update?" interactive prompt from ever firing in this pane. Without
    # this, that prompt can print itself right as the pipeline's first
    # tmux_send arrives, silently eating the leading character of the
    # command (observed live: "cd /path..." became "d /path...", so `cd`
    # never ran, `cat .claude-code/.aidev_prompt.txt` failed with the
    # wrong cwd, and `claude --resume <id>` then failed with "No
    # conversation found" — not a real session-ID problem at all, just a
    # mangled command). Caught on RND-14853, on BOTH the original launch
    # and the auto-retry relaunch — same race, same corruption, twice.
    # Caught again on RND-14737: DISABLE_AUTO_UPDATE alone wasn't enough —
    # any shell startup noise (MOTD, another prompt, oh-my-zsh's own
    # init) racing the very first send-keys can eat/mangle it the same
    # way, and the fallout is severe: claude starts in the wrong cwd,
    # never notices, and can sit hung for hours with nothing detecting it
    # (a live claude process satisfies claude_process_alive, so the
    # monitor's crash check never fires). wait_for_shell_ready below is
    # the real fix — never send the first real command until the shell
    # has demonstrably settled, instead of assuming a fixed delay or an
    # env var covers every source of startup noise.
    sh(
        f"tmux new-session -d -s {shlex.quote(name)} -x {width} -y {height} "
        f"-e DISABLE_AUTO_UPDATE=true"
    )
    wait_for_shell_ready(name)


def wait_for_shell_ready(name, timeout_s=10, quiet_s=0.5):
    """Blocks until the pane's output has stopped changing for `quiet_s`
    seconds (or `timeout_s` total elapses) — a shell that's still printing
    MOTD/update-check/init noise has output still in flux; one that's
    reached its prompt and gone idle does not. Sending the first real
    command only once the pane is provably quiet closes the race that
    DISABLE_AUTO_UPDATE alone didn't (see tmux_new_session's comment)."""
    deadline = time.time() + timeout_s
    last = None
    stable_since = None
    while time.time() < deadline:
        current = tmux_capture(name, lines=20)
        if current == last:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= quiet_s:
                return
        else:
            stable_since = None
        last = current
        time.sleep(0.2)
    # Timed out without ever seeing quiet output — proceed anyway rather
    # than blocking pickup/monitor forever; the post-launch cwd
    # verification below is the real backstop if this pane is still
    # genuinely unsettled.


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


def tmux_pane_pid(name):
    """Returns the top-level shell PID of the tmux pane, or None if the
    session doesn't exist."""
    res = subprocess.run(
        f"tmux list-panes -t {shlex.quote(name)} -F '#{{pane_pid}}'",
        shell=True, capture_output=True, text=True,
    )
    pid = res.stdout.strip()
    return pid if res.returncode == 0 and pid else None


def claude_process_alive(tmux_name):
    """A tmux session can outlive the `claude` process running inside it —
    e.g. an unhandled API error crashes Claude Code back to a bare shell
    prompt, and the session then sits there indefinitely looking `RUNNING`
    to anything that only checks `tmux_session_exists`. This walks the
    pane's shell process tree (pgrep -P, recursively) looking for a `claude`
    binary among the descendants. Returns False if the session is gone, the
    pane has no such descendant, or the check itself fails for any reason —
    callers should treat False as \"can't confirm it's alive\", not silently
    ignore it."""
    pane_pid = tmux_pane_pid(tmux_name)
    if not pane_pid:
        return False
    to_check = [pane_pid]
    seen = set()
    while to_check:
        pid = to_check.pop()
        if pid in seen:
            continue
        seen.add(pid)
        res = subprocess.run(f"ps -o command= -p {pid}", shell=True, capture_output=True, text=True)
        if res.returncode == 0 and "claude" in res.stdout.lower():
            return True
        children = subprocess.run(f"pgrep -P {pid}", shell=True, capture_output=True, text=True)
        to_check.extend(children.stdout.split())
    return False


def read_context_usage_pct(worktree_path, session_id, context_window=1_000_000):
    """Best-effort read of a live Claude Code session's current context usage,
    as a fraction of `context_window` (default matches the 1M-token Sonnet
    window shown in the TUI's own status bar). Reads the session's own JSONL
    transcript under ~/.claude/projects/<slug>/<session_id>.jsonl (the same
    file the TUI itself renders from) rather than scraping the tmux pane's
    rendered status-bar text — that text is free-form UI, not a structured
    field, and the project-folder slug/percentage math are things we control
    directly from the worktree path and the last `usage` block in the
    transcript. Returns None if the session_id/file/usage block isn't found —
    callers must treat None as \"unknown\", never as \"0% used\".

    The project directory name is the worktree's absolute path with every
    `/` replaced by `-` (confirmed against a real live session's directory
    name) — reconstructed here rather than globbed by session_id alone,
    since a stale project dir from an earlier ticket in the same worktree
    path could otherwise collide."""
    if not session_id:
        return None
    slug = worktree_path.strip("/").replace("/", "-")
    jsonl_path = os.path.expanduser(f"~/.claude/projects/-{slug}/{session_id}.jsonl")
    if not os.path.exists(jsonl_path):
        matches = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
        if not matches:
            return None
        jsonl_path = matches[0]

    last_usage = None
    try:
        with open(jsonl_path, "rb") as f:
            # Scan from the end in chunks — these transcripts can be huge and
            # we only need the most recent `usage` block, not the whole file.
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 200_000)
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", errors="ignore")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = (entry.get("message") or {}).get("usage") if isinstance(entry.get("message"), dict) else None
            if usage:
                last_usage = usage
                break
    except OSError:
        return None

    if not last_usage:
        return None
    used = (
        last_usage.get("input_tokens", 0)
        + last_usage.get("cache_creation_input_tokens", 0)
        + last_usage.get("cache_read_input_tokens", 0)
        + last_usage.get("output_tokens", 0)
    )
    return used / context_window


def claude_pid_in_tmux(tmux_name):
    """Same descendant-walk as claude_process_alive, but returns the PID of
    the claude process itself (not just True/False) — used by
    verify_claude_launched_in to check its real cwd."""
    pane_pid = tmux_pane_pid(tmux_name)
    if not pane_pid:
        return None
    to_check = [pane_pid]
    seen = set()
    while to_check:
        pid = to_check.pop()
        if pid in seen:
            continue
        seen.add(pid)
        res = subprocess.run(f"ps -o command= -p {pid}", shell=True, capture_output=True, text=True)
        if res.returncode == 0 and "claude" in res.stdout.lower():
            return pid
        children = subprocess.run(f"pgrep -P {pid}", shell=True, capture_output=True, text=True)
        to_check.extend(children.stdout.split())
    return None


def verify_claude_launched_in(tmux_name, expected_cwd, timeout_s=15, poll_interval=1):
    """Real backstop for the shell-race class of bug (see tmux_new_session's
    comment): confirms the claude process that actually started in this
    pane has the cwd we intended, not just that some claude process exists
    (claude_process_alive alone can't tell a correctly-launched session
    from one that silently started in the wrong directory after a mangled
    `cd`). Polls briefly since the process may not have fully started yet
    right after tmux_send. Returns True if confirmed correct, False if a
    claude process is running but in the wrong directory (the actual
    RND-14737 failure mode) or nothing showed up in time — callers should
    treat False as launch-failed and retry with a fresh session, not
    assume it'll sort itself out."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pid = claude_pid_in_tmux(tmux_name)
        if pid:
            res = subprocess.run(f"lsof -a -p {pid} -d cwd -Fn", shell=True, capture_output=True, text=True)
            actual_cwd = None
            for line in res.stdout.splitlines():
                if line.startswith("n"):
                    actual_cwd = line[1:]
            if actual_cwd:
                return actual_cwd.rstrip("/") == expected_cwd.rstrip("/")
        time.sleep(poll_interval)
    return False


def launch_claude_verified(tmux_name, worktree_path, claude_cmd_builder, retries=1):
    """Sends `claude_cmd_builder()`'s command to a freshly-created tmux
    session, then confirms via verify_claude_launched_in that the process
    actually started in `worktree_path` — not just that some claude process
    exists. On mismatch (the shell-race class of bug: cd got mangled,
    claude started in the wrong directory and can sit hung for hours with
    nothing detecting it), kills the pane and retries with a fresh
    tmux_new_session (which itself waits for shell readiness) up to
    `retries` times. Returns True if eventually verified, False if every
    attempt failed — callers should treat False as a real launch failure,
    not silently proceed as if the session is good. claude_cmd_builder is a
    zero-arg callable so a fresh command (e.g. containing a newly-minted
    session-id) can be built fresh on each retry, not reused stale."""
    for attempt in range(retries + 1):
        if attempt > 0:
            tmux_kill(tmux_name)
            tmux_new_session(tmux_name)
        tmux_send(tmux_name, claude_cmd_builder())
        if verify_claude_launched_in(tmux_name, worktree_path):
            return True
    return False


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
