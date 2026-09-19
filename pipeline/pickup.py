#!/usr/bin/env python3
"""
aidev pickup — polls Jira for tickets labeled `aidev` in the configured
"to do" status, spins up a git worktree + tmux + Claude Code session per
ticket, and posts the worktree/session info back as a Jira comment.

Run this from cron (e.g. every 5 minutes):
    python3 pickup.py
"""
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import config, jira_client, state, procs, lockfile, github
from lib.pipelog import get_logger
from lib.soul import soul_section

log = get_logger("pickup")


def slugify(key):
    return re.sub(r"[^a-zA-Z0-9_-]", "-", key)


def mark_failed(key, reason):
    log(f"{key}: FAILED — {reason}")
    state.set_state(key, "FAILED")
    try:
        jira_client.add_comment(key, f"[aidev] Marked as failed: {reason}")
        jira_client.set_state_label(key, "aidev-stuck")
    except Exception as e:
        log(f"{key}: could not post failure comment: {e}")


def build_task_prompt(issue, stack_base_key=None):
    key = issue["key"]
    summary = issue["fields"]["summary"]
    desc = jira_client.plain_description(issue)
    stack_note = ""
    if stack_base_key:
        stack_note = f"""
## Stacked branch — important

This ticket is blocked by {stack_base_key}, which is still In Review (not
merged yet). Your branch was created ON TOP of {stack_base_key}'s branch, so
your diff will include {stack_base_key}'s changes until that PR merges — this
is expected and correct, not a mistake. Do not try to remove or revert
{stack_base_key}'s changes. When you open your PR, it will show as based on
{stack_base_key}'s branch; that's normal for a stacked PR and a human will
rebase once the base merges.
"""
    return f"""{soul_section()}You are working on Jira ticket {key}: {summary}
{stack_note}
Description:
{desc or '(no description provided)'}

## Shared context — read before doing anything else

The ticket description or comments above may reference "must read" material:
absolute file paths (e.g. under `.claude-code/` in some other worktree —
brainstorm docs, decisions, handoff notes shared across multiple related
tickets), Notion pages (use the `/notion` skill/slash-command to fetch them),
Figma links (the Figma MCP is connected — use it to inspect designs/frames
directly), Slack message links (the Slack MCP is connected — use it to fetch
the actual message/thread content, not just a raw URL fetch), or other links.

If any such references exist:
1. Read every one of them FULLY before starting implementation. They often
   contain decisions, constraints, or context essential to doing this right
   — do not skip past them or skim.
2. Any local `.md` file referenced this way is almost certainly shared by
   OTHER tickets too (a whole epic's worth of context can live in one doc).
   Treat it as a living document, not this ticket's private scratch space.
3. When you learn something new, make a decision, or finish a step that
   future readers (you on a later ticket, a human, or another agent) would
   need to know — APPEND it to the relevant file, in a clearly dated/labeled
   section. Never overwrite or delete existing content, never rewrite
   history, only add. Follow the file's existing structure/style if it has
   one (e.g. a "Decisions" table, a dated "## Decisions taken while
   implementing" section).
4. If no such references exist in this ticket, skip this section entirely —
   don't invent one.

Task:
- Implement the ticket end to end on this branch.
- Follow the repo's existing conventions (see AGENTS.md/CLAUDE.md if present).
- When the implementation is complete and working, stage and commit ALL
  changes with a clear, descriptive commit message that references {key}.
- Push the branch and open the PR yourself:
  `git push -u origin <branch>` then
  `gh pr create --base <the branch you branched from — check git log/CLAUDE.md
  for a stacked base> --title "..." --body "..."`.
  Write a REAL title and description from your own understanding of what you
  built and why — no placeholder like "automated by aidev". Follow this
  repo's own PR conventions if it has a template. A generic/empty PR
  description is not acceptable output for this ticket.
- Do NOT run /simplify, /custom-simplify, or /custom-review yet — those run
  in a follow-up pass after the PR exists (custom-review needs a real PR to
  tag and comment on).
- Do not create `.aidev_prompt.txt` or any other pipeline-internal file in
  the repo — if you notice one from the orchestrator's tooling already
  tracked in git, that's a bug; `git rm --cached` it rather than leaving it
  in your commit.

If at any point you are blocked and need clarification from a human (ambiguous
requirements, a decision you can't safely make on your own, missing access,
etc.), do NOT guess. Instead print a line in EXACTLY this format and then wait:
AIDEV_NEEDS_INPUT: <your question here, one line>
A human will reply as a comment on the Jira ticket; when the orchestrator
detects a reply it will paste it into this same session so you can continue.

When you are fully done — committed, pushed, and the PR is open with a real
title and description — say exactly: AIDEV_TASK_COMPLETE
"""


