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
from lib.soul import soul_section, jira_live_fetch_note

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
    cfg = config.load()
    base_url = cfg["jira"]["base_url"]
    review_skill = config.review_skill()
    link = f"{base_url}/browse/{key}"
    live_fetch_note = jira_live_fetch_note(key, link)
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

You do **not** need to read {stack_base_key}'s full diff or PR description up
front. Check its section in the shared epic doc first (see below) — pull its
actual diff or PR (`gh pr view <branch>` / `git diff`) only if that section's
bullets don't answer a specific question you have about what it changed.

Also check whether anything is already stacked on THIS ticket — another open
ticket/PR that lists {key} as its own blocker. If so, be aware your changes
here can affect that work too (mention it in your PR description if it's
relevant), since it's relying on whatever you land.
"""
    cross_repo_note = """
## Check your blockers

Look at this ticket's Jira blockers (either direction). If a blocker is in
a different repo, it's a soft blocker that may be running in parallel with
you right now — check its status and branch yourself and make sure you're
aligned with it (shared interface, API shape, whatever the two sides hand
off) before assuming anything about it.
"""
    return f"""{soul_section()}You are working on Jira ticket {key}: {summary}
Link: {link}
{stack_note}
{cross_repo_note}
{live_fetch_note}
## Shared context — read before doing anything else, and lives in the repo, not the worktree

The ticket description or comments you just fetched may reference "must
read" material:
file paths (brainstorm docs, decisions, handoff notes shared across multiple
related tickets), Notion pages (use the `/notion` skill/slash-command to
fetch them), Figma links (the Figma MCP is connected — use it to inspect
designs/frames directly), Slack message links (the Slack MCP is connected —
use it to fetch the actual message/thread content, not just a raw URL
fetch), or other links.

**Where shared `.claude-code/` files actually live:** the canonical copy of
any cross-ticket doc belongs in the **repo's own `.claude-code/`** directory
(gitignored, next to the repo root — not inside a worktree). A worktree gets
`git worktree remove --force`'d the moment its ticket reaches a terminal Jira
status, which silently deletes anything that only ever lived there — while
other tickets in the same epic may still be running and expecting to read
it. You may draft or scratch inside your own worktree's `.claude-code/`
while working, but before you finish, make sure the durable version is
synced to the **repo root's** `.claude-code/` (copy it there if it isn't
already), not left only in your soon-to-be-deleted worktree. If a referenced
path is under some other worktree that no longer exists, treat that as data
loss worth flagging, not something to silently re-derive.

If any such references exist:
1. Read every one of them FULLY before starting implementation (this applies
   to ticket-specific docs/Notion/Figma/Slack links — the narrower
   on-demand exception above is only about an *ancestor* stacked ticket's
   own diff/PR, not these). They often contain decisions, constraints, or
   context essential to doing this right — do not skip past them or skim.
