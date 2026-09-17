# aidev-pipeline

Jira → Claude Code autonomous development pipeline. Tag a Jira ticket
`aidev`; the pipeline creates a git worktree, runs Claude Code end-to-end
(implement → review → commit → PR), and reports back on the ticket —
including asking for clarification via Jira comments when genuinely stuck.

## Repo layout

```
pipeline/               the actual orchestrator (Python, stdlib-only + PyYAML)
  lib/
    config.py           loads config.yaml
    jira_client.py      Jira Cloud REST v3 client (urllib, Basic auth via Keychain)
    state.py             SQLite ticket-tracking state machine
    procs.py             tmux + shell helpers
  pickup.py              polls Jira, launches worktree + tmux + Claude Code session
  monitor.py              watches running/stuck sessions, opens PRs, relays replies
  status.py               CLI status table
  config.yaml.example     copy to config.yaml (gitignored) and fill in

skills/
  aidev-jira-tickets/     Claude Code skill: how to write tickets for this pipeline
                          (symlinked into ~/.claude/skills/ for global availability)
```

## Setup (per machine)

1. Clone this repo, e.g. to `~/Documents/GitHub/aidev-pipeline`.
2. `cp pipeline/config.yaml.example ~/jira-claude-pipeline/config.yaml` (or
   run the pipeline directly from `pipeline/` — either works, just keep
   `config.yaml`, `state/`, `logs/`, `worktrees/` out of git, see `.gitignore`).
3. Save your Jira API token to macOS Keychain (never commit it, never paste it
   into chat):
   ```bash
   echo -n "Jira API token: "; read -s TOKEN; echo
   security add-generic-password -a "<your-jira-email>" -s "aidev-jira-token" -w "$TOKEN" -U
   unset TOKEN
   ```
   Create a token at https://id.atlassian.com/manage-profile/security/api-tokens
4. Edit `config.yaml`: Jira base URL/email/label/statuses, `repos.default`
   path, `pr.base_branch`.
5. Symlink the skill into Claude Code globally:
   ```bash
   ln -sfn "$(pwd)/skills/aidev-jira-tickets" ~/.claude/skills/aidev-jira-tickets
   ```
   and reference it as a MUST-read in `~/.claude/CLAUDE.md` before any `aidev`
   ticket gets created (see the skill file for the exact ticket-writing rules
   and how to express dependency chains between tickets).
6. Make sure `gh` is authenticated for the target repo(s) and `tmux` is
   installed (`brew install tmux`).

## Running

Two independent loops, meant to run on a schedule (cron):

- `python3 pickup.py` — polls for new `aidev`-labeled tickets (skips anything
  still blocked by an unresolved Jira "Blocks" link), launches worktree +
  tmux + Claude Code, comments worktree/session info back, labels
  `aidev-picked`.
- `python3 monitor.py` — watches `RUNNING` tickets for the `AIDEV_TASK_COMPLETE`
  marker (opens a PR, labels `aidev-done`) or an `AIDEV_NEEDS_INPUT: <question>`
  marker (posts the question, labels `aidev-stuck`); also watches `STUCK`
  tickets for a human reply comment and relays it back into the live tmux
  session automatically.

Suggested cadence: pickup.py every 5 min, monitor.py every 2-3 min.

`python3 status.py` — quick table of all tracked tickets and their state.

## Manual intervention

Every ticket's Jira comment includes the worktree path, branch, session ID,
and a ready-to-paste resume command:
```bash
cd <worktree> && claude --resume <session-id>
```
Claude Code sessions are keyed by working directory + session id, independent
of the tmux pane, so you can resume from anywhere, anytime.

## State machine

`NEW → RUNNING → (STUCK ↔ RUNNING)* → POSTPROCESS → PR_OPENED → DONE` (or
`FAILED`), tracked in `state/aidev.sqlite3`. `DONE`/`FAILED` tickets are
checked every `monitor.py` run against their live Jira status; once that
status reaches Done/Rejected/Cancelled/Closed (a human call, never the
pipeline's), the worktree and local branch are deleted and the ticket moves
to `ARCHIVED` — this keeps disk usage bounded across weeks of tickets instead
of growing forever.

## Concurrency cap

`claude.max_concurrent` in `config.yaml` caps how many tickets can occupy a
Claude Code slot at once (`RUNNING`/`STUCK`/`POSTPROCESS`/`PR_OPENED`/`DONE`
all count — `ARCHIVED`/`FAILED` don't). If more `aidev`-labeled tickets are
waiting than there's room for, `pickup.py` picks up only as many as fit and
logs how many it deferred to a later run. Tagging 10 tickets at once won't
launch 10 parallel Claude Code sessions competing for your Mac's resources.

## Dependency chains between tickets

See the `aidev-jira-tickets` skill for the full guide. Short version: link
ticket 2 as "is blocked by" ticket 1 in Jira; `pickup.py` will not touch a
ticket while it has any non-terminal blocker.