def pickup_ticket(issue):
    cfg = config.load()
    key = issue["key"]
    if state.get(key):
        log(f"{key}: already tracked, skipping")
        return False

    labels = issue["fields"].get("labels", [])
    repo_path = config.repo_for(labels)

    review_status = cfg["jira"].get("review_status")
    blockers = jira_client.get_blocking_issues(key, review_status=review_status)
    hard_blockers = [(k, s) for k, s, hard in blockers if hard]
    if hard_blockers:
        blocker_list = ", ".join(f"{k} ({s})" for k, s in hard_blockers)
        log(f"{key}: blocked by {blocker_list} — skipping")
        return False

    # Soft blockers: still open, but sitting at review_status (e.g. "In
    # Review", not merged). If stacking is enabled, proceed by branching off
    # the blocker's own branch instead of waiting for it to merge — mirrors
    # a human building PR N+1 on top of PR N before N lands.
    soft_blockers = [(k, s) for k, s, hard in blockers if not hard]
    stack_base_branch = None
    stack_base_key = None
    if soft_blockers:
        if not cfg["claude"].get("stack_on_review"):
            blocker_list = ", ".join(f"{k} ({s})" for k, s in soft_blockers)
            log(f"{key}: blocked by {blocker_list} (in review, stacking disabled) — skipping")
            return False
        if len(soft_blockers) > 1:
            log(f"{key}: multiple soft blockers {soft_blockers} — stacking only supports one, skipping")
            return False
        stack_base_key, _ = soft_blockers[0]

        # Prefer the pipeline's own state DB (exact, no ambiguity). Fall
        # back to searching GitHub for an open PR referencing the blocker's
        # ticket key — covers blockers built manually, outside this pipeline.
        blocker_ticket = state.get(stack_base_key)
        if blocker_ticket and blocker_ticket.get("branch"):
            stack_base_branch = blocker_ticket["branch"]
            log(f"{key}: stacking on {stack_base_key}'s branch {stack_base_branch} "
                f"(tracked locally, still In Review, not merged)")
        else:
            stack_base_branch = github.find_open_pr_branch(repo_path, stack_base_key)
            if stack_base_branch:
                log(f"{key}: stacking on {stack_base_key}'s branch {stack_base_branch} "
                    f"(found via gh pr list, not tracked locally, still In Review)")
            else:
                log(f"{key}: blocker {stack_base_key} is In Review but has no local record and "
                    f"no unambiguous open PR found via gh — cannot determine its branch, skipping")
                return False

    worktree_root = cfg["worktree_root"]
    os.makedirs(worktree_root, exist_ok=True)

    slug = slugify(key)
    worktree_path = os.path.join(worktree_root, slug)
    branch = f"aidev/{slug}"
    tmux_name = f"aidev-{slug}"
    session_id = str(uuid.uuid4())

    if stack_base_branch:
        log(f"{key}: creating worktree at {worktree_path} (branch {branch}, stacked on {stack_base_branch})")
        procs.sh(f"git worktree add {worktree_path} -b {branch} {stack_base_branch}", cwd=repo_path, check=False)
    else:
        log(f"{key}: creating worktree at {worktree_path} (branch {branch})")
        procs.sh(f"git worktree add {worktree_path} -b {branch}", cwd=repo_path, check=False)
    # If branch/worktree already exists from a previous failed attempt, reuse it.
    if not os.path.isdir(worktree_path):
        raise RuntimeError(f"{key}: failed to create worktree at {worktree_path}")
    if procs.tmux_session_exists(tmux_name):
        procs.tmux_kill(tmux_name)
    procs.tmux_new_session(tmux_name)

    prompt = build_task_prompt(issue, stack_base_key=stack_base_key)
    os.makedirs(os.path.join(worktree_path, ".claude-code"), exist_ok=True)
    prompt_file = os.path.join(worktree_path, ".claude-code", ".aidev_prompt.txt")
    with open(prompt_file, "w") as f:
        f.write(prompt)

    skip_perms = "--dangerously-skip-permissions" if cfg["claude"]["dangerously_skip_permissions"] else ""
    claude_cmd = (
        f"cd {worktree_path} && "
        f"DISABLE_AUTOUPDATER=1 claude --session-id {session_id} {skip_perms} "
        f"\"$(cat .claude-code/.aidev_prompt.txt)\""
    )
    procs.tmux_send(tmux_name, claude_cmd)

    state.insert(key, repo_path, worktree_path, branch, session_id, tmux_name, state="RUNNING",
                 stacked_on=stack_base_key, stacked_on_branch=stack_base_branch)

    resume_cmd = f"cd {worktree_path} && claude --resume {session_id}"
    stack_note = f"\nStacked on: {stack_base_key} (still In Review — this branch will need rebasing once it merges)\n" if stack_base_key else ""
    comment = (
        f"\U0001f916 aidev picked up this ticket.\n"
        f"Worktree: {worktree_path}\n"
        f"Branch: {branch}\n"
        f"{stack_note}"
        f"Session ID: {session_id}\n"
        f"tmux: tmux attach -t {tmux_name}\n"
        f"Resume yourself: {resume_cmd}\n"
    )
    jira_client.add_comment(key, comment)
    try:
        jira_client.transition_issue(key, cfg["jira"]["in_progress_status"])
    except RuntimeError as e:
        log(f"{key}: transition warning: {e}")
    try:
        jira_client.set_state_label(key, "aidev-picked")
    except Exception as e:
        log(f"{key}: label warning: {e}")

    log(f"{key}: launched, session {session_id}")
    return True


