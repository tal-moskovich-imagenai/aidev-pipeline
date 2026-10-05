import sqlite3
import contextlib
from datetime import datetime
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_key       TEXT PRIMARY KEY,
    repo_path        TEXT NOT NULL,
    worktree_path    TEXT NOT NULL,
    branch           TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    tmux_session     TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'NEW',
    pr_url           TEXT,
    stuck_question   TEXT,
    last_comment_id  TEXT,
    running_since    TEXT,
    stacked_on       TEXT,
    stacked_on_branch TEXT,
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stage_transitions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key   TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    stage        TEXT NOT NULL,
    model        TEXT,
    started_at   TEXT DEFAULT (datetime('now'))
);
"""

# Valid states: NEW -> RUNNING -> (STUCK <-> RUNNING)* -> POSTPROCESS -> PR_OPENED -> DONE  (or FAILED)


@contextlib.contextmanager
def db():
    cfg = config.load()
    conn = sqlite3.connect(cfg["state_db"])
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get(ticket_key):
    with db() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE ticket_key = ?", (ticket_key,)).fetchone()
        return dict(row) if row else None


def all_in_state(state):
    with db() as conn:
        rows = conn.execute("SELECT * FROM tickets WHERE state = ?", (state,)).fetchall()
        return [dict(r) for r in rows]


ACTIVE_STATES = ("NEW", "RUNNING", "STUCK", "POSTPROCESS", "PR_OPENED")
CLEANUP_CANDIDATE_STATES = ("RUNNING", "STUCK", "POSTPROCESS", "PR_OPENED", "DONE", "FAILED")


def count_active():
    """Tickets currently occupying a Claude Code slot (not yet archived or
    permanently failed). Used to cap concurrent pickups. DONE is deliberately
    excluded: a DONE ticket has already handed off to human review (In
    Review + aidev-done in Jira) — no Claude session or worktree work is
    happening for it, so it shouldn't block a new ticket from being picked
    up. It only occupies a rework slot again if a human reopens it, at
    which point reopen_for_rework moves it back to RUNNING and it counts."""
    with db() as conn:
        placeholders = ",".join("?" * len(ACTIVE_STATES))
        row = conn.execute(
            f"SELECT COUNT(*) as c FROM tickets WHERE state IN ({placeholders})", ACTIVE_STATES
        ).fetchone()
        return row["c"]


def all_cleanup_candidates():
    """Tickets whose worktree may still exist on disk — checked against Jira
    each run to see if they've reached a terminal status and can be archived."""
    with db() as conn:
        placeholders = ",".join("?" * len(CLEANUP_CANDIDATE_STATES))
        rows = conn.execute(
            f"SELECT * FROM tickets WHERE state IN ({placeholders})", CLEANUP_CANDIDATE_STATES
        ).fetchall()
        return [dict(r) for r in rows]


def set_archived(ticket_key):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET state = 'ARCHIVED', updated_at = datetime('now') WHERE ticket_key = ?",
            (ticket_key,),
        )


def delete_archived(ticket_key):
    """Removes an ARCHIVED row entirely so the ticket can be picked up
    fresh (e.g. a wrong-repo pickup that was cleaned up and corrected).
    `ticket_key` is the table's PRIMARY KEY, so a second insert() for the
    same key without this first would crash on a UNIQUE constraint
    violation — deliberately scoped to ARCHIVED only, never call this on a
    row that might still be RUNNING/DONE/etc."""
    with db() as conn:
        conn.execute(
            "DELETE FROM tickets WHERE ticket_key = ? AND state = 'ARCHIVED'",
            (ticket_key,),
        )


def insert(ticket_key, repo_path, worktree_path, branch, session_id, tmux_session, state="NEW", stacked_on=None, stacked_on_branch=None):
    with db() as conn:
        conn.execute(
            "INSERT INTO tickets (ticket_key, repo_path, worktree_path, branch, session_id, tmux_session, state, running_since, stacked_on, stacked_on_branch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)",
            (ticket_key, repo_path, worktree_path, branch, session_id, tmux_session, state, stacked_on, stacked_on_branch),
        )


