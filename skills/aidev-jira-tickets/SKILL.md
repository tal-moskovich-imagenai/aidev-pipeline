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

## Shared context — "must read" material from your own brainstorming

If you've done research or planning outside the ticket — your own
`.claude-code/` scratch docs, decisions logs, HLDs, Slack/call summaries,
handoff notes, the kind that can feed a whole multi-ticket epic rather than
one ticket — point the agent at it explicitly. Don't assume it will find
these on its own.

**How:** add a section to the ticket description (or a comment), e.g.:

```
## Context — must read before implementing
- /Users/talmoskovich/Documents/GitHub/<repo>/.claude-code/<epic>-decisions.md
- /tmp/<epic>-handoff.md
- Notion: <page URL>
- Figma: <file/frame URL>
- Slack: <message permalink>
```

Use **absolute paths** — the agent works from a completely different
worktree than wherever you wrote the doc, so a relative path resolves to
nothing.

**Any absolute path on this machine works**, including `/tmp` and paths under
`~/.superset/worktrees/...`. The agent reads the filesystem directly; it is
not restricted to its own worktree.

**It reads exactly the paths the ticket lists — it does not scan directories.**
Putting a doc in `/tmp` (or anywhere else) does nothing on its own; if it isn't
named in the ticket description or a comment, the agent will never open it.
List each file you want read, one per line. A directory path on its own is not
a reference.

**Where to put it, in order of preference:**

1. **The main checkout's `.claude-code/`** (e.g.
   `/Users/talmoskovich/Documents/GitHub/submitter-app-electron/.claude-code/`)
   — the durable choice for anything a multi-ticket epic depends on.
   `.claude-code/` is gitignored, so it never leaks into the pipeline's
   worktrees, and the main checkout is not itself ephemeral.
2. **`/tmp`** — fine, and the right home for genuinely short-lived handoffs.
   Note macOS prunes `/tmp` entries untouched for ~3 days and clears it on
   reboot, so don't park the only copy of an epic's context there if the
   epic will run for weeks.
3. **Another tool's worktree** (`~/.superset/worktrees/<repo>/<name>/...`) —
   works, but that worktree can be cleaned up by the tool that made it,
   which silently breaks every ticket referencing it. Prefer (1) for
   anything long-lived.

Whichever you pick, it must stay **outside `worktree_root`** in
`config.yaml` — see the safety note below.

**What the pipeline does with this (already built into the task prompt, no
extra setup needed):**
- The agent is instructed to read every referenced file/link in full before
  implementing anything.
- Notion links are fetched via the `/notion` skill. Figma links are fetched
  via the connected **Figma MCP** — the agent can inspect designs/frames
  directly. Slack links are fetched via the connected **Slack MCP** — the
  agent can pull the actual message/thread content, not just a raw URL
  fetch. All three are live integrations in this Claude Code setup, not
  best-effort guesses.
