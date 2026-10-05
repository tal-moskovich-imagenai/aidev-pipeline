#!/usr/bin/env python3
"""aidev dashboard — a small stdlib-only kanban-style web UI over the
pipeline's real state (DB + Jira + GitHub), with action buttons wired to
the exact same functions the pipeline itself (and every manual recovery
this session) uses: tag review, tag codex review, rework with a note, tag
auto-merge, merge, archive, recover a crashed session.

No third-party deps (Flask etc. aren't installed) — plain http.server +
a single HTML/JS page that polls the JSON API. Run from anywhere; it sets
config.CONFIG_PATH itself.

    python3 webui/server.py [--port 8765]
"""
import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PIPELINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PIPELINE_DIR)

DEPLOYED_CONFIG = "/Users/talmoskovich/jira-claude-pipeline/config.yaml"
from lib import config as cfgmod
cfgmod.CONFIG_PATH = DEPLOYED_CONFIG
from lib import state, jira_client, github, procs

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Simple in-process cache for the Jira/GitHub-heavy ticket list — these are
# real network calls (Jira REST, gh CLI) and the UI polls every few
# seconds; without this every poll would refetch every ticket's Jira issue
# and every open PR's GitHub state, which is slow and rate-limit-risky.
_CACHE = {"tickets": None, "at": 0}
_CACHE_TTL = 8


def _fetch_pr_summary(repo_path, pr_url):
    if not pr_url:
        return None
    details = github.get_pr_details(repo_path, pr_url)
    if not details:
        return {"error": "could not fetch PR details"}
    # De-dupe by check name, keeping only the latest run — GitHub's rollup
    # does not drop a superseded check run when a workflow re-triggers off
    # a label change (only a fresh commit reliably does), so a stale
    # FAILURE from before a `no-changelog` label was applied can sit right
    # alongside the fresh SKIPPED run for the same check name. Same fix as
    # monitor.py's _classify_checks.
    latest_by_name = {}
    for c in details.get("statusCheckRollup", []):
        name = c.get("name")
        if not name:
            continue
        ts = c.get("completedAt") or c.get("startedAt") or ""
        if name not in latest_by_name or ts > (latest_by_name[name].get("completedAt") or latest_by_name[name].get("startedAt") or ""):
            latest_by_name[name] = c
    red = []
    for c in latest_by_name.values():
        concl = (c.get("conclusion") or "").upper()
        if concl in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"):
            red.append(c.get("name"))
    return {
        "state": details.get("state"),
        "base": details.get("baseRefName"),
        "mergeable": details.get("mergeable"),
        "mergeStateStatus": details.get("mergeStateStatus"),
        "redChecks": red,
        "labels": [l.get("name") for l in details.get("labels", [])],
        "approved": any(r.get("state") == "APPROVED" for r in details.get("reviews", [])),
    }


def build_ticket_view(row, cfg, include_pr=True):
    key = row["ticket_key"]
    view = {
        "key": key,
        "dbState": row.get("state"),
        "stage": row.get("stage"),
        "prUrl": row.get("pr_url"),
        "repoPath": row.get("repo_path"),
        "worktreePath": row.get("worktree_path"),
        "tmuxSession": row.get("tmux_session"),
        "sessionId": row.get("session_id"),
        "updatedAt": row.get("updated_at"),
        "stuckQuestion": row.get("stuck_question"),
        "crashRetryCount": row.get("crash_retry_count"),
        "needsYou": bool(row.get("stuck_question")) or row.get("state") in ("FAILED", "STUCK"),
        "jira": None,
        "pr": None,
        "blockers": [],
    }
    try:
        issue = jira_client.get_issue(key, fields=["summary", "status", "labels", "parent"])
        parent = issue["fields"].get("parent")
        view["jira"] = {
            "summary": issue["fields"]["summary"],
            "status": issue["fields"]["status"]["name"],
            "labels": issue["fields"].get("labels", []),
            "link": f"{cfg['jira']['base_url']}/browse/{key}",
            "epicKey": parent.get("key") if parent else None,
            "epicLink": f"{cfg['jira']['base_url']}/browse/{parent.get('key')}" if parent else None,
        }
        try:
            blockers = jira_client.get_blocking_issues(key, review_status=cfg["jira"].get("review_status"))
            view["blockers"] = [
                {"key": bk, "status": bs, "hard": hard, "link": f"{cfg['jira']['base_url']}/browse/{bk}"}
                for bk, bs, hard in blockers
            ]
        except Exception:
            pass
    except Exception as e:
        view["jira"] = {"error": str(e)}
    if include_pr and row.get("pr_url"):
        try:
            view["pr"] = _fetch_pr_summary(row["repo_path"], row["pr_url"])
        except Exception as e:
            view["pr"] = {"error": str(e)}
    return view


def get_all_tickets(force=False):
    now = time.time()
    if not force and _CACHE["tickets"] is not None and (now - _CACHE["at"]) < _CACHE_TTL:
        return _CACHE["tickets"]
    cfg = cfgmod.load()
    with state.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM tickets WHERE state != 'ARCHIVED' ORDER BY updated_at DESC"
        ).fetchall()]
    tickets = [build_ticket_view(r, cfg) for r in rows]
    _CACHE["tickets"] = tickets
    _CACHE["at"] = now
    return tickets


def invalidate_cache():
    _CACHE["tickets"] = None


