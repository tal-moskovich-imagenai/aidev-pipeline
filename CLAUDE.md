# aidev-pipeline — instructions for Claude Code

This repo builds the code in `pipeline/` (deployed at
`~/jira-claude-pipeline`). It is developed BY Claude Code sessions, and it
also autonomously RUNS Claude Code sessions against Jira tickets — don't
confuse the two: `SOUL.md` is injected into the pipeline's own *spawned*
sessions (the ones doing ticket work), not read by a session developing
this repo's code. This file is for the latter.

## Never regex-parse a running process's free text — use agent judgment

If a signal comes from text that Claude, Cursor, Bugbot, Codex, or any
other LLM-driven process wrote in its own words — a PR comment, a review
verdict, a completion marker's surrounding prose — do not extract meaning
from it with a regex. Free text from a process we don't control the exact
wording of *will* eventually phrase itself differently than whatever
pattern you wrote against, and the failure is silent: the regex just stops
matching, with no exception and no error log pointing at the real cause.

**Concrete case that actually happened:** `_custom_review_covers_head`
extracted the last-reviewed commit SHA by regexing Claude's own
"aidev decisions: ... commit(s) [up to] `<sha>`" PR comment heading.
Claude phrased one comment "current HEAD `<sha>`" instead of "commit
`<sha>`" — a completely reasonable, unremarkable choice of words — and the
regex silently stopped matching, forever. Every later auto-merge cycle
then saw the `ai-reviewed` label as permanently stale and re-triggered a
review pass that had, in reality, already run and found nothing. RND-14813
/ PR #5759 cycled `auto_merge_recheck` 34 times over ~19 hours before
`max_running_hours` finally killed it as FAILED — pure wasted tokens and
wall-clock time, with no error anywhere to point at the actual cause.

**What to do instead, in order of preference:**
1. **Read the value directly from a source we control**, not from prose
   about it — e.g. `git rev-parse HEAD` for "what commit did this review
   actually run against," not a sentence describing it. This is how the
   fix above works: the reviewed SHA is now written to the state DB
   directly from git the moment a review stage completes, never parsed
   back out of a comment.
2. **If there's no non-text source, let an agent read and judge the text**,
   the same way the auto-merge approval-verdict stage already does with
   Cursor's Approval Agent output — hand it the text, ask for a judgment,
   don't pattern-match the wording yourself. A wrong judgment call is
   visible and correctable; a silently-broken regex is neither.
