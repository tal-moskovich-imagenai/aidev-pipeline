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
