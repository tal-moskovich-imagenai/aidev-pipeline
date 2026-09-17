---
name: aidev-jira-tickets
description: Read before creating or tagging a Jira ticket for the aidev pipeline (label "aidev"). Covers ticket-writing conventions and how to express dependency chains between tickets so the pipeline respects ordering.
---

# aidev — Writing Jira Tickets for Autonomous Pickup

The **aidev pipeline** (code in this repo under `pipeline/`, deployed at
`~/jira-claude-pipeline` on this machine) polls Jira for tickets labeled
`aidev` and runs them end-to-end through Claude Code: git worktree →
implementation → `/simplify` → `/custom-simplify` → `/custom-review` → commit
→ PR → Jira comment/status update. No human types anything unless the ticket
is genuinely ambiguous.

This skill is for **preparing tickets that feed the pipeline** — read it
before tagging anything `aidev`, whether you're writing the ticket by hand or
asking Claude Code to draft one for you.

## Minimum bar for an `aidev` ticket

A ticket is "food" for the pipeline only if it has:

1. **A single, scoped outcome.** One coherent unit of work committable as one
   PR. If it needs 3+ unrelated changes, split it into linked sub-tickets
   instead (see Dependency Chains below).
2. **Enough context to implement without asking a human.** State:
   - What must change and why (the actual goal, not just "fix X")
   - Which repo (if not obvious) — label `repo:<key>` maps to `repos.<key>` in
     `~/jira-claude-pipeline/config.yaml`; otherwise the default repo is used
   - Acceptance criteria — how to know it's done
   - Any file/module hints if you already know where the change belongs
3. **The `aidev` label.** Nothing runs without it.
4. **Project = RND** (or whatever `jql_extra` in config.yaml currently scopes
   pickup to).

If a ticket is inherently ambiguous (a product decision, a design choice with
no clear default), still tag it `aidev` — the pipeline instructs Claude to
print `AIDEV_NEEDS_INPUT: <question>` and post it as a Jira comment instead of
guessing. Reply on the ticket with a normal comment; the pipeline detects it
and resumes the same Claude Code session automatically. But don't rely on this
for routine ambiguity — a ticket that triggers `AIDEV_NEEDS_INPUT` for
something you could have just stated up front wastes a full pipeline cycle.

## Dependency chains (epics / sequenced tickets)

When ticket 2 must not start until ticket 1 is merged (e.g. ticket 2 builds on
files ticket 1 creates), **use Jira's native "Blocks" link type** — do not
invent a custom label or comment convention for this.

**How to link:** On ticket 2, add an issue link: "is blocked by" → ticket 1.
(Equivalently: on ticket 1, "blocks" → ticket 2.)

**Why this and not something else:** the pipeline's `pickup.py` calls
`jira_client.get_blocking_issues(key)` before touching any ticket. If it
returns one or more blockers whose status isn't Done/Rejected/Cancelled/Closed,
the ticket is skipped silently (logged, not commented — no Jira noise) and
retried on the next poll. As soon as the blocking ticket's status flips to a
terminal state, the next `pickup.py` run picks up the now-unblocked ticket
automatically. No manual intervention needed.

**Practical rules for a sequenced epic:**
- Tag ALL tickets in the chain `aidev` up front — don't wait to tag ticket 2
  until ticket 1 finishes. The pipeline will simply skip blocked ones.
- Link every dependency explicitly, even "obvious" ones. The pipeline has no
  other way to know ticket order.
- A ticket can have multiple blockers; ALL must clear before pickup.
- Avoid diamond/circular dependencies — the pipeline does not detect cycles,
  it will just skip both forever. Keep chains linear or tree-shaped.
- If ticket 1 fails or is abandoned mid-pipeline (state `FAILED`, still open
  in Jira), tickets blocked by it stay blocked. Resolve ticket 1 manually
  (finish it, reject it, or unlink it) to unstick the chain.

**Example — a 3-step epic:**
```
RND-100 "Add the settings schema"                — aidev, no blockers
RND-101 "Add settings UI"                          — aidev, blocked by RND-100
RND-102 "Wire settings UI to the new schema"       — aidev, blocked by RND-101
```
All three get tagged `aidev` on day one. The pipeline picks up RND-100
immediately; RND-101 and RND-102 sit skipped until their blockers clear, in
order.

## Pipeline status/label lifecycle (read-only — the pipeline manages these)

| Jira status  | Jira label     | Meaning |
|--------------|----------------|---------|
| New          | `aidev`        | Waiting for pickup (and not blocked) |
| In Progress  | `aidev-picked` | Claude Code is actively working in a worktree |
| In Progress  | `aidev-stuck`  | Claude asked a question, waiting on your reply comment |
| In Review    | `aidev-done`   | PR opened, ready for human review |

Don't manually set these labels — they're mutually exclusive and managed by
`pickup.py`/`monitor.py` via `jira_client.set_state_label()`. Manually editing
status while a ticket is `RUNNING` in the pipeline's state DB can desync it.

## Review feedback loop

You control the `In Review → Done` transition entirely — the pipeline never
makes that call. If a PR needs changes after review:

1. Move the ticket's status back to `In Progress` and leave a comment with
   the feedback.
2. The next `monitor.py` run detects a `DONE` ticket now sitting on
   `In Progress` again, treats it as a rework request, and relaunches Claude
   Code **in the same worktree and branch** with your comment as context.
3. Claude addresses the feedback, re-runs the review steps, commits, and
   pushes to the **same branch** — this updates the existing PR, no
   duplicate is created.
4. The ticket lands back on `In Review` + `aidev-done` once done, same as the
   first pass. Repeat as many times as needed.
5. When you're actually satisfied, move it to `Done` yourself.

## What the pipeline does NOT do

- It does not infer dependency order from ticket text, epic links, or
  fix-versions — only explicit "Blocks"/"is blocked by" issue links.
- It does not re-order or prioritize tickets — `pickup.py` processes whatever
  the JQL query returns, blockers permitting.
- It does not merge PRs — a human reviews and merges.
- It does not resolve merge conflicts with the base branch automatically.

## Checking pipeline state

```bash
cd ~/jira-claude-pipeline
python3 status.py          # table of all tracked tickets and their state
tmux attach -t aidev-<TICKET-KEY>   # watch/intervene in a live session
cd worktrees/<TICKET-KEY> && claude --resume <session-id>   # resume yourself
```