3. A fixed-format machine output we ourselves emit (Codex's `## Verdict:
   CLEAN` line) is not the same risk class — we control that format
   completely. Even so, prefer a structured channel over free text where
   one is available: `.claude-code/status.json` is now the sole
   completion/needs-input signal — the `AIDEV_TASK_COMPLETE`/
   `AIDEV_NEEDS_INPUT` marker text and its regex scraper were removed
   entirely (not just deprioritized), specifically because the marker
   regex had already had one live near-miss of its own (a leading
   TUI glyph it didn't originally account for).

## Regex-parsing free text needs a real reason, checked every time

Before adding any new `re.compile`/`re.search`/`re.match` against text an
LLM-driven process generated, stop and ask: what happens the day this
process phrases it slightly differently? If the answer is "nothing tells
anyone, it just quietly stops working," don't write the regex.

## Nudge the agent to fetch context itself — don't pre-fetch it into the prompt

When a task prompt needs to point an agent at more context (a blocker's
status, a sibling ticket in another repo, a linked doc), give it the
pointer — a ticket key, a repo name, "go check its status/branch" — and
let the agent decide whether and how deep to look, rather than fetching
that context ourselves and pasting it into the prompt up front. Every
token spent pre-fetching is spent whether or not the agent actually needed
it; a short nudge costs a few words and the agent only pays the larger
cost of reading a linked ticket/PR when its own judgment says the task
actually calls for it. This mirrors the existing stacked-ticket convention
(pointer + on-demand fetch, not "read the full diff up front") — applied
generally, not just to that one case.

## Keep `skills/aidev-jira-tickets/SKILL.md` in sync — it is documentation-as-code

`skills/aidev-jira-tickets/SKILL.md` is symlinked into Claude's own skills
directory and is read by Claude before every ticket gets tagged `aidev` —
treat it exactly like code, not like a comment. Whenever a change in this
repo alters:
- the Jira label lifecycle (which labels exist, what triggers them, what
  they mean),
- the review-feedback/rework loop's behavior (what gets read, from where,
  how it's filtered),
- the auto-merge sequence's steps or gates,
- or any other behavior that skill file describes,

update `skills/aidev-jira-tickets/SKILL.md` in the SAME commit/PR as the
code change. Do not treat this as a follow-up task. If you're not sure
whether your change is documented there, grep the file for the
function/label/concept you touched before assuming it isn't.

This applies to bug fixes and corrections too, not just new features — if
a fix changes what the pipeline actually does compared to what that skill
currently claims, the skill was wrong the moment the old behavior stopped
being true, even if nobody has read it since.

## Before touching monitor.py/pickup.py/config.yaml

Run `python3 status.py` after your change — it must keep working (no
crash, same ticket count). If it errors, your change broke something; fix
it before moving on.

## Model/session decisions (locked, revisit only if measurement contradicts)

- Every pipeline stage gets an explicit `--model` from `config.yaml`'s
  `claude.models` block — never left `null`/"account default". Sonnet for
  all real-work stages (implement, rework, self_review, codex_check, every
  auto_merge_* sub-stage except approval); Haiku only for
  `auto_merge_addressing_approval_feedback` (a 3-way classification against
  an explicit rule, not open-ended judgment).
- `--resume` is used for every relaunch instead of a fresh session
  (`relaunch_claude` in `monitor.py`) — this is a locked assumption
  (session-cache reuse saves tokens), not yet independently verified by a
  spike; if a later ccusage re-measurement doesn't show the expected
  improvement, that assumption is the first thing to re-check, not the
  last.
- Review depth (self_review, then Codex) is controlled by two
  self-consuming Jira tags (`aidev-self-review`, `aidev-codex-review`), not
  a round-counter or a diff-size heuristic. Presence → run once → remove
  the tag; absent → skip. Tags are removed only after their stage
  completes successfully — a crash mid-stage leaves the tag in place, and
  the tag's presence IS the retry state, no separate counter needed.
- `aidev-auto-merge` is NOT a self-consuming tag like the two above — it is
  a standing human signature ("I trust the review process through to
  merge"), never removed by a rework cycle. It IS removed when
  `escalate_auto_merge` hits a genuinely-stuck case (repeated identical
  Cursor verdict, missing required label, merge failure, etc.) — that
  removal is the deliberate human hand-off ("sorry, take it now"), not
  tag-consumption; re-tagging afterward means a human looked and said try
  again. A rework never touches this label, so once rework lands back on
  `In Review` + `aidev-done`, the very next DONE-sweep restarts the entire
  auto-merge sequence from the top (base check → unresolved-PR-feedback
  check → CI check → fresh `@bugbot run` → approval), not from wherever it
  stopped before rework.

## A Jira blocker link has no notion of repo — never assume same-repo stacking

A cross-repo blocker is handled differently depending on whether it's hard
or soft. A **hard** blocker (not yet in review_status) genuinely still
blocks regardless of repo — the ticket depending on it should wait either
way. A **soft** blocker (sitting In Review, not merged) exists specifically
so a same-repo ticket can stack a branch on top of it instead of waiting —
but that rationale only holds when there's an actual branch in the SAME
repo to stack on. A cross-repo soft blocker has no code dependency at all:
`get_blocking_issues`'s soft-blocker path filters these out entirely (not
"can't stack, skip" — genuinely not blocking) before even considering
stacking, checking the blocker's own `repo:` label/tracked `repo_path`
against the current ticket's repo. A Jira "blocks" link is purely a
project-management relationship — nothing stops two tickets in entirely
different repos (a BE service ticket blocking an unrelated FE ticket) from
being linked, and being In Review in a repo the current ticket doesn't
touch has zero bearing on whether the current ticket can proceed. Caught
live twice on the same real case (RND-14828 submitter-app-electron,
soft-blocked by RND-14840 app-web-server): first fixed to stop crashing the
whole pickup batch on a bogus cross-repo `git worktree add`, then corrected
further — a cross-repo soft blocker isn't just "unstackable," it isn't a
blocker at all, and treating it as one meant RND-14828 sat skipped every
5-minute cycle indefinitely for no real reason.

## Auto-retry a crashed session before failing it

A crash (tmux alive but the claude process dead, or status.json stale past
`STATUS_FILE_STALE_MINUTES`) auto-relaunches the same session/stage up to
`claude.max_crash_auto_retries` times (default 1) before `mark_failed`
actually fires. Added after tracing every real crash on 2026-09-26/27
(RND-14818, RND-14824, RND-14825, RND-14845) and finding each one had a
clean, safely-resumable worktree — the crash itself was harmless, but every
occurrence required manual DB/Jira recovery (restore `aidev-done`, reset
`stage`, kill/relaunch) before this existed. `state.crash_retry_count` is
the retry counter (mirrors `postprocess_attempts`'s existing pattern);
reset to 0 in `finish_ticket` the moment a ticket makes real forward
progress (reaches POSTPROCESS), so a later, unrelated crash still gets its
own full retry budget instead of inheriting an exhausted count from an
already-resolved problem.

## `claude_process_alive` isn't enough — verify WHICH claude process, and where

A tmux pane's shell can eat/mangle the very first `cd <worktree> && claude
...` command sent to it (see the DISABLE_AUTO_UPDATE comment in
`procs.py`'s `tmux_new_session`) — any shell startup noise racing that
first `send-keys` can do it, not just oh-my-zsh's update prompt. When it
happens, claude still starts and stays alive, just in the wrong directory
(e.g. the pipeline's own root instead of the ticket's worktree) — so
`claude_process_alive`'s "is some claude process running in this pane"
check reports healthy while the session sits genuinely hung, doing
nothing, for as long as nobody looks (caught live on RND-14737: 5+ hours).
Fixed two ways: `tmux_new_session` now calls `wait_for_shell_ready`
(polls pane output until it stops changing) before the caller sends
anything real, closing most of the race at the source; and
`launch_claude_verified` (used by both `pickup.py`'s initial launch and
`monitor.py`'s `relaunch_claude`) confirms via `verify_claude_launched_in`
that the actual claude process's real cwd (read via `lsof -d cwd`) matches
the intended worktree, retrying once with a fresh pane if it doesn't —
closing the race even on the rare case it still slips through.

## `state.get()` finding a row does not mean "still tracked"

`pickup_ticket`'s dedup check must not treat an `ARCHIVED` row the same as
a live one. `ticket_key` is the DB's PRIMARY KEY, so once a row exists for
a key at all, a naive `if state.get(key): skip` blocks that ticket from
ever being picked up again — even after the ticket is legitimately reset
(Jira status back to New, worktree/branch cleaned up, e.g. a wrong-repo
pickup corrected via a `repo:` label fix). Caught live: RND-14736 was
archived and reset to New specifically to be re-picked up against the
corrected repo, and sat silently skipped every 5-minute cycle
indefinitely — "already tracked, skipping" with no error, nothing to
notice unless someone asked why it wasn't progressing. Fix: only a
non-ARCHIVED row blocks pickup; an ARCHIVED row is deleted (via
`state.delete_archived`, scoped to `state='ARCHIVED'` only) right before
the fresh `insert()`, since a second `insert()` for the same PRIMARY KEY
would otherwise crash on a UNIQUE constraint violation.

## Scripting against the live deployment from outside a worktree

`lib/config.py` resolves `config.yaml` relative to this repo's own
directory, which has no `config.yaml` (only `config.yaml.example` — the
real one lives at the deploy location, gitignored). Set
`config.CONFIG_PATH` explicitly before importing anything that reads
config:

```python
from lib import config
config.CONFIG_PATH = "/Users/talmoskovich/jira-claude-pipeline/config.yaml"
from lib import jira_client, state, monitor  # now config.load() resolves correctly
```
