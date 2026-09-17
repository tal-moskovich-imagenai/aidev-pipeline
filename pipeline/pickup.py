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
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import config, jira_client, state, procs


def slugify(key):
    return re.sub(r"[^a-zA-Z0-9_-]", "-", key)


def log(msg):
    print(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}")


def build_task_prompt(issue):
    key = issue["key"]
    summary = issue["fields"]["summary"]
    desc = jira_client.plain_description(issue)
    cfg = config.load()
    steps = "\n".join(f"{i+1}. Run the slash command: {s}" for i, s in enumerate(cfg["claude"]["post_steps"]))
    return f"""You are working on Jira ticket {key}: {summary}

Description:
{desc or '(no description provided)'}

Task:
- Implement the ticket end to end on this branch.
- Follow the repo's existing conventions (see AGENTS.md/CLAUDE.md if present).
- When the implementation is complete and working, run these steps in order:
{steps}
- Then stage and commit ALL changes with a clear, descriptive commit message
  that references {key}.
- Do not push or open a PR yourself; the orchestrator handles that next.

If at any point you are blocked and need clarification from a human (ambiguous
requirements, a decision you can't safely make on your own, missing access,
etc.), do NOT guess. Instead print a line in EXACTLY this format and then wait:
AIDEV_NEEDS_INPUT: <your question here, one line>
A human will reply as a comment on the Jira ticket; when the orchestrator
detects a reply it will paste it into this same session so you can continue.

When you are fully done and have committed, say exactly: AIDEV_TASK_COMPLETE
"""


def pickup_ticket(issue):
    cfg = config.load()
    key = issue["key"]
    if state.get(key):
        log(f"{key}: already tracked, skipping")
        return

    blockers = jira_client.get_blocking_issues(key)
    if blockers:
        blocker_list = ", ".join(f"{k} ({s})" for k, s in blockers)
        log(f"{key}: blocked by {blocker_list} — skipping")
        return

    labels = issue["fields"].get("labels", [])
    repo_path = config.repo_for(labels)
    worktree_root = cfg["worktree_root"]
    os.makedirs(worktree_root, exist_ok=True)

    slug = slugify(key)
    worktree_path = os.path.join(worktree_root, slug)
    branch = f"aidev/{slug}"
    tmux_name = f"aidev-{slug}"
    session_id = str(uuid.uuid4())

    log(f"{key}: creating worktree at {worktree_path} (branch {branch})")
    procs.sh(f"git worktree add {worktree_path} -b {branch}", cwd=repo_path, check=False)
    # If branch/worktree already exists from a previous failed attempt, reuse it.
    if not os.path.isdir(worktree_path):
        raise RuntimeError(f"{key}: failed to create worktree at {worktree_path}")

    if procs.tmux_session_exists(tmux_name):
        procs.tmux_kill(tmux_name)
    procs.tmux_new_session(tmux_name)

    prompt = build_task_prompt(issue)
    prompt_file = os.path.join(worktree_path, ".aidev_prompt.txt")
    with open(prompt_file, "w") as f:
        f.write(prompt)

    skip_perms = "--dangerously-skip-permissions" if cfg["claude"]["dangerously_skip_permissions"] else ""
    claude_cmd = (
        f"cd {worktree_path} && "
        f"claude --session-id {session_id} {skip_perms} "
        f"\"$(cat .aidev_prompt.txt)\""
    )
    procs.tmux_send(tmux_name, claude_cmd)

    state.insert(key, repo_path, worktree_path, branch, session_id, tmux_name, state="RUNNING")

    resume_cmd = f"cd {worktree_path} && claude --resume {session_id}"
    comment = (
        f"\U0001f916 aidev picked up this ticket.\n"
        f"Worktree: {worktree_path}\n"
        f"Branch: {branch}\n"
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


def main():
    cfg = config.load()
    jcfg = cfg["jira"]
    jql = f'labels = "{jcfg["label"]}" AND status = "{jcfg["todo_status"]}"'
    if jcfg.get("jql_extra"):
        jql += f" AND {jcfg['jql_extra']}"

    log(f"Polling: {jql}")
    issues = jira_client.search_issues(jql)
    log(f"Found {len(issues)} ticket(s) to pick up")

    for issue in issues:
        try:
            pickup_ticket(issue)
        except Exception as e:
            log(f"ERROR handling {issue.get('key')}: {e}")


if __name__ == "__main__":
    main()
