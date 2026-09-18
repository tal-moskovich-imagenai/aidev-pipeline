"""Runs `codex exec` as a non-critical second-opinion review pass.

Codex gets full workspace-write permissions (per the operator's choice — no
sandbox restriction) but is instructed, in the prompt only, to write a report
and never touch the tracked code. `run_codex_review` defends against a
misbehaving run anyway: anything Codex changes beyond the report file itself
is reverted with `git checkout` / untracked-file removal before returning,
so a prompt-following failure can't leak into the ticket's diff.

Every failure mode here (timeout, non-zero exit, codex not installed, out of
credits, malformed output) is non-fatal by design — the caller always gets a
best-effort (ok: bool, report_path_or_None, detail: str) and must proceed
without blocking the pipeline on Codex specifically.
"""
import glob
import os
import subprocess

from . import config


def _resolve_skill_paths():
    cfg = config.load()
    codex_cfg = cfg.get("codex") or {}
    paths = []
    for pattern in codex_cfg.get("skill_globs", []):
        matches = sorted(glob.glob(pattern))
        if matches:
            paths.append(matches[-1])  # highest version dir sorts last lexicographically for x.y.z
    return paths


def build_codex_review_prompt(key, summary, report_path, skill_paths):
    skill_block = "\n".join(f"  - {p}" for p in skill_paths) or "  (none found — proceed on general judgment)"
    return f"""You are a second-opinion code reviewer on Jira ticket {key}: {summary},
running as `codex exec` in this git worktree/branch alongside a primary
implementer (Claude Code) and an automated bot (Cursor Bugbot).

You have full read/write permissions in this sandbox, but your ONLY job here
is to WRITE A REPORT. Do not edit, create, delete, or move any tracked file
in this repository. Do not run `git commit`, `git add`, or anything that
changes the working tree's tracked content. The one file you may write is
the report path given below.

Apply the judgment criteria from these two skills (read them in full first):
{skill_block}

- The first is a readability pass: comments that should become names, split
  functions, KISS/DRY/module-separation issues, in the code that changed.
- The second is a correctness/convention review: read the repo's own
  conventions (AGENTS.md, CLAUDE.md, .agents/rules/*) and check the diff
  against them, plus general correctness concerns a careful reviewer would
  flag.

Determine the diff to review yourself (likely `git diff <base>...HEAD` where
base is the branch's true parent — check `git log` and any stacked-PR notes
in the worktree). Do not fetch anything from GitHub or run any `gh` command —
this is a local-diff-only pass, not the full /custom-review GitHub flow.

Write your findings to exactly this path, as markdown, and nothing else:
{report_path}

Use this structure:
# Codex Review — {key}
## Verdict: CLEAN | NEEDS_ATTENTION
## Findings
(one item per issue: `file:line`, severity, what's wrong, suggested fix —
 or "No findings." if genuinely clean)

When you are done writing the report, stop. Do not attempt anything else.
"""


def run_codex_review(ticket, key, summary, log=lambda *a, **k: None):
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

    skill_paths = _resolve_skill_paths()
    prompt = build_codex_review_prompt(key, summary, report_rel, skill_paths)

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