# --- Actions -----------------------------------------------------------
# Every action below reuses the exact same lib functions this session used
# manually all day (state.set_state, jira_client.transition_issue/add_label/
# remove_label/add_comment, github.merge_pr) — the UI is a thin wrapper,
# not a parallel implementation.

def action_tag_review(key):
    jira_client.add_label(key, "aidev-self-review")
    return f"{key}: tagged aidev-self-review"


def action_send_to_rework(key, note):
    cfg = cfgmod.load()
    jira_client.transition_issue(key, cfg["jira"]["in_progress_status"])
    if note:
        row = state.get(key) or {}
        pr_url = row.get("pr_url")
        comment = f"Rework request: {note}"
        if pr_url:
            try:
                github.comment_on_pr(row["repo_path"], pr_url, comment)
            except Exception as e:
                jira_client.add_comment(key, f"[aidev] (could not post to PR, posting here instead) {comment}\n\n{e}")
        else:
            jira_client.add_comment(key, f"[aidev] {comment}")
    return f"{key}: sent to rework" + (" with note" if note else "")


def action_tag_label(key, label):
    jira_client.add_label(key, label)
    return f"{key}: tagged {label}"


def action_untag_label(key, label):
    jira_client.remove_label(key, label)
    return f"{key}: removed {label}"


def action_merge_pr(key):
    row = state.get(key)
    if not row or not row.get("pr_url"):
        raise RuntimeError(f"{key}: no PR URL on record")
    # Real safety net, not just a UI-level disable: re-check the PR's actual
    # current mergeability right before attempting, so a stale cached view
    # can't trigger a `gh pr merge` that GitHub's own branch protection will
    # reject anyway — surface a clear reason instead of gh's raw CLI error.
    summary = _fetch_pr_summary(row["repo_path"], row["pr_url"])
    if summary and not summary.get("error"):
        if summary.get("redChecks"):
            raise RuntimeError(f"{key}: CI still red ({', '.join(summary['redChecks'])}) — not merging")
        if summary.get("mergeStateStatus") not in ("CLEAN", None) and not summary.get("approved"):
            raise RuntimeError(
                f"{key}: not approved and mergeStateStatus={summary.get('mergeStateStatus')} — "
                f"branch protection will likely reject this, not attempting"
            )
    ok, out = github.merge_pr(row["repo_path"], row["pr_url"])
    if not ok:
        raise RuntimeError(f"{key}: merge failed: {out}")
    try:
        jira_client.transition_issue(key, "Done")
    except Exception:
        pass
    return f"{key}: merged"


def action_archive(key):
    row = state.get(key)
    if row:
        import sys as _s
        _s.path.insert(0, DEPLOYED_CONFIG.rsplit("/", 1)[0])
        import monitor as _monitor  # deployed monitor.py, has cleanup_worktree
        try:
            _monitor.cleanup_worktree(row)
        except Exception:
            pass
    state.set_archived(key)
    return f"{key}: archived"


def action_recover_crash(key):
    """Same recovery pattern used manually all session: restore DB state to
    DONE (or RUNNING if still mid-stage) + aidev-done label, clear crash
    counter, so the next monitor cycle picks it back up cleanly."""
    row = state.get(key)
    if not row:
        raise RuntimeError(f"{key}: not tracked")
    stage = row.get("stage") or "implement"
    if stage.startswith("auto_merge"):
        state.set_state(key, "DONE", pr_url=row.get("pr_url"))
        state.set_stage(key, "auto_merge_recheck")
        jira_client.set_state_label(key, "aidev-done")
    else:
        state.set_state(key, "RUNNING")
        jira_client.set_state_label(key, "aidev-picked")
    state.clear_crash_retry_count(key)
    return f"{key}: recovered — next monitor cycle will relaunch"


ACTIONS = {
    "tag_review": lambda key, **kw: action_tag_review(key),
    "send_to_rework": lambda key, note="", **kw: action_send_to_rework(key, note),
    "tag_auto_merge": lambda key, **kw: action_tag_label(key, "aidev-auto-merge"),
    "untag_auto_merge": lambda key, **kw: action_untag_label(key, "aidev-auto-merge"),
    "tag_codex_review": lambda key, **kw: action_tag_label(key, "aidev-codex-review"),
    "merge_pr": lambda key, **kw: action_merge_pr(key),
    "archive": lambda key, **kw: action_archive(key),
    "recover_crash": lambda key, **kw: action_recover_crash(key),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep stdout clean; errors still print via traceback

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html")
        elif parsed.path == "/app.js":
            self._send_file(os.path.join(STATIC_DIR, "app.js"), "application/javascript")
        elif parsed.path == "/api/tickets":
            force = qs.get("force", ["0"])[0] == "1"
            try:
                self._send_json({"tickets": get_all_tickets(force=force)})
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": str(e)}, status=500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
            action = payload.get("action")
            key = payload.get("key")
            note = payload.get("note", "")
            if not action or not key:
                raise ValueError("action and key are required")
            fn = ACTIONS.get(action)
            if not fn:
                raise ValueError(f"unknown action: {action}")
            msg = fn(key, note=note)
            invalidate_cache()
            self._send_json({"ok": True, "message": msg})
        except Exception as e:
            traceback.print_exc()
            self._send_json({"ok": False, "error": str(e)}, status=400)


def main():
    port = 8765
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"aidev dashboard: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