def main():
    try:
        with lockfile.Lock("pickup"):
            _run()
    except lockfile.LockHeld as e:
        log(f"skip run: {e}")


def _run():
    cfg = config.load()
    jcfg = cfg["jira"]
    max_concurrent = cfg["claude"].get("max_concurrent")

    if max_concurrent:
        active = state.count_active()
        free_slots = max_concurrent - active
        log(f"Concurrency: {active}/{max_concurrent} slots occupied, {max(0, free_slots)} free")
        if free_slots <= 0:
            log("No free slots — skipping pickup this run")
            return
    else:
        free_slots = None

    jql = f'labels = "{jcfg["label"]}" AND status = "{jcfg["todo_status"]}"'
    if jcfg.get("jql_extra"):
        jql += f" AND {jcfg['jql_extra']}"

    log(f"Polling: {jql}")
    issues = jira_client.search_issues(jql)
    log(f"Found {len(issues)} ticket(s) to pick up")

    picked_up = 0
    for issue in issues:
        if free_slots is not None and picked_up >= free_slots:
            remaining = len(issues) - picked_up
            log(f"Concurrency cap reached — deferring {remaining} remaining ticket(s) to next run")
            break
        try:
            if pickup_ticket(issue):
                picked_up += 1
        except Exception as e:
            log(f"ERROR handling {issue.get('key')}: {e}")


if __name__ == "__main__":
    main()