def set_state(ticket_key, state, pr_url=None):
    with db() as conn:
        if pr_url is not None:
            conn.execute(
                "UPDATE tickets SET state = ?, pr_url = ?, updated_at = datetime('now') WHERE ticket_key = ?",
                (state, pr_url, ticket_key),
            )
        else:
            conn.execute(
                "UPDATE tickets SET state = ?, updated_at = datetime('now') WHERE ticket_key = ?",
                (state, ticket_key),
            )


def reopen_for_rework(ticket_key):
    """DONE -> RUNNING, resets running_since and clears last_comment_id so the
    rework comment is picked up as fresh context."""
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET state = 'RUNNING', running_since = datetime('now'), "
            "last_comment_id = NULL, updated_at = datetime('now') WHERE ticket_key = ?",
            (ticket_key,),
        )


def set_stuck(ticket_key, question):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET state = 'STUCK', stuck_question = ?, updated_at = datetime('now') WHERE ticket_key = ?",
            (question, ticket_key),
        )


def clear_stuck(ticket_key):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET state = 'RUNNING', stuck_question = NULL, running_since = datetime('now'), "
            "updated_at = datetime('now') WHERE ticket_key = ?",
            (ticket_key,),
        )


def set_last_comment_id(ticket_key, comment_id):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET last_comment_id = ? WHERE ticket_key = ?",
            (comment_id, ticket_key),
        )


def set_session_id(ticket_key, session_id):
    """Persists a freshly-minted session_id onto the ticket row — used when
    a context-exhaustion handoff starts a genuinely new session (fresh
    --session-id, not --resume) rather than continuing the old one, so every
    later relaunch (crash-retry, rework, review stages) --resumes the new
    session and not the one that was deliberately retired."""
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET session_id = ?, updated_at = datetime('now') WHERE ticket_key = ?",
            (session_id, ticket_key),
        )


def _migrate(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()}
    if "stuck_question" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN stuck_question TEXT")
    if "last_comment_id" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN last_comment_id TEXT")
    if "running_since" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN running_since TEXT")
    if "stacked_on" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN stacked_on TEXT")
    if "stacked_on_branch" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN stacked_on_branch TEXT")
    if "postprocess_attempts" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN postprocess_attempts INTEGER NOT NULL DEFAULT 0")
    if "last_postprocess_error" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN last_postprocess_error TEXT")
    if "stage" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN stage TEXT NOT NULL DEFAULT 'implement'")
    if "last_bugbot_trigger_sha" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN last_bugbot_trigger_sha TEXT")
    if "last_bugbot_trigger_at" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN last_bugbot_trigger_at TEXT")
    if "last_reviewed_sha" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN last_reviewed_sha TEXT")
    if "last_feedback_checked_sha" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN last_feedback_checked_sha TEXT")
    if "crash_retry_count" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN crash_retry_count INTEGER NOT NULL DEFAULT 0")


def set_last_reviewed_sha(ticket_key, sha):
    """Records the exact commit SHA that a genuine /custom-review pass just
    ran against (read directly from git, not parsed out of Claude's own
    free-text PR comment — that parsing was a real, live bug: Claude wrote
    'current HEAD 6d162f4' instead of 'commit 6d162f4' once and the regex
    silently stopped matching forever, causing a 34-cycle self-review loop
    on RND-14813/PR #5759 until max_running_hours finally killed it).
    set_last_reviewed_sha is called right after a stage that actually runs
    /custom-review (self_review, auto_merge_recheck) pushes its changes —
    the git HEAD at that moment IS the reviewed commit, no text-parsing
    needed."""
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET last_reviewed_sha = ? WHERE ticket_key = ?",
            (sha, ticket_key),
        )