2. Any local `.md` file referenced this way is almost certainly shared by
   OTHER tickets too (a whole epic's worth of context can live in one doc).
   Treat it as a living document, not this ticket's private scratch space.
3. When you learn something new, make a decision, or finish a step that
   future readers (you on a later ticket, a human, or another agent) would
   need to know — write it to the relevant file **in the repo root's
   `.claude-code/`**, as your own section: `## {key}` followed by up to 10
   short bullets (what changed, why, anything the next ticket needs to
   know). If this ticket already has a `## {key}` section in that doc
   (e.g. from an earlier rework pass), **overwrite that whole section in
   place** — don't append a second one; this is what keeps the doc bounded
   across a long epic, not a per-bullet cap by itself. Other tickets'
   sections are still never touched or rewritten by you, only your own.
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
- Include a real, clickable link to the Jira ticket in the PR body — not
  just the bare ticket key in prose or the title. A key by itself doesn't
  link anywhere on GitHub; incident/traceability review needs to jump
  GitHub → Jira with one click, and the PR title alone isn't reliable for
  this (not every PR titles itself with the key). Use: {link}
- State in the PR body which Jira source you actually worked from: either
  "Jira context: live-fetched via <tool>" if you independently re-fetched
  the ticket per the note above, or "Jira context: pipeline's embedded
  snapshot only (could not live-fetch: <reason>)" if you couldn't. Don't
  skip this line — a reviewer needs to know which one happened, not just
  that Jira context existed somewhere.
- Do NOT run {review_skill} yet — it runs in a
  follow-up pass after the PR exists (its review needs a real PR to
  tag and comment on).
- Do not create `.aidev_prompt.txt` or any other pipeline-internal file in
  the repo — if you notice one from the orchestrator's tooling already
  tracked in git, that's a bug; `git rm --cached` it rather than leaving it
  in your commit.

If at any point you hit a decision that genuinely needs a human (per the
"How much to decide vs. ask" and "`needs_input` fires at most once per
ticket" sections above), do NOT stop and ask immediately — research it, form
a suggested answer, keep working with that as your provisional assumption,
and log it to `.claude-code/decisions-<TICKET>.md`. Only write status.json's
`needs_input` update once, at the very end, batching every open question
together, as described above.

When you are fully done — committed, pushed, and the PR is open with a real
title and description — write status.json's state to "complete".
"""


def pickup_ticket(issue):
    cfg = config.load()
    key = issue["key"]
    existing = state.get(key)
    if existing and existing.get("state") != "ARCHIVED":
        log(f"{key}: already tracked, skipping")
        return False
    # An ARCHIVED row means a prior attempt is fully wound down (worktree
    # removed, branch deleted) and the ticket has since been reset to New —
    # e.g. a wrong-repo pickup that was cleaned up and corrected via a repo:
    # label fix. Insert below correctly overwrites the old row for the same
    # key rather than skipping. Caught live: RND-14736 was archived and
    # reset to New specifically to be re-picked up against the corrected
    # repo, but sat untouched forever because this check treated ANY row —
    # including an already-archived one — as "still tracked."

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
    #
    # This whole "stack instead of wait" rationale only applies when the
    # blocker's code lives in the SAME repo — there is no branch to stack
    # on, and no real code dependency to wait for, when the blocker is in a
    # different repo (a Jira "blocks" link has no notion of repo). Such a
    # cross-repo soft blocker is dropped from consideration entirely here —
    # not skipped, not stacked on, just not blocking — rather than treated
    # as "in review, can't determine branch, skip." Caught live: RND-14828
    # (submitter-app-electron) was soft-blocked by RND-14840 (app-web-server,
    # itself In Review) and sat skipped every cycle even though nothing
    # about RND-14840 being in review has any bearing on RND-14828's own
    # ability to proceed against master right now.
    all_soft_blockers = [(k, s) for k, s, hard in blockers if not hard]
    soft_blockers = []
    for k, s in all_soft_blockers:
        blocker_ticket = state.get(k)
        if blocker_ticket:
            blocker_repo = blocker_ticket.get("repo_path")
        else:
            try:
                blocker_issue = jira_client.get_issue(k, fields=["labels"])
                blocker_repo = config.repo_for(blocker_issue["fields"].get("labels", []))
            except Exception as e:
                log(f"{key}: could not resolve blocker {k}'s repo, treating as same-repo "
                    f"(conservative — will still try to stack): {e}")
                blocker_repo = repo_path
        if blocker_repo != repo_path:
            log(f"{key}: blocker {k} ({s}) lives in a different repo ({blocker_repo} vs "
                f"{repo_path}) — no code dependency here, not blocking")
            continue
        soft_blockers.append((k, s))

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
        # Must also confirm the blocker lives in the SAME repo as this
        # ticket — a Jira "blocks" link has no notion of repo, so two
        # tickets can be linked while their actual code lives in entirely
        # separate repos (e.g. a BE ticket blocking an unrelated FE one).
        # Stacking a branch in repo A on top of a branch that only exists in
        # repo B's git history is meaningless and fails at worktree-creation
        # time. Caught live: RND-14828 (submitter-app-electron) was blocked
        # by RND-14840 (app-web-server) — pickup tried `git worktree add`
        # against `origin/aidev/RND-14840`, a branch that only exists in the
        # other repo's remote, failed every cycle for 35+ minutes, and
        # crashed out of the whole pickup batch before ever reaching the
        # other 2 waiting tickets that cycle.
        blocker_ticket = state.get(stack_base_key)
        if blocker_ticket and blocker_ticket.get("branch") and blocker_ticket.get("repo_path") == repo_path:
            stack_base_branch = blocker_ticket["branch"]
            log(f"{key}: stacking on {stack_base_key}'s branch {stack_base_branch} "
                f"(tracked locally, still In Review, not merged)")
        else:
            if blocker_ticket and blocker_ticket.get("branch") and blocker_ticket.get("repo_path") != repo_path:
                log(f"{key}: blocker {stack_base_key} is tracked locally but lives in a "
                    f"different repo ({blocker_ticket.get('repo_path')} vs {repo_path}) — "
                    f"cannot stack across repos, falling back to gh search")
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
    model = (cfg["claude"].get("models") or {}).get("implement")
    model_flag = f"--model {model} " if model else ""
    def _build_claude_cmd():
        return (
            f"cd {worktree_path} && "
            f"DISABLE_AUTOUPDATER=1 claude --session-id {session_id} {model_flag}{skip_perms} "
            f"\"$(cat .claude-code/.aidev_prompt.txt)\""
        )
    if not procs.launch_claude_verified(tmux_name, worktree_path, _build_claude_cmd):
        raise RuntimeError(
            f"{key}: claude never verifiably started in {worktree_path} after retrying — "
            f"likely the shell-startup race (see procs.py); check the pane manually"
        )

    if existing and existing.get("state") == "ARCHIVED":
        state.delete_archived(key)
    state.insert(key, repo_path, worktree_path, branch, session_id, tmux_name, state="RUNNING",
                 stacked_on=stack_base_key, stacked_on_branch=stack_base_branch)
    state.record_stage_transition(key, session_id, "implement", model)

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
    if cfg["claude"].get("auto_review", True):
        try:
            jira_client.add_label(key, "aidev-self-review")
        except Exception as e:
            log(f"{key}: could not apply aidev-self-review label: {e}")
    board_id = cfg["jira"].get("board_id")
    if board_id:
        try:
            sprint_id = jira_client.get_active_sprint_id(board_id)
            if sprint_id:
                jira_client.add_issue_to_sprint(sprint_id, key)
            else:
                log(f"{key}: no single active sprint on board {board_id} — leaving out of sprint")
        except Exception as e:
            log(f"{key}: sprint-add warning: {e}")

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

    jql = f'labels = "{jcfg["label"]}" AND status = "{jcfg["todo_status"]}" AND assignee = currentUser()'
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
