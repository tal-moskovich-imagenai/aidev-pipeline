#!/usr/bin/env python3
"""aidev status — quick view of all tracked tickets."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import state


def main():
    import sqlite3
    with state.db() as conn:
        rows = conn.execute("SELECT * FROM tickets ORDER BY updated_at DESC").fetchall()
    if not rows:
        print("No tracked tickets.")
        return
    for r in rows:
        r = dict(r)
        print(f"{r['ticket_key']:<12} {r['state']:<12} branch={r['branch']:<30} session={r['session_id']}")
        print(f"             worktree={r['worktree_path']}")
        if r["pr_url"]:
            print(f"             pr={r['pr_url']}")
        print()


if __name__ == "__main__":
    main()
