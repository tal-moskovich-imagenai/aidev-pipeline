import sqlite3
import contextlib
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
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);
"""

# Valid states: NEW -> RUNNING -> (STUCK <-> RUNNING)* -> POSTPROCESS -> PR_OPENED -> DONE  (or FAILED)


@contextlib.contextmanager
def db():
    cfg = config.load()
    conn = sqlite3.connect(cfg["state_db"])
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
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


def insert(ticket_key, repo_path, worktree_path, branch, session_id, tmux_session, state="NEW"):
    with db() as conn:
        conn.execute(
            "INSERT INTO tickets (ticket_key, repo_path, worktree_path, branch, session_id, tmux_session, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_key, repo_path, worktree_path, branch, session_id, tmux_session, state),
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


def set_stuck(ticket_key, question):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET state = 'STUCK', stuck_question = ?, updated_at = datetime('now') WHERE ticket_key = ?",
            (question, ticket_key),
        )


def clear_stuck(ticket_key):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET state = 'RUNNING', stuck_question = NULL, updated_at = datetime('now') WHERE ticket_key = ?",
            (ticket_key,),
        )


def set_last_comment_id(ticket_key, comment_id):
    with db() as conn:
        conn.execute(
            "UPDATE tickets SET last_comment_id = ? WHERE ticket_key = ?",
            (comment_id, ticket_key),
        )


def _migrate(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()}
    if "stuck_question" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN stuck_question TEXT")
    if "last_comment_id" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN last_comment_id TEXT")
