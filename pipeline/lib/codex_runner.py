"""Runs `codex exec` as a non-critical second-opinion pass using the same
review skill Claude runs (`claude.review_skill` in config.yaml, invoked in
Codex as `$<name>`), against the PR that already exists for the ticket.

Codex has the skill installed natively (~/.codex/config.toml:
`plugins."<name>@imagen-skills"`, same marketplace repo as Claude's plugin
cache). Because the skill fixes, commits and pushes on its own, Codex is no
longer report-only; it still writes a short report so the caller can tell
whether anything was left for a human, and `_revert_stray_changes` still
discards uncommitted leftovers beyond that report.

Every failure mode here (timeout, non-zero exit, codex not installed, out of
credits, malformed output) is non-fatal by design — the caller always gets a
best-effort (ok: bool, report_path_or_None, detail: str) and must proceed
without blocking the pipeline on Codex specifically.
"""
import os
import subprocess

from . import config
from .soul import soul_section


def build_codex_review_prompt(key, summary, pr_url, report_path):
    skill = config.review_skill().lstrip("/")
    return f"""{soul_section()}You are a second-opinion reviewer on Jira ticket {key}: {summary},
running as `codex exec` in this git worktree/branch alongside a primary
implementer (Claude Code) and an automated bot (Cursor Bugbot).

The implementation is already committed and a PR is already open: {pr_url}
Do NOT create a new branch or a second PR — the skill's commit/open-PR steps
must reuse this worktree, branch and PR. Claude has already run the same
skill once on this PR, so focus on what it may have missed. If you need
ticket context beyond the above, read the PR description and the repo's own
conventions (CLAUDE.md, AGENTS.md, .agents/rules/*).

Run the `{skill}` skill with Codex's own syntax:

${skill}

Follow its documented procedure step by step. If invoking it this way doesn't
work in this session, read its instruction file yourself and follow it
manually. This is a headless run: skip any demo-video step and say so.

When it finishes, write a short markdown report to exactly this path, and
nothing else beyond what the skill itself does:
{report_path}

Use this structure:
# Codex Review — {key}
## Verdict: CLEAN | NEEDS_ATTENTION
## Fixed automatically
(what the skill fixed and pushed, with commit SHAs — or "Nothing.")
## Needs your call
(the skill's unresolved items, one per line with `file:line` and the reason —
 or "Nothing.")

Verdict is CLEAN only if "Needs your call" is empty and CI is green or
unaffected. When the report is written, stop.
"""


def run_codex_review(ticket, key, summary, pr_url, log=lambda *a, **k: None):
    """Runs codex exec in the ticket's worktree. Returns (ok, report_path,
    detail). ok=False means Codex is unavailable/failed for any reason —
    callers must treat this as skip-and-continue, never as a pipeline
    failure. Reverts any stray working-tree changes Codex made before
    returning, regardless of outcome."""
    cfg = config.load()
    codex_cfg = cfg.get("codex") or {}
    if not codex_cfg.get("enabled", True):
        return False, None, "codex review disabled in config"

    worktree_path = ticket["worktree_path"]
    timeout = codex_cfg.get("timeout_seconds", 1800)
    sandbox = codex_cfg.get("sandbox", "workspace-write")
    report_dir = codex_cfg.get("report_dir", ".claude-code")
    report_rel = os.path.join(report_dir, f"codex-review-{key}.md")
    report_abs = os.path.join(worktree_path, report_rel)

    os.makedirs(os.path.dirname(report_abs), exist_ok=True)
    if os.path.exists(report_abs):
        os.remove(report_abs)  # stale report from a previous round shouldn't look like a fresh one

    prompt = build_codex_review_prompt(key, summary, pr_url, report_rel)

    try:
        result = subprocess.run(
            ["codex", "exec", "--sandbox", sandbox, prompt],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, None, "codex CLI not installed"
    except subprocess.TimeoutExpired:
        _revert_stray_changes(worktree_path, report_rel, log, key)
        return False, None, f"codex exec timed out after {timeout}s"
    except Exception as e:
        _revert_stray_changes(worktree_path, report_rel, log, key)
        return False, None, f"codex exec raised: {e}"

    _revert_stray_changes(worktree_path, report_rel, log, key)

    if result.returncode != 0:
        tail = (result.stdout or result.stderr or "")[-500:]
        return False, None, f"codex exec exited {result.returncode}: {tail}"

    if not os.path.exists(report_abs):
        tail = (result.stdout or "")[-500:]
        return False, None, f"codex exec finished but wrote no report; last output: {tail}"

    return True, report_rel, "ok"


def _revert_stray_changes(worktree_path, keep_rel_path, log, key):
    """Codex has full write permissions by design (per operator choice) —
    this is the safety net for the case it ignores the prompt and edits
    tracked code anyway. Keeps only the report file; reverts/removes
    everything else so a misbehaving run can't leak into the ticket's diff."""
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=worktree_path,
        capture_output=True, text=True, check=False,
    ).stdout
    tracked_changed, untracked_new = [], []
    for line in status.splitlines():
        code, path = line[:2], line[3:]
        if path == keep_rel_path:
            continue
        (untracked_new if code.strip() == "??" else tracked_changed).append(path)
    if not tracked_changed and not untracked_new:
        return
    log(f"{key}: codex review touched {len(tracked_changed) + len(untracked_new)} "
        f"file(s) beyond its report — reverting")
    if tracked_changed:
        subprocess.run(["git", "checkout", "--", *tracked_changed], cwd=worktree_path,
                        capture_output=True, check=False)
    if untracked_new:
        subprocess.run(["git", "clean", "-f", "--", *untracked_new], cwd=worktree_path,
                        capture_output=True, check=False)