- Local `.md` files referenced this way are treated as **living, shared
  documents** — the agent appends new decisions/findings to them (in a
  dated/labeled section, following the file's existing style) rather than
  treating them as private scratch space. This is exactly the "Decisions
  taken while implementing" pattern already used in hand-written docs like
  `jxl-highres-upload-decisions.md` — the pipeline just continues that
  pattern automatically, across however many tickets share the same file.
  Existing content is never overwritten or rewritten, only appended to.
- The same instruction applies during the review-feedback rework loop (see
  below) — the agent re-checks referenced context if relevant to the
  feedback and keeps appending to it.

**Why this stays safe automatically:** every location recommended above —
the main checkout's `.claude-code/`, `/tmp`, another tool's worktree — sits
completely outside `worktree_root` in `config.yaml`
(`~/jira-claude-pipeline/worktrees/` by default). The pipeline's cleanup step
only ever deletes worktrees it created itself, tracked by exact path in its
own state DB; it has no way to reach anything else. No extra guardrail is
needed as long as your context docs stay outside `worktree_root`.

The one thing to avoid is putting them *inside* `worktree_root` — that is the
only directory the pipeline deletes from.

Note that the main checkout's `.claude-code/` is gitignored, so it is present
on disk at that absolute path but **never** appears inside a pipeline
worktree. That is why the absolute path is required rather than a
repo-relative one, even for a doc that lives in the same repo.

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

### Creating the link over the API — get the direction right

Doing this in the Jira UI is unambiguous. Doing it over the REST API is a trap
that has already inverted a whole epic's chain. To say **"X is blocked by B"**:

```json
POST /rest/api/3/issueLink
{"type": {"name": "Blocks"},
 "inwardIssue":  {"key": "B"},     // the BLOCKER
 "outwardIssue": {"key": "X"}}     // the ticket that WAITS
```

**Verify with JQL. Never by reading the link JSON back.**

Which key lands under `inwardIssue` vs `outwardIssue` in a *response* depends
on which issue you fetched, so inspecting the fields is self-confirming — it
agrees just as readily with a backwards link as a correct one. Two separate
"corrections" were made on 2026-09-18 on the strength of field-reading, both
wrong, before JQL settled it.

```
issue in linkedIssues("X", "is blocked by")   -> must return B
issue in linkedIssues("X", "blocks")          -> must NOT return B
```

Run both. The second one catches an inverted link that the first can miss.

A silently inverted chain is expensive: the pipeline runs the epic **backwards**,
picking up the last ticket first, and every agent then finds none of its
foundation in master.

**Why this and not something else:** the pipeline's `pickup.py` calls
`jira_client.get_blocking_issues(key)` before touching any ticket. If it
returns one or more blockers whose status isn't Done/Rejected/Cancelled/Closed,
the ticket is skipped silently (logged, not commented — no Jira noise) and
retried on the next poll. As soon as the blocking ticket's status flips to a
terminal state, the next `pickup.py` run picks up the now-unblocked ticket
automatically. No manual intervention needed.

### Stacking on a blocker that's only In Review (not merged yet)

`stack_on_review: true` in `config.yaml` (on by default) lets the pipeline
proceed on a ticket as soon as its blocker reaches `review_status` (e.g.
"In Review") — it does not wait for the blocker to actually merge. This
mirrors what you'd do by hand: build PR N+1 on top of PR N before N lands,
rather than blocking your whole day on someone else's review turnaround.

**What happens:** the new ticket's worktree branches off the blocker's
branch (`git worktree add ... -b <new> <blocker-branch>`), not off
`master`/`main`. Its eventual PR is opened with the blocker's branch as the
base, not `master` — same shape as manually chaining `gh pr create --base
<other-pr-branch>`. The agent is told explicitly in its task prompt that it's
on a stacked branch and that the blocker's changes being present in the diff
is expected, not a mistake to undo.

**Requirements for stacking to actually trigger:**
- The blocker must have been picked up **by this same pipeline instance**
  (its branch is looked up from the pipeline's own state DB, not guessed).
  A ticket you built manually, or one another pipeline instance/repo picked
  up, has no recorded branch here — the dependent ticket is skipped with a
  clear log line (`blocker X is In Review but not tracked locally`) rather
  than guessing at a branch name.
- Only one soft (In-Review) blocker is supported per ticket. A ticket with
  two or more open blockers, even if all are In Review, is skipped until
  it's down to at most one.
- A **hard** blocker (any status other than terminal or `review_status`)
  still blocks unconditionally — stacking only changes the "In Review isn't
  merged yet" case, not "still New"/"In Progress" ones.

**Consequence you must handle manually:** a stacked PR is not runnable in
isolation — it needs its base to merge (or be rebased onto whatever actually
lands) before it can go into `master`. Once the base PR merges, rebase the
stacked one yourself; the pipeline does not automate this. If the base
ticket's review produces changes, they land on the base branch and the
stacked PR's diff stays layered on top — same as any manual PR stack.

**When to leave this off:** if your team prefers each PR to be independently
mergeable, or blockers routinely change enough during review that stacking
would mean constant rebasing, set `stack_on_review: false` and let tickets
wait for a real merge like before.

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