def set_last_feedback_checked_sha(ticket_key, sha):
    """Records the commit SHA at which the auto-merge sequence last had
    Claude read every current PR comment/review (human feedback, or a
    Bugbot review from before this pass) and judge whether anything real
    is still unresolved — see check_new_pr_feedback in monitor.py. Keyed
    to a SHA, not a boolean, so a later push (new commits) re-triggers the
    check rather than trusting a stale 'already checked' flag forever."""
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET last_feedback_checked_sha = ? WHERE ticket_key = ?",
            (sha, ticket_key),
        )


def set_last_bugbot_trigger_sha(ticket_key, sha):
    """Records the head commit SHA we last sent '@bugbot run' for, plus the
    timestamp, so process_auto_merge_ticket doesn't re-trigger a fresh
    Bugbot run every monitor cycle while waiting for the same one to come
    back — Bugbot can take several minutes, and there is no reason to ask
    twice for the same commit (caught live on PR #5698/#5700: repeated
    '@bugbot run' comments piling up across many cycles for what was still
    the same unreviewed head). The timestamp lets the caller re-trigger
    after a timeout if Bugbot itself never comes back (errored, rate
    limited) rather than waiting forever on a SHA match alone."""
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET last_bugbot_trigger_sha = ?, last_bugbot_trigger_at = datetime('now') "
            "WHERE ticket_key = ?",
            (sha, ticket_key),
        )


def set_stage(ticket_key, stage):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET stage = ?, updated_at = datetime('now') WHERE ticket_key = ?",
            (stage, ticket_key),
        )


def record_stage_transition(ticket_key, session_id, stage, model=None):
    """Append-only log of every stage transition — session_id + stage +
    model + timestamp — so a later ccusage session record can be
    cross-referenced back to a specific pipeline stage (Step J). Never
    updates/deletes existing rows; a ticket's full stage history is just
    every row with its ticket_key, in insertion order."""
    with db() as conn:
        conn.execute(
            "INSERT INTO stage_transitions (ticket_key, session_id, stage, model) VALUES (?, ?, ?, ?)",
            (ticket_key, session_id, stage, model),
        )


def record_postprocess_failure(ticket_key, error):
    """Transient PR-step failure: bump the retry counter and go back to
    POSTPROCESS (not FAILED) so the next monitor.py run retries — the work is
    already committed, no tmux/Claude session needed for a retry."""
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET state = 'POSTPROCESS', postprocess_attempts = postprocess_attempts + 1, "
            "last_postprocess_error = ?, updated_at = datetime('now') WHERE ticket_key = ?",
            (str(error), ticket_key),
        )


def clear_postprocess_failure(ticket_key):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET postprocess_attempts = 0, last_postprocess_error = NULL WHERE ticket_key = ?",
            (ticket_key,),
        )


def bump_crash_retry_count(ticket_key):
    """Returns the count AFTER incrementing — callers compare this against
    their own retry limit before deciding to auto-relaunch vs. give up."""
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET crash_retry_count = crash_retry_count + 1 WHERE ticket_key = ?",
            (ticket_key,),
        )
        row = conn.execute(
            "SELECT crash_retry_count FROM tickets WHERE ticket_key = ?", (ticket_key,)
        ).fetchone()
        return row["crash_retry_count"] if row else 0


def clear_crash_retry_count(ticket_key):
    """Called once a ticket makes real forward progress (a fresh status.json
    update, or reaching POSTPROCESS/DONE) — resets the counter so a later,
    unrelated crash still gets its own full retry budget rather than
    inheriting an old ticket's exhausted count."""
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET crash_retry_count = 0 WHERE ticket_key = ?",
            (ticket_key,),
        )


def seconds_running(ticket):
    """Returns elapsed seconds since running_since, or None if unset/unparseable."""
    return _seconds_since(ticket.get("running_since"))


def _seconds_since(timestamp_str):
    if not timestamp_str:
        return None
    try:
        started = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (datetime.utcnow() - started).total_seconds()
