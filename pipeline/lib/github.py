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
