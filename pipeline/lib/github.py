"""Best-effort lookup of an open PR's head branch for a Jira ticket key, via
`gh pr list --search`. Used as a stacking fallback when a blocker ticket
wasn't picked up by this pipeline (e.g. a human built its branch/PR
manually) but still has a discoverable open PR referencing the ticket key
in its title.
"""
from . import procs


def find_open_pr_branch(repo_path, ticket_key):
    """Returns the head branch name of an open PR whose title contains
    `ticket_key`, or None if no such PR exists (or gh isn't authenticated,
    or more than one candidate PR is found — ambiguous, refuse to guess)."""
    out = procs.sh(
        f'gh pr list --search "{ticket_key} in:title" --state open '
        f'--json number,headRefName,title',
        cwd=repo_path, check=False,
    )
    if not out:
        return None
    import json
    try:
        candidates = json.loads(out)
    except ValueError:
        return None
    if len(candidates) != 1:
        return None
    return candidates[0]["headRefName"]


def get_pr_comments(repo_path, pr_url):
    """Returns a PR's issue-level comments (author, body, createdAt), oldest
    first — same shape as jira_client.get_comments, so callers can treat
    both sources the same way. `pr_url` is a full GitHub PR URL (what the
    pipeline stores in state.pr_url); `gh pr view <url> --json comments`
    accepts a URL directly, no repo/number parsing needed. Returns [] on any
    failure (gh not authenticated, PR not found, network) — this is a
    best-effort secondary feedback source, never a reason to fail a ticket."""
    if not pr_url:
        return []
    out = procs.sh(f"gh pr view {procs.shlex.quote(pr_url)} --json comments", cwd=repo_path, check=False)
    if not out:
        return []
    import json
    try:
        return json.loads(out).get("comments", [])
    except ValueError:
        return []


def get_pr_details(repo_path, pr_url):
    """Returns the PR's current state for the auto-merge flow: base branch,
    mergeable/mergeStateStatus, labels, required-check rollup, reviews (in
    submission order), and requested reviewers. Returns None on any failure
    — callers should treat that as "can't act this cycle, try again later",
    not as a terminal condition."""
    if not pr_url:
        return None
    fields = "baseRefName,number,labels,mergeable,mergeStateStatus,statusCheckRollup,reviews,reviewRequests"
    out = procs.sh(f"gh pr view {procs.shlex.quote(pr_url)} --json {fields}", cwd=repo_path, check=False)
    if not out:
        return None
    import json
    try:
        return json.loads(out)
    except ValueError:
        return None


def comment_on_pr(repo_path, pr_url, body):
    """Posts an issue-level comment on the PR. Raises on failure — unlike
    the read helpers above, a caller triggering an action (e.g. `@bugbot
    run`, `/approve`) needs to know if it didn't actually go through."""
    procs.sh(f"gh pr comment {procs.shlex.quote(pr_url)} --body {procs.shlex.quote(body)}", cwd=repo_path)


def merge_pr(repo_path, pr_url):
    """Merges the PR with a real merge commit (never --squash, never
    --rebase — this pipeline's convention, matching how aidev PRs have
    always been merged here). Returns (True, output) on success, (False,
    error_text) on failure — never raises, since a failed merge is exactly
    the "stuck, escalate" case the auto-merge flow needs to detect and
    report, not crash on."""
    try:
        out = procs.sh(f"gh pr merge {procs.shlex.quote(pr_url)} --merge", cwd=repo_path)
        return True, out
    except RuntimeError as e:
        return False, str(e)
