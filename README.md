# aidev-pipeline

Jira → Claude Code autonomous development pipeline. Tag a Jira ticket
`aidev`; the pipeline creates a git worktree, runs Claude Code end-to-end
(implement → review → commit → PR), and reports back on the ticket —
including asking for clarification via Jira comments when genuinely stuck.

**Current status on this machine:** running unattended via Hermes cron (see
"Scheduling" below) — `pickup.py` every 5 min, `monitor.py` every 3 min.

## Repo layout

```
pipeline/               the actual orchestrator (Python, stdlib-only + PyYAML)
  lib/
    config.py           loads config.yaml
    jira_client.py      Jira Cloud REST v3 client (urllib, Basic auth via Keychain)
    state.py             SQLite ticket-tracking state machine
    procs.py             tmux + shell helpers
    github.py            gh pr lookup fallback for stacking on manually-built blockers
    lockfile.py           PID lock so pickup.py/monitor.py never overlap
    pipelog.py             stdout + dated file logging
    notify.py               macOS + optional Slack notifications
    soul.py                   loads SOUL.md into every task prompt
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

## Scheduling

**macOS `cron` requires Full Disk Access and silently fails without it** —
confirmed on this machine (`Operation not permitted` reading the scripts,
zero indication in `crontab` itself that anything is wrong). Rather than
grant that, this deployment uses **Hermes's own cron scheduler**
(`cronjob_manage`), which runs as a normal user process and isn't subject to
the same TCC restriction:

```bash
# wrapper scripts, referenced by filename only from ~/.hermes/scripts/
~/.hermes/scripts/aidev-pickup.sh    # cd + python3 pickup.py
~/.hermes/scripts/aidev-monitor.sh   # cd + python3 monitor.py
```

Both are registered as `no_agent: true` jobs (pure script execution, no LLM
call) named `aidev-pickup` (`*/5 * * * *`) and `aidev-monitor`
(`*/3 * * * *`), `deliver: local` (no chat/notification spam per run — the
pipeline's own `notify()` handles that separately for stuck/done/failed).

If you ever move this to a plain `cron`/`launchd` setup instead, remember
the Full Disk Access step for `/usr/sbin/cron` first (System Settings →
Privacy & Security → Full Disk Access → add `/usr/sbin/cron`), or jobs will
appear scheduled but never actually run.

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
