#!/usr/bin/env python3
"""
aidev monitor — checks tickets in RUNNING state; when `.claude-code/status.json`
reports state "complete", moves them to POSTPROCESS: pushes the branch, opens
a PR via gh, and posts a summary comment back to Jira.

Also checks tickets in STUCK state; when status.json reports "needs_input" it
posts the question as a Jira comment and waits. Once a human replies on the
ticket (a comment that isn't one of aidev's own [aidev]-style comments), the
reply text is typed into the tmux session so Claude can continue.

Run this from cron (e.g. every 2-3 minutes), separate from pickup.py.
"""
import os
import re
import subprocess
import sys
import uuid
import json
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import codex_runner, config, jira_client, state, procs, lockfile, github
from lib.pipelog import get_logger
from lib.notify import notify
from lib.soul import soul_section, jira_live_fetch_note

log = get_logger("monitor")


STATUS_FILE_STALE_MINUTES = 60  # no update to status.json while "running" past
                                 # this many minutes -> treat as stuck/crashed,
                                 # BUT ONLY IF claude_process_alive also says the
                                 # process is gone (see process_running_ticket) —
                                 # a live process just means a long turn (parallel
                                 # /simplify sub-agents, a big test suite, a slow
                                 # Codex wait), not a crash. 10 min was too tight:
                                 # traced live on RND-14840, whose session was
                                 # continuously active (no gap over ~5 min in its
                                 # own transcript, no OS crash report, no sleep/wake)
                                 # but still got mark_failed'd 4 times in one day
                                 # purely for not having written an incremental
                                 # status.json update during one long turn/parallel
                                 # sub-agent run — SOUL.md only asks for updates "at
                                 # each meaningful transition," not on a fixed clock,
                                 # so a healthy long turn legitimately produces gaps
                                 # bigger than the old 10-minute window.


def read_status_file(worktree_path):
    """Reads .claude-code/status.json — the ONLY completion/input signal
    (no AIDEV_TASK_COMPLETE/AIDEV_NEEDS_INPUT marker text is scanned for
    anymore; that regex-on-free-text approach had a real live bug — see
    CLAUDE.md's "never regex a running process's free text" rule, plus a
    documented near-miss of its own on RND-14813 with a leading TUI glyph).
    Returns a dict with state/detail/updated_at, or None if missing/
    unreadable/malformed. Never raises."""
    path = os.path.join(worktree_path, ".claude-code", "status.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or "state" not in data:
        return None
    return data


def status_file_is_stale(status):
    """True if status['state'] == 'running' and updated_at is older than
    STATUS_FILE_STALE_MINUTES — signals a likely crash, not legitimate
    long-running work (which should keep re-writing 'running')."""
    if not status or status.get("state") != "running":
        return False
    updated_at = status.get("updated_at")
    if not updated_at:
        return False
    try:
        ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.utcnow()
    except (ValueError, TypeError):
        return False
    return (now - ts).total_seconds() > STATUS_FILE_STALE_MINUTES * 60


def open_pr(ticket):
    cfg = config.load()
    worktree_path = ticket["worktree_path"]
    branch = ticket["branch"]
    key = ticket["ticket_key"]

    # Defense in depth: .aidev_prompt.txt lives under .claude-code/ now (see
    # relaunch_claude/pickup.py), but if it's tracked from an older run or a
    # future bug re-adds it at the repo root, strip it before it ships in a PR.
    procs.sh("git rm --cached --ignore-unmatch .aidev_prompt.txt .claude-code/.aidev_prompt.txt",
             cwd=worktree_path, check=False, timeout=60)

    # Make sure everything is committed (Claude was instructed to commit,
    # but double-check / catch stragglers).
    # Big repos (large node_modules + husky pre-commit/lint-staged) can push
    # `git add`/`git commit`/`git push` well past the default 60s timeout —
    # give these a 10-minute budget instead of procs.sh's default.
    procs.sh("git add -A", cwd=worktree_path, check=False, timeout=600)
    status = procs.sh("git status --porcelain", cwd=worktree_path, check=False, timeout=600)
    if status:
        msg = procs.shlex.quote(f"{key}: wip changes captured by aidev monitor")
        procs.sh(f"git commit -m {msg}", cwd=worktree_path, check=False, timeout=600)

    procs.sh(f"git push -u origin {branch}", cwd=worktree_path, check=False, timeout=600)

    # Normal case now: Claude opens its own PR (with a real title/description)
    # before marking status.json complete — reuse it rather than creating a
    # second one.
    existing = procs.sh(
        f"gh pr view {branch} --json url --jq .url", cwd=worktree_path, check=False
    )
    if existing and existing.startswith("https://"):
        return existing.strip(), f"reused existing PR: {existing.strip()}"

    # Fallback only: Claude should have opened this itself. If we're here,
    # something went wrong upstream — this placeholder unblocks the pipeline
    # but is not the intended path; check why Claude didn't open its own PR.
    log(f"{key}: no PR found after implement — Claude should have opened one; falling back to a placeholder PR")
    base = ticket.get("stacked_on_branch") or cfg["pr"]["base_branch"]
    title = f"{key}: automated by aidev"
    body = (
        f"Automated implementation for {key}.\n\n"
        f"Generated by Claude Code via aidev pipeline. This PR was opened by the "
        f"orchestrator's fallback, not by Claude itself — that's unexpected; the task "
        f"prompt instructs Claude to open its own PR with a real description."
    )
    if ticket.get("stacked_on"):
        body += (
            f"\n\nStacked on {ticket['stacked_on']} (still In Review at PR open time) — "
            f"this PR's diff includes {ticket['stacked_on']}'s changes until that one merges. "
            f"Rebase onto {cfg['pr']['base_branch']} after {ticket['stacked_on']} lands."
        )
    pr_out = procs.sh(
        f'gh pr create --base {base} --head {branch} --title {procs.shlex.quote(title)} '
        f'--body {procs.shlex.quote(body)}',
        cwd=worktree_path, check=False,
    )
    pr_url = None
    for line in pr_out.splitlines():
        if line.startswith("https://"):
            pr_url = line.strip()
    return pr_url, pr_out


def mark_failed(key, reason, tmux_name=None):
    log(f"{key}: FAILED — {reason}")
    state.set_state(key, "FAILED")
    try:
        jira_client.add_comment(key, f"[aidev] Marked as failed: {reason}")
        jira_client.set_state_label(key, "aidev-stuck")
    except Exception as e:
        log(f"{key}: could not post failure comment: {e}")
    if tmux_name:
        procs.tmux_kill(tmux_name)
    notify(f"aidev: {key} failed", reason, key=key)


def build_crash_retry_prompt(key, stage):
    return f"""{soul_section()}You are resuming work on Jira ticket {key} after your previous session
crashed mid-{stage} (the process died without writing a final status.json
update — an unhandled error, an interrupted tool call, or similar).

Check your own recent commits and any uncommitted changes in this worktree
first — you may already have real progress that just needs to be picked up
and continued, not redone from scratch. Continue from wherever you actually
left off; don't assume you have to restart the whole stage.

When done, write status.json's state to "complete" (or "needs_input" if you
genuinely need a human, same rules as usual).
"""


HANDOFF_MARKER_STALE_MINUTES = 10  # if the requested handoff doc still hasn't
                                    # appeared after this long, give up waiting
                                    # and fall back to the normal crash-retry
                                    # path instead of blocking this ticket
                                    # forever on a request that may itself have
                                    # gotten stuck


def handoff_marker_path(worktree_path):
    return os.path.join(worktree_path, ".claude-code", ".context_handoff_requested")


def handoff_doc_path(worktree_path, key):
    return os.path.join(worktree_path, ".claude-code", f"handoff-{key}.md")


def build_context_handoff_request(key):
    """Sent as a live tmux_send into an otherwise-healthy session whose
    context usage just crossed the configured threshold — not a crash, so
    this doesn't go through relaunch_claude/a fresh prompt file; it's typed
    into the session exactly like a human's STUCK reply already is."""
    doc_path = f".claude-code/handoff-{key}.md"
    return (
        f"Your context usage has crossed the configured handoff threshold. "
        f"Before continuing, write a complete handoff document for the next "
        f"session at {doc_path} (create it if it doesn't exist), covering: "
        f"what you've done so far and why, the current state of the code/PR, "
        f"what's left to do, any open questions or judgment calls you made, "
        f"and exactly where to resume. Be thorough — the next session will "
        f"have NO memory of this conversation and will rely entirely on this "
        f"file plus the repo's own commits/decisions log. Once the file is "
        f"written and saved, stop — do not do any further work in this "
        f"session, a fresh session will take over from the handoff doc."
    )


def build_context_handoff_resume_prompt(key, stage, doc_rel_path, old_session_id):
    return f"""{soul_section()}You are picking up Jira ticket {key} in a brand-new session. The
previous session ({old_session_id}) was proactively retired after its context
usage crossed the configured handoff threshold — it was still healthy, not
crashed, and wrote a handoff document for you before stopping.

Read {doc_rel_path} FIRST, in full, before doing anything else — it has the
complete picture: what's been done, current state of the code/PR, what's left,
and any open questions or judgment calls already made. Do not redo work it
describes as already done; do not silently re-decide something it already
decided unless you have a concrete reason to disagree (and if you do,
document that in the same decisions-log convention SOUL.md already
describes).

After reading it, re-orient against the real live state too — `git log`,
`git status`, `git diff` against the base branch, and the PR's current state
via `gh pr view` — a handoff doc can describe intent accurately but still be
a few minutes stale relative to CI/reviews.

When done, write status.json's state to "complete" (or "needs_input" if you
genuinely need a human, same rules as usual).
"""


def check_context_handoff(ticket):
    """Proactively retires a healthy-but-context-heavy RUNNING session before
    it has a chance to crash from sheer conversation size — this is a
    distinct, EARLIER signal than the crash-retry logic elsewhere in this
    file (which only reacts after something has already gone wrong). Traced
    live on RND-14840: a --resume'd session sitting at 95%/1M tokens crashed
    twice in a row shortly after; the fix that actually worked was starting
    a genuinely fresh session rather than a third --resume of the same
    bloated conversation. This automates exactly that recovery, triggered
    proactively instead of reactively.

    Returns True if it took an action this cycle (caller should not also run
    its normal status-file checks against a session that's mid-handoff or
    just got replaced), False if there's nothing to do.
    """
    cfg = config.load()
    threshold = cfg["claude"].get("context_handoff_threshold")
    if not threshold:
        return False

    key = ticket["ticket_key"]
    worktree_path = ticket["worktree_path"]
    tmux_name = ticket["tmux_session"]
    session_id = ticket.get("session_id")

    marker_path = handoff_marker_path(worktree_path)
    doc_path = handoff_doc_path(worktree_path, key)

    if os.path.exists(marker_path):
        # Already requested — check whether the handoff doc has landed yet.
        if os.path.exists(doc_path):
            log(f"{key}: handoff doc written — replacing session (context handoff)")
            old_session_id = session_id
            stage = ticket.get("stage") or "implement"
            doc_rel_path = os.path.relpath(doc_path, worktree_path)
            prompt = build_context_handoff_resume_prompt(key, stage, doc_rel_path, old_session_id)
            fresh_ticket = dict(ticket)
            fresh_ticket["session_id"] = None  # forces relaunch_claude to mint --session-id, not --resume
            new_session_id = relaunch_claude(fresh_ticket, prompt, stage=stage)
            state.set_session_id(key, new_session_id)
            state.clear_crash_retry_count(key)
            try:
                os.remove(marker_path)
            except OSError:
                pass
            try:
                jira_client.add_comment(
                    key,
                    f"[aidev] Context usage crossed {int(threshold * 100)}% — proactively replaced "
                    f"the session with a fresh one to avoid a context-exhaustion crash. Old session "
                    f"{old_session_id} wrote a handoff doc ({doc_rel_path}) before stopping; new "
                    f"session {new_session_id} picked up from it.",
                )
            except Exception as e:
                log(f"{key}: could not post handoff-replacement comment: {e}")
            notify(
                f"aidev: {key} session replaced (context handoff)",
                f"Old session {old_session_id} -> new session {new_session_id}. "
                f"Handoff doc: {doc_rel_path}",
                key=key,
                pr_url=ticket.get("pr_url"),
            )
            return True

        # Still waiting on the handoff doc — give up after a while and let
        # the normal crash-retry logic take over instead of blocking forever.
        age_minutes = (time.time() - os.path.getmtime(marker_path)) / 60
        if age_minutes > HANDOFF_MARKER_STALE_MINUTES:
            log(f"{key}: handoff doc never appeared after {age_minutes:.0f}m — giving up on the "
                f"handoff, falling back to normal crash handling")
            try:
                os.remove(marker_path)
            except OSError:
                pass
            return False
        log(f"{key}: waiting for handoff doc ({age_minutes:.0f}m)")
        return True

    pct = procs.read_context_usage_pct(worktree_path, session_id)
    if pct is None or pct < threshold:
        return False

    log(f"{key}: context usage {pct:.0%} >= threshold {threshold:.0%} — requesting a handoff doc")
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    procs.tmux_send(tmux_name, build_context_handoff_request(key))
    with open(marker_path, "w") as f:
        f.write(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return True



def process_running_ticket(ticket):
    cfg = config.load()
    key = ticket["ticket_key"]
    tmux_name = ticket["tmux_session"]
    max_hours = cfg["claude"].get("max_running_hours")

    if not procs.tmux_session_exists(tmux_name):
        mark_failed(key, "tmux session disappeared while RUNNING (crash, reboot, or manual kill)")
        return

    # Checked before the crash-detection logic below: this is a proactive,
    # healthy-session handoff, not a reaction to something already broken.
    # A session mid-handoff (marker written, doc not yet landed) is still a
    # live, working `claude` process — the crash checks below would
    # otherwise have nothing to distinguish it from a hung/dead one for the
    # ~minutes it takes to write the handoff doc.
    if check_context_handoff(ticket):
        return

    if not procs.claude_process_alive(tmux_name):
        # Same crash class as the status.json-staleness check further down
        # (tmux alive, Claude process dead) — every real occurrence we've
        # traced turned out to be a harmless process crash with a clean,
        # safely-resumable worktree. Give it the same auto-retry budget
        # before escalating to a human, rather than failing immediately —
        # this check runs first and would otherwise bypass that retry logic
        # entirely for the exact same failure mode.
        retry_count = state.bump_crash_retry_count(key)
        max_retries = cfg["claude"].get("max_crash_auto_retries", 1)
        if retry_count <= max_retries:
            log(f"{key}: claude process dead (crash retry {retry_count}/{max_retries}) — "
                f"auto-relaunching same session/stage before giving up")
            stage = ticket.get("stage") or "implement"
            prompt = build_crash_retry_prompt(key, stage)
            relaunch_claude(ticket, prompt, stage=stage)
            try:
                jira_client.add_comment(
                    key,
                    f"[aidev] Session crashed (claude process died) — auto-retrying "
                    f"(attempt {retry_count}/{max_retries}) before escalating to a human.",
                )
            except Exception as e:
                log(f"{key}: could not post crash-retry comment: {e}")
            return
        mark_failed(
            key,
            "tmux session is alive but the claude process inside it is not — it crashed or exited "
            "(e.g. an unhandled API error) without finishing. The session's pane output up to that "
            f"point may still be useful context for a human or for a manual relaunch. "
            f"(already auto-retried {max_retries} time(s), still crashing)",
        )
        return

    elapsed = state.seconds_running(ticket)
    if max_hours and elapsed and elapsed > max_hours * 3600:
        mark_failed(
            key,
            f"exceeded max_running_hours ({max_hours}h) — likely stuck in a loop or blocked silently",
            tmux_name=tmux_name,
        )
        return

    status = read_status_file(ticket["worktree_path"])
    if status and status.get("state") == "complete":
        log(f"{key}: status.json reports complete, waiting for idle before postprocess")
        procs.wait_for_idle(
            tmux_name,
            cfg["claude"]["idle_seconds"],
            cfg["claude"]["idle_poll_interval"],
            max_wait_seconds=300,
        )
        finish_ticket(ticket)
        return
    if status and status.get("state") == "needs_input" and status.get("detail"):
        question = status["detail"]
        log(f"{key}: status.json reports needs_input — {question}")
        state.set_stuck(key, question)
        try:
            jira_client.add_comment(
                key,
                f"[aidev] I need input to continue:\n\n{question}\n\n"
                f"Reply on this ticket and I'll pick it up on the next check.",
            )
            jira_client.set_state_label(key, "aidev-stuck")
        except Exception as e:
            log(f"{key}: could not post stuck comment: {e}")
        notify(f"aidev: {key} needs input", question, key=key, pr_url=ticket.get("pr_url"))
        return
    if status_file_is_stale(status):
        # Only treat this as a crash signal if the claude process is ALSO
        # confirmed gone — claude_process_alive already ran above and we'd
        # have returned there if it were dead, so reaching here with a stale
        # status.json means the process is technically alive but hasn't
        # written an incremental update in STATUS_FILE_STALE_MINUTES. Do NOT
        # kill a live process over that alone: a long single turn (parallel
        # /simplify sub-agents, a big test suite, a slow Codex wait) can
        # legitimately go this long without a status.json write — SOUL.md
        # only asks for updates "at each meaningful transition," not on a
        # fixed clock. Re-check liveness explicitly (not just "we didn't
        # return above a few lines ago") since staleness can be noticed on a
        # LATER sweep than the alive-check ran on, and the process could have
        # died in between.
        if procs.claude_process_alive(tmux_name):
            log(f"{key}: status.json stale but claude process is still alive — "
                f"assuming a long turn, not a crash; not killing")
            return
        # status.json is the ONLY completion/input signal now (no marker-text
        # fallback) — stuck on "running" past the staleness window with
        # nothing else to check IS the crash signal, not just a maybe.
        #
        # Every real occurrence of this signal we've traced (RND-14818,
        # RND-14824, RND-14825, RND-14845, all same day) turned out to be a
        # genuine but harmless process crash (tmux alive, Claude process
        # gone) with a clean, safely-resumable worktree — not corrupted
        # work. Auto-retry once before giving up: same session (--resume),
        # same stage, fresh status.json (relaunch_claude already resets it
        # on every call). Only escalate to a real FAILED after repeated
        # crashes on the SAME ticket, which is a genuinely different,
        # worth-a-human-looking-at signal from "crashed once."
        retry_count = state.bump_crash_retry_count(key)
        max_retries = cfg["claude"].get("max_crash_auto_retries", 1)
        if retry_count <= max_retries:
            log(f"{key}: status.json stale (crash retry {retry_count}/{max_retries}) — "
                f"auto-relaunching same session/stage before giving up")
            stage = ticket.get("stage") or "implement"
            prompt = build_crash_retry_prompt(key, stage)
            relaunch_claude(ticket, prompt, stage=stage)
            try:
                jira_client.add_comment(
                    key,
                    f"[aidev] Session crashed (status.json went stale) — auto-retrying "
                    f"(attempt {retry_count}/{max_retries}) before escalating to a human.",
                )
            except Exception as e:
                log(f"{key}: could not post crash-retry comment: {e}")
            return
        mark_failed(
            key,
            f"status.json stuck on 'running' with no update in {STATUS_FILE_STALE_MINUTES}+ "
            f"minutes — likely crashed or blocked without writing needs_input "
            f"(already auto-retried {max_retries} time(s), still crashing)",
            tmux_name=tmux_name,
        )
        return
    if not status:
        log(f"{key}: no status.json yet — still running")
        return

    log(f"{key}: still running")


SELF_REVIEW_LABEL = "aidev-self-review"
CODEX_REVIEW_LABEL = "aidev-codex-review"


def finish_ticket(ticket):
    """Called when a RUNNING Claude session's status.json reports state
    "complete" and it goes idle. What happens next depends on ticket['stage']:
    - 'implement' (the normal first pass) and 'rework' (resumed after a
      human sent the ticket back from review) share this path: push, open
      /update the PR. Then check the `aidev-self-review` tag: present ->
      relaunch Claude for 'self_review' so the configured review skill
      (claude.review_skill) runs against a PR that actually exists (its review
      tags it ai-reviewed and needs a real PR number — running it before
      the PR existed silently no-op'd this); absent -> skip self_review
      entirely and go straight to the codex-tag check. `process_done_ticket`
      re-applies `aidev-self-review` on rework entry when `auto_review` is
      on, mirroring pickup's own first-pass behavior — the codex tag stays
      manual-only on both paths, same as pickup.
    - 'self_review': the tag just did its job — remove it (only after
      success, so a crash mid-stage leaves it in place and the next sweep
      retries; the tag's presence IS the retry state, no counter needed).
      Push whatever Claude changed, then check the `aidev-codex-review`
      tag: present -> run Codex as a non-critical second opinion (see
      lib/codex_runner) and relaunch Claude only if Codex left real
      findings; absent -> skip Codex, hand off to human review directly.
      Bugbot does NOT run here — it runs once, at auto-merge time (right
      before the PR actually lands in master), not on every review pass,
      since a PR can sit in human review for a long time and running it
      here too would just mean running it twice for the same commit.
    - 'codex_check' (Claude just addressed Codex's findings, if any):
      remove the `aidev-codex-review` tag (same after-success-only rule),
      push, hand off to human review.
    - 'auto_merge*' (Claude just resolved a conflict or fixed a Bugbot
      finding as part of the auto-merge sequence): push, then re-enter the
      auto-merge check from the top (see continue_auto_merge).
    """
    cfg = config.load()
    key = ticket["ticket_key"]
    tmux_name = ticket["tmux_session"]
    max_attempts = cfg["claude"].get("max_postprocess_attempts", 5)
    stage = ticket.get("stage") or "implement"

    state.set_state(key, "POSTPROCESS")
    state.clear_crash_retry_count(key)
    log(f"{key}: pushing / opening PR (stage={stage})")
    try:
        pr_url, pr_out = open_pr(ticket)
    except Exception as e:
        attempts = ticket.get("postprocess_attempts", 0) + 1
        if attempts >= max_attempts:
            mark_failed(
                key,
                f"PR step failed after {attempts} attempts: {e}",
                tmux_name=tmux_name,
            )
        else:
            # Transient failure (e.g. a slow git commit/push in a big repo
            # blowing past a subprocess timeout) — the work is already
            # committed in the worktree, so just retry next run. Do NOT
            # kill the tmux session: a live pane is what lets a human
            # `tmux attach` and intervene, and killing it here would make
            # any future STUCK/rework flow for this ticket impossible.
            state.record_postprocess_failure(key, e)
            log(f"{key}: PR step failed (attempt {attempts}/{max_attempts}), will retry: {e}")
        return

    state.clear_postprocess_failure(key)

    if not pr_url:
        mark_review_done(ticket, pr_url, pr_out)
        return

    if stage == "implement" or stage == "rework":
        if _has_label(key, SELF_REVIEW_LABEL):
            review_skill = config.review_skill()
            relaunch_for_stage(ticket, "self_review", pr_url,
                                build_self_review_prompt,
                                notice=f"running self-review ({review_skill}) "
                                "now that the PR exists")
        else:
            log(f"{key}: {SELF_REVIEW_LABEL} not present — skipping self_review")
            _proceed_past_self_review(ticket, pr_url)
        return

    if stage == "self_review":
        _consume_label(key, SELF_REVIEW_LABEL)
        _record_reviewed_sha(ticket)
        _proceed_past_self_review(ticket, pr_url)
        return

    if stage == "codex_check":
        _consume_label(key, CODEX_REVIEW_LABEL)
        mark_review_done(ticket, pr_url, pr_out)
        return

    if stage.startswith("auto_merge"):
        if stage == "auto_merge_recheck":
            _record_reviewed_sha(ticket)
        if stage == "auto_merge_addressing_pr_feedback":
            _record_feedback_checked_sha(ticket)
        continue_auto_merge(ticket, pr_url)
        return

    mark_review_done(ticket, pr_url, pr_out)


def _has_label(key, label):
    try:
        issue = jira_client.get_issue(key, fields=["labels"])
        return label in (issue["fields"].get("labels") or [])
    except Exception as e:
        log(f"{key}: could not check labels ({label}): {e} — treating as absent")
        return False


def _consume_label(key, label):
    """Removes a self-consuming review tag — call ONLY after its stage has
    actually completed successfully. If this raises, the tag is left in
    place and the next sweep simply retries the removal+stage; no separate
    retry-counter needed, since presence/absence of the tag IS the retry
    state."""
    try:
        jira_client.remove_label(key, label)
    except Exception as e:
        log(f"{key}: could not remove {label}: {e}")


def _record_reviewed_sha(ticket):
    """Records the worktree's actual current HEAD as the last-reviewed
    commit — called right after a stage that genuinely ran the review skill
    finishes and pushed. Reads git directly, never Claude's own free-text
    PR comment (that was a real, live bug — see set_last_reviewed_sha's
    docstring). Best-effort: a git failure here just means the next
    auto-merge cycle re-runs review once more, not a hard failure."""
    key = ticket["ticket_key"]
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ticket["worktree_path"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
        state.set_last_reviewed_sha(key, sha)
    except Exception as e:
        log(f"{key}: could not record reviewed SHA: {e}")


def _record_feedback_checked_sha(ticket):
    """Records the worktree's actual current HEAD as checked for unresolved
    PR feedback — called right after auto_merge_addressing_pr_feedback
    completes and pushed (whether Claude found something to fix, or found
    nothing and just verified). This is the completion signal that closes
    the loop opened by check_new_pr_feedback's relaunch: without it,
    process_auto_merge_ticket would see the same (or a fixed, but
    unmarked) head SHA as still-unchecked on every future cycle and
    relaunch this stage forever — the exact class of bug the SHA-based
    review-staleness fix addressed elsewhere in this file."""
    key = ticket["ticket_key"]
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ticket["worktree_path"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
        state.set_last_feedback_checked_sha(key, sha)
    except Exception as e:
        log(f"{key}: could not record feedback-checked SHA: {e}")


def _proceed_past_self_review(ticket, pr_url):
    """Shared by both the 'implement -> skip self_review' path and the
    'self_review just finished' path: check the codex-review tag next."""
    key = ticket["ticket_key"]
    if _has_label(key, CODEX_REVIEW_LABEL):
        run_codex_stage(ticket, pr_url)
    else:
        log(f"{key}: {CODEX_REVIEW_LABEL} not present — skipping codex, handing off to human review")
        mark_review_done(ticket, pr_url)


def relaunch_for_stage(ticket, next_stage, pr_url, prompt_builder, notice=None):
    key = ticket["ticket_key"]
    try:
        issue = jira_client.get_issue(key, fields=["summary"])
        summary = issue["fields"]["summary"]
    except Exception:
        summary = key
    prompt = prompt_builder(key, summary, pr_url)
    session_id = relaunch_claude(ticket, prompt, stage=next_stage)
    with state.db() as conn:
        conn.execute(
            "UPDATE tickets SET session_id = ? WHERE ticket_key = ?",
            (session_id, key),
        )
    state.set_stage(key, next_stage)
    state.reopen_for_rework(key)
    try:
        jira_client.set_state_label(key, "aidev-picked")
    except Exception as e:
        log(f"{key}: label warning: {e}")
    log(f"{key}: relaunched Claude for stage={next_stage} (session {session_id})")
    try:
        extra = f"\n{notice}." if notice else ""
        jira_client.add_comment(
            key,
            f"[aidev] PR: {pr_url}{extra}\nSession ID: {session_id}",
        )
    except Exception as e:
        log(f"{key}: could not post stage-transition comment: {e}")


def run_codex_stage(ticket, pr_url):
    """Non-critical: Codex's success/failure never blocks the pipeline. On
    any failure (disabled, not installed, timeout, no credits, bad output)
    this logs and hands off to human review, same as if Codex had reported
    no findings — Bugbot itself now only runs at auto-merge time (right
    before the PR actually lands in master), not here, to avoid running it
    twice for the same commit across a possibly-long review wait."""
    key = ticket["ticket_key"]
    try:
        issue = jira_client.get_issue(key, fields=["summary"])
        summary = issue["fields"]["summary"]
    except Exception:
        summary = key

    ok, report_rel, detail = codex_runner.run_codex_review(ticket, key, summary, pr_url, log=log)
    if not ok:
        log(f"{key}: codex review skipped ({detail}) — handing off to human review")
        _consume_label(key, CODEX_REVIEW_LABEL)
        mark_review_done(ticket, pr_url)
        return

    report_abs = os.path.join(ticket["worktree_path"], report_rel)
    try:
        with open(report_abs) as f:
            report_text = f.read()
    except OSError as e:
        log(f"{key}: codex report unreadable ({e}) — handing off to human review")
        _consume_label(key, CODEX_REVIEW_LABEL)
        mark_review_done(ticket, pr_url)
        return

    if re.search(r"^##\s*Verdict:\s*CLEAN\b", report_text, re.MULTILINE):
        log(f"{key}: codex review clean — handing off to human review")
        _consume_label(key, CODEX_REVIEW_LABEL)
        mark_review_done(ticket, pr_url)
        return

    log(f"{key}: codex review left findings at {report_rel} — relaunching Claude")

    def prompt_builder(key, summary, pr_url):
        return build_codex_findings_prompt(key, summary, pr_url, report_rel, report_text)

    relaunch_for_stage(ticket, "codex_check", pr_url, prompt_builder,
                        notice=f"a second reviewer (Codex) left notes at {report_rel} — "
                        f"Claude is reading them now")


def build_self_review_prompt(key, summary, pr_url):
    steps = f"1. Run the slash command: {config.review_skill()}"
    return f"""{soul_section()}You just finished implementing Jira ticket {key}: {summary}, and the
orchestrator has pushed your branch and opened the PR: {pr_url}

Now run your own review pass on the diff, in order:
{steps}

`{config.review_skill()}` needs a real PR to tag and comment on — it now has one
({pr_url}), which is why this runs as its own pass instead of before the PR
existed. Commit and push again if you make any changes (same worktree/branch
— do not open a new PR).

Before finishing, post your decisions log as its own PR comment: read
`.claude-code/decisions-{key}.md` (if it doesn't exist, you made no notable
judgment calls — skip this) and `gh pr comment {pr_url} --body '...'` with it
formatted per the "Post a decisions log as its own PR comment" section above
(aidev decisions / My decisions).

When done, write status.json's state to "complete".
"""


def build_codex_findings_prompt(key, summary, pr_url, report_rel, report_text):
    steps = f"1. Run the slash command: {config.review_skill()}"
    return f"""{soul_section()}You are addressing a second reviewer's findings on Jira ticket {key}: {summary}.

This ticket already has an open PR: {pr_url}
You are in the same worktree and branch as before — the existing implementation
is already committed and pushed. Do NOT start over or create a new branch.

Codex (a lower-budget second-opinion reviewer, not authoritative) left this
report at `{report_rel}`:

---
{report_text}
---

Use your own judgment, the same way you would for a human reviewer's comment:
fix what's real, and say explicitly in the commit message why you're leaving
anything you disagree with or consider out of scope. Codex already ran the review skill and pushed its own
fixes — this report lists what it fixed and what it left for a human.

When done, run these steps in order:
{steps}
Stage and commit ALL changes with a clear commit message that references
{key}, then push to the existing branch (this updates the existing PR
automatically — do not open a new PR). If you made no changes because
there was nothing to fix, that's fine — just say so plainly.

When done, write status.json's state to "complete".
"""


def mark_review_done(ticket, pr_url, pr_out=None):
    """Hand off to a human: Jira comment, status transition, label, DONE
    state, kill the now-unneeded tmux session."""
    cfg = config.load()
    key = ticket["ticket_key"]
    tmux_name = ticket["tmux_session"]

    state.set_state(key, "PR_OPENED", pr_url=pr_url)

    comment_lines = [f"\u2705 aidev finished {key}."]
    if pr_url:
        comment_lines.append(f"PR: {pr_url}")
    elif pr_out:
        comment_lines.append(f"PR step output:\n{pr_out}")
    comment_lines.append(f"Worktree: {ticket['worktree_path']}")
    comment_lines.append(f"Session ID: {ticket['session_id']}")
    jira_client.add_comment(key, "\n".join(comment_lines))

    try:
        jira_client.transition_issue(key, cfg["jira"]["review_status"])
    except RuntimeError as e:
        log(f"{key}: transition warning: {e}")
    try:
        jira_client.set_state_label(key, "aidev-done")
    except Exception as e:
        log(f"{key}: label warning: {e}")

    state.set_state(key, "DONE", pr_url=pr_url)
    state.set_stage(key, "implement")  # reset for any future rework cycle
    procs.tmux_kill(tmux_name)
    log(f"{key}: done")
    notify(f"aidev: {key} done", "PR opened" if pr_url else "PR step had no URL — check comment",
           key=key, pr_url=pr_url)


def process_stuck_ticket(ticket):
    cfg = config.load()
    key = ticket["ticket_key"]
    tmux_name = ticket["tmux_session"]
    max_hours = cfg["claude"].get("max_running_hours")

    if not procs.tmux_session_exists(tmux_name):
        mark_failed(key, "tmux session disappeared while STUCK (crash, reboot, or manual kill)")
        return

    elapsed = state.seconds_running(ticket)
    if max_hours and elapsed and elapsed > max_hours * 3600:
        mark_failed(
            key,
            f"stuck waiting for a reply past max_running_hours ({max_hours}h) with no response",
            tmux_name=tmux_name,
        )
        return

    try:
        comments = jira_client.get_comments(key)
    except Exception as e:
        log(f"{key}: could not fetch comments: {e}")
        return

    if not comments:
        return

    last = comments[-1]
    body_text = jira_client.plain_description({"fields": {"description": last["body"]}})
    if body_text.strip().startswith("[aidev]"):
        # Last comment is our own — no human reply yet.
        return

    last_seen = ticket.get("last_comment_id")
    if last_seen == last["id"]:
        return

    log(f"{key}: human reply found — resuming session with the reply")
    procs.tmux_send(tmux_name, body_text.replace("\n", " "))
    state.set_last_comment_id(key, last["id"])
    state.clear_stuck(key)
    try:
        jira_client.set_state_label(key, "aidev-picked")
    except Exception as e:
        log(f"{key}: label warning: {e}")


def relaunch_claude(ticket, prompt, stage=None):
    """Resumes the ticket's existing Claude Code session (same conversation
    memory — no re-orientation cost) instead of minting a fresh one, unless
    no prior session_id is on record (first-ever relaunch for this ticket),
    in which case it falls back to a fresh --session-id. `stage` selects the
    --model override from config.yaml's claude.models block, if set for that
    stage; omitted/no match means no --model flag (account default)."""
    worktree_path = ticket["worktree_path"]
    tmux_name = ticket["tmux_session"]
    cfg = config.load()
    prior_session_id = ticket.get("session_id")

    if procs.tmux_session_exists(tmux_name):
        procs.tmux_kill(tmux_name)
    procs.tmux_new_session(tmux_name)

    prompt_file = os.path.join(worktree_path, ".claude-code", ".aidev_prompt.txt")
    os.makedirs(os.path.dirname(prompt_file), exist_ok=True)
    with open(prompt_file, "w") as f:
        f.write(prompt)

    # Reset status.json to a fresh "running" state stamped with *now*, not
    # left holding whatever the previous (possibly crashed) session last
    # wrote. Without this, a relaunch after a stale-status crash inherits an
    # already-10-minutes-old timestamp, and the very next monitor sweep
    # (every ~3 min) sees it as instantly stale again before the new Claude
    # process has had any chance to write its own first update — a fast
    # fail/relaunch/fail loop. Caught live: RND-14824 relaunched at 18:23:37,
    # marked FAILED again at 18:24:00 (23 seconds later) on this exact bug.
    status_file = os.path.join(worktree_path, ".claude-code", "status.json")
    try:
        with open(status_file, "w") as f:
            json.dump(
                {
                    "state": "running",
                    "detail": "resumed" if prior_session_id else "starting",
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                f,
            )
    except Exception as e:
        log(f"{ticket['ticket_key']}: could not reset status.json on relaunch: {e}")

    skip_perms = "--dangerously-skip-permissions" if cfg["claude"]["dangerously_skip_permissions"] else ""
    model = (cfg["claude"].get("models") or {}).get(stage) if stage else None
    model_flag = f"--model {model} " if model else ""
    if prior_session_id:
        session_flag = f"--resume {prior_session_id}"
        session_id = prior_session_id
    else:
        session_id = str(uuid.uuid4())
        session_flag = f"--session-id {session_id}"
    claude_cmd = (
        f"cd {worktree_path} && "
        f"DISABLE_AUTOUPDATER=1 claude {session_flag} {model_flag}{skip_perms} "
        f"\"$(cat .claude-code/.aidev_prompt.txt)\""
    )
    if not procs.launch_claude_verified(tmux_name, worktree_path, lambda: claude_cmd):
        log(f"{ticket['ticket_key']}: claude never verifiably started in {worktree_path} "
            f"after retrying — likely the shell-startup race (see procs.py)")
    state.record_stage_transition(ticket["ticket_key"], session_id, stage or "implement", model)
    return session_id


def build_rework_prompt(key, summary, pr_url):
    """No `feedback` parameter — this is the same session (Step B --resume)
    that implemented the ticket originally, so it already has conversation
    memory of its own prior comments and doesn't need to be told which of
    them to ignore. Mirrors build_task_prompt's minimal handoff style:
    ticket key, a pointer, trust the session to read and judge live context
    itself — including the PR, not just Jira, since rework happens on an
    already-open PR that can independently have picked up new GitHub review
    comments, a changed/failing CI run, or a merge conflict with master
    since it was last touched."""
    steps = f"1. Run the slash command: {config.review_skill()}"
    link = f"{config.load()['jira']['base_url']}/browse/{key}"
    live_fetch_note = jira_live_fetch_note(key, link)
    return f"""{soul_section()}You are resuming work on Jira ticket {key}: {summary}
Link: {link}

This is the same session that implemented this ticket originally — you have
full memory of what you did and why. A human moved this ticket back to
in-progress and/or commented, which means there's new feedback to address.
You already know which of your own past comments to ignore; nothing here
duplicates that.

This ticket already has an open PR: {pr_url}
You are in the same worktree and branch as before — the existing implementation
is already committed and pushed. Do NOT start over or create a new branch.

Re-fetch and read what's changed, from BOTH sources, not just Jira:
- Jira: {live_fetch_note}
- The PR itself: run `gh pr view {pr_url}` for new review comments/threads,
  `gh pr checks {pr_url}` for CI status, and check whether it still merges
  cleanly against `master` (a merge conflict since you last touched this
  can happen independently of any comment).

If the original ticket referenced shared context files (repo root's
`.claude-code/`, Notion pages via `/notion`, Slack links), re-check them for
anything the feedback might relate to, and keep appending any new
decisions/learnings there — repo root's `.claude-code/`, not this worktree's
(it gets deleted on cleanup), never overwrite existing content, only add.

Task:
- Make the changes needed to address whatever you find, in this worktree.
- When done, run these steps in order:
{steps}
- Stage and commit ALL changes with a clear commit message that references {key}.
- Then push to the existing branch (this updates the existing PR automatically
  — do not open a new PR).
- Add a line to the PR (a new comment, or fold into the commit message)
  stating which Jira source you worked from: "Jira context: live-fetched
  via <tool>" or "Jira context: pipeline's embedded snapshot only (could
  not live-fetch: <reason>)".

If you are blocked and need clarification, follow the same batching rule as
the main implementation stage (see "`needs_input` fires at most once
per ticket" above): research, keep working with a provisional answer, log to
`.claude-code/decisions-{key}.md`, and only write one batched
status.json `needs_input` update at the end if anything is still open.

When you are fully done, committed, and pushed, write status.json's state
to "complete".
"""


def process_done_ticket(ticket):
    """DONE tickets whose Jira status was manually moved back to
    in_progress_status are review-feedback re-opens: relaunch Claude in the
    same worktree/branch to address the feedback. Re-applies aidev-self-review
    (per auto_review config) before relaunching so finish_ticket's shared
    implement/rework path re-runs self-review the same way a first pass
    would. aidev-auto-merge is a standing signature, not consumed by
    rework — deliberately left untouched here, so once the rework lands
    back on review_status + aidev-done via mark_review_done, the very next
    DONE-sweep auto-merge check picks it back up and restarts the whole
    approval sequence from the top (base/feedback/CI checks, then a fresh
    Bugbot request), not from wherever it left off before rework.

    Also checks, every cycle and independent of the `aidev-auto-merge`
    label, whether the PR was merged directly on GitHub by a human — a
    ticket never tagged for auto-merge (or merged while a rework/base check
    was mid-flight) still needs its Jira ticket moved to Done and its
    worktree cleaned up; nothing else in the pipeline watches for a plain
    manual merge outside the auto-merge flow. Detected straight from the
    PR's own `state` field via `gh`, never inferred from comment text."""
    cfg = config.load()
    key = ticket["ticket_key"]
    pr_url = ticket.get("pr_url")

    if pr_url:
        details = github.get_pr_details(ticket["repo_path"], pr_url)
        if details and details.get("state") == "MERGED":
            log(f"{key}: PR already merged (by a human, outside auto-merge) — finishing up")
            try:
                jira_client.transition_issue(key, "Done")
            except Exception as e:
                log(f"{key}: could not transition to Done after human merge: {e}")
            try:
                jira_client.remove_label(key, AUTO_MERGE_LABEL)
            except Exception as e:
                log(f"{key}: could not remove {AUTO_MERGE_LABEL} label after human merge: {e}")
            cleanup_worktree(ticket)
            state.set_archived(key)
            notify(f"aidev: {key} merged", "Merged directly on GitHub (not via auto-merge)",
                   key=key, pr_url=pr_url)
            return

    try:
        issue = jira_client.get_issue(key, fields=["status", "summary"])
    except Exception as e:
        log(f"{key}: could not fetch issue for rework check: {e}")
        return

    current_status = issue["fields"]["status"]["name"]
    if current_status.lower() != cfg["jira"]["in_progress_status"].lower():
        return  # still In Review / Done / whatever — nothing to do

    log(f"{key}: moved back to {current_status} after DONE — treating as rework request")

    if cfg["claude"].get("auto_review", True):
        try:
            jira_client.add_label(key, SELF_REVIEW_LABEL)
        except Exception as e:
            log(f"{key}: could not re-apply {SELF_REVIEW_LABEL} for rework: {e}")

    summary = issue["fields"]["summary"]
    prompt = build_rework_prompt(key, summary, ticket.get("pr_url") or "")

    session_id = relaunch_claude(ticket, prompt, stage="rework")
    with state.db() as conn:
        conn.execute(
            "UPDATE tickets SET session_id = ? WHERE ticket_key = ?",
            (session_id, key),
        )
    state.reopen_for_rework(key)

    try:
        jira_client.set_state_label(key, "aidev-picked")
    except Exception as e:
        log(f"{key}: label warning (rework): {e}")
    try:
        jira_client.add_comment(
            key,
            f"[aidev] Picked up your feedback, resuming work in the same worktree/branch.\n"
            f"Session ID: {session_id}",
        )
    except Exception as e:
        log(f"{key}: could not post rework comment: {e}")

    notify(f"aidev: {key} rework started", "resuming session with full memory of prior work",
           key=key, pr_url=ticket.get("pr_url"))


AUTO_MERGE_LABEL = "aidev-auto-merge"
REQUIRED_LABEL = "ai-reviewed"


def escalate_auto_merge(ticket, reason, waiting_on_human=False):
    """Stops the auto-merge sequence and tells the human why. When
    `waiting_on_human` is True this is a pause, not a dead end — Cursor
    itself deferred to a human GitHub review (its own judgment call, not
    something fixable by pushing code), so the ticket stays DONE with stage
    `auto_merge_waiting_human` and every future DONE-sweep cycle re-checks
    whether that approval landed, resuming automatically the moment it has.
    Anything else genuinely can't proceed without a person looking at it, so
    the label is removed — re-tag `aidev-auto-merge` once it's addressed to
    try again, same as tagging a ticket in the first place."""
    key = ticket["ticket_key"]
    log(f"{key}: auto-merge {'waiting on human review' if waiting_on_human else 'stuck'} — {reason}")
    try:
        jira_client.add_comment(
            key,
            f"[aidev] auto-merge {'paused, waiting on a human GitHub review' if waiting_on_human else 'stuck'}: {reason}",
        )
    except Exception as e:
        log(f"{key}: could not post auto-merge status comment: {e}")
    if waiting_on_human:
        state.set_stage(key, "auto_merge_waiting_human")
    else:
        try:
            jira_client.remove_label(key, AUTO_MERGE_LABEL)
        except Exception as e:
            log(f"{key}: could not remove {AUTO_MERGE_LABEL} label: {e}")
        state.set_stage(key, "implement")  # back to the normal DONE baseline
    # Belt-and-suspenders: every caller of process_auto_merge_ticket expects
    # to operate on a DONE ticket (that's what the sweep iterates), but a
    # caller reached via continue_auto_merge may have come from POSTPROCESS
    # — make sure escalating never silently drops the ticket out of DONE.
    state.set_state(key, "DONE")
    notify(f"aidev: {key} auto-merge {'waiting on human' if waiting_on_human else 'stuck'}", reason[:200],
           key=key, pr_url=ticket.get("pr_url"))


def build_auto_merge_conflict_prompt(key, summary, pr_url):
    return f"""{soul_section()}You are resolving a merge conflict so Jira ticket {key}: {summary}'s PR
({pr_url}) can merge to master as part of an auto-merge sequence. You are in
the same worktree/branch as before — the existing implementation is already
committed and pushed. Do NOT start over or create a new branch.

`git merge origin/master` in this worktree and resolve any conflicts. Prefer
keeping both sides where the conflicting hunks are genuinely independent
additions (e.g. two unrelated fields added near each other) — that is the
common case for a stacked PR whose sibling branches merged ahead of it.
Verify the resolution actually builds/typechecks before committing (this
repo's own convention — `npm run type:check` or equivalent). If the conflict
is semantically ambiguous (the two sides changed the same behavior in
different, incompatible ways) do NOT guess — write status.json with
`state: "needs_input"` and `detail` describing the ambiguous conflict in one
line, then stop.

Otherwise, commit the resolution and push to the existing branch (this
updates the existing PR — do not open a new PR), then write status.json's
state to "complete".
"""


def build_auto_merge_bugbot_fix_prompt(key, summary, pr_url, bugbot_review_text):
    return f"""{soul_section()}You are addressing a Cursor Bugbot finding on Jira ticket {key}: {summary}'s
PR ({pr_url}), as part of an auto-merge sequence. You are in the same
worktree/branch as before — the existing implementation is already committed
and pushed. Do NOT start over or create a new branch.

Bugbot's latest review on this PR:
---
{bugbot_review_text}
---

Fix what's real, same judgment as any other Bugbot pass — explain in the
commit message why you're leaving anything you disagree with or consider a
false positive, rather than silently ignoring it. Resolve the GitHub review
threads for whatever you addressed (see the "Cursor Bugbot" section above
for the `gh api graphql` commands).

Commit and push to the existing branch (this updates the existing PR — do
not open a new PR), then write status.json's state to "complete".
"""


def build_auto_merge_approval_prompt(key, summary, pr_url, verdict_text, repeat_count=1):
    repeat_note = ""
    if repeat_count >= 2:
        repeat_note = f"""
This exact verdict (same reason, not just similar wording) has now come
back {repeat_count} times in a row across separate `/approve` attempts —
whatever caused it did NOT change between attempts. If the reason is
something re-approving genuinely cannot fix (e.g. a structural gate like
"too many lines changed" — that number doesn't change just by asking
again), do not comment `/approve` again; that's exactly the case the
"unambiguous human review required" rule below is for, even if the verdict
text itself doesn't use those words. Splitting the PR, or another concrete
action, might be the real fix here — if you can't take that action
yourself, this is a genuine needs_input case.
"""
    return f"""{soul_section()}You are handling Cursor's Approval Agent verdict on Jira ticket {key}: {summary}'s
PR ({pr_url}), as part of an auto-merge sequence. You are in the same
worktree/branch as before — the existing implementation is already committed
and pushed. Do NOT start over or create a new branch.

Cursor's latest Approval Agent verdict on this PR (NOT an approval):
---
{verdict_text}
---
{repeat_note}
Use your own judgment reading this verdict, the same way you'd read a human
reviewer's comment — there is no fixed rule for what this text says, Cursor's
wording varies run to run. In particular: a human reviewer being assigned to
the PR (e.g. "reviewers assigned", "reviewers were already assigned") is NOT
by itself a reason to stop — Cursor itself still approves plenty of PRs that
have an assigned reviewer (see e.g. PR #5789 for a real example: assigned +
still approved shortly after on a repeat verdict). Only treat a human
reviewer as required if the verdict explicitly says human review is
mandatory/already required with no further automated path (e.g. it names a
completed/pending human sign-off as the only remaining step, not just that
someone was tagged in passing).

- If the verdict tells you to run something ({config.review_skill()}, a
  fresh Bugbot pass, etc.) and you haven't already done that for the
  current commit, do it now — the same {config.review_skill()} skill
  from earlier stages is available to you here. Once you've taken that
  real action (code changed and pushed, a label added/removed, a skill run
  that genuinely wasn't run before on this commit), THEN comment `/approve`
  to get a fresh verdict — the action is what justifies asking again, not
  the other way round.
- If the verdict names nothing actionable, or already reflects work you've
  already done for this exact commit (you already ran {config.review_skill()}, the
  label's already set, etc.) — do NOT comment `/approve` again. Nothing
  changed since the last ask, so asking again would just get the same
  answer for no reason. Instead write status.json with `state:
  "needs_input"` and `detail`: one line on what the verdict says and why
  nothing here is actionable, then stop.
- Only if the verdict unambiguously states that a human review is required
  and already exists/is pending as the sole remaining path (not just "a
  reviewer is assigned" in passing) — do not comment `/approve` again, and
  instead write status.json with `state: "needs_input"` and `detail`:
  quote the part of the verdict that says human review is required, then
  stop.

Otherwise, once you've taken whatever action applies (running a skill,
committing/pushing if you changed anything, commenting `/approve`), write
status.json's state to "complete".
"""


_VERDICT_SIGNATURE_RE = re.compile(r"Risk: \w+\. (.+?)(?:<div>|$)", re.DOTALL)


def _verdict_signature(body):
    """Normalizes an Approval Agent verdict body down to its actual
    judgment text (the 'Risk: ... Not approved — <reason>' sentence),
    stripping the HTML/footer boilerplate that's identical on every review,
    the run-specific automation IDs, and any digits, so two verdicts with
    the same substantive reason compare as equal even if the exact number
    drifts slightly between runs (caught live on PR #5759: '1134 lines...'
    then '1133 lines...' one commit later — still the same '1000-line size
    gate' reason, not a resolved one, but an exact-string compare missed
    it and let the repeat count reset to 1)."""
    body = body or ""
    m = _VERDICT_SIGNATURE_RE.search(body)
    text = m.group(1) if m else body
    text = re.sub(r"[Rr]eviewers? (will be|are already|were already) assigned\.?", "", text)
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _repeated_approval_verdict_count(pr_details, marker="Approval Agent"):
    """Counts how many of the most recent consecutive Approval Agent
    verdicts (by submission order) share the same normalized signature —
    i.e. how many times in a row Cursor has given the identical reason for
    not approving. A verdict repeating unchanged across several /approve
    attempts (caught live on PR #5759: the same 'over the 1000-line limit'
    structural size gate, 5 times over ~50 minutes) means re-approving is
    not going to change the outcome — that's a real stuck-waiting-on-human
    case, not something another /approve or another Claude judgment pass
    can resolve, since the size gate isn't fixable by discussion."""
    verdicts = [r for r in pr_details.get("reviews", []) if marker in (r.get("body") or "")]
    if not verdicts:
        return 0
    latest_sig = _verdict_signature(verdicts[-1].get("body"))
    if not latest_sig:
        return 0
    count = 0
    for r in reversed(verdicts):
        if _verdict_signature(r.get("body")) == latest_sig:
            count += 1
        else:
            break
    return count


def _latest_review_matching(pr_details, marker):
    """Returns the most recent review (by submission order, which is what
    `gh pr view --json reviews` returns) whose body contains `marker`, or
    None. Cursor posts both Bugbot and Approval Agent verdicts as reviews
    from the same `cursor` author, so the body marker — not the author — is
    what actually distinguishes them (`BUGBOT_REVIEW` vs `Cursor Approval
    Agent`)."""
    matches = [r for r in pr_details.get("reviews", []) if marker in (r.get("body") or "")]
    return matches[-1] if matches else None


def _bugbot_review_commit(review):
    """Returns the commit SHA Bugbot's review was actually submitted
    against — read directly from GitHub's own `commit.oid` field on the
    review object (`gh pr view --json reviews` already includes it), not
    parsed out of Bugbot's free-text footer ("Reviewed by Cursor Bugbot for
    commit <sha>."). That text is Cursor's own wording, not something we
    control, and CLAUDE.md's "never regex a running process's free text"
    rule applies here just as much as it did to the review-staleness bug —
    GitHub already hands us the real value as structured data, so there was
    never a need to re-derive it from prose in the first place."""
    if not review:
        return None
    return (review.get("commit") or {}).get("oid")


def _custom_review_covers_head(ticket, head_sha):
    """The `ai-reviewed` label (unlike Bugbot's review comment) carries no
    commit SHA — GitHub labels aren't tied to a commit, so once applied it
    stays applied forever even after new commits land (e.g. a conflict
    resolution push in the middle of an auto-merge run). Caught live: Cursor's
    own Approval Agent correctly refused to approve a PR with a stale
    `ai-reviewed` label and asked for review to be re-run — this
    mirrors that same judgment in our own gate instead of trusting the label
    alone.

    Compares head_sha against ticket['last_reviewed_sha'] — a DB field set
    directly from `git rev-parse HEAD` right when a review stage actually
    completes (see _record_reviewed_sha/state.set_last_reviewed_sha). This
    used to parse the exact reviewed commit out of Claude's own free-text
    "aidev decisions: ... commit(s) [up to] <sha>" PR comment via regex —
    a real, live bug: Claude phrased one comment "current HEAD 6d162f4"
    instead of "commit 6d162f4", the regex silently stopped matching
    forever, and RND-14813/PR #5759 cycled auto_merge_recheck 34 times over
    ~19 hours before max_running_hours finally killed it. Free-running-text
    from a process we don't control (Claude's own comment wording, just
    like Cursor's/Bugbot's) is never a safe parse target — see CLAUDE.md's
    "Never regex-parse a running process's free text" rule. No matching
    recorded SHA counts as NOT covering head — conservative by design, a
    false "needs re-review" is cheap (one extra Claude pass) but a false
    "clean" would ship an unreviewed commit."""
    if not head_sha:
        return False
    reviewed_sha = ticket.get("last_reviewed_sha")
    if not reviewed_sha:
        return False
    return head_sha == reviewed_sha


def _last_approve_request_time(repo_path, pr_url):
    """Returns the createdAt of the most recent `/approve` PR comment (ours
    or anyone's — only aidev and humans post this convention), or None if
    it's never been requested."""
    comments = github.get_pr_comments(repo_path, pr_url)
    approve_comments = [c for c in comments if (c.get("body") or "").strip() == "/approve"]
    return approve_comments[-1]["createdAt"] if approve_comments else None


def build_auto_merge_ci_failure_prompt(key, summary, pr_url, failed_checks):
    checks_block = "\n".join(
        f"- {c['name']}: {c.get('conclusion') or c.get('status') or 'unknown'}"
        + (f" ({c['detailsUrl']})" if c.get("detailsUrl") else "")
        for c in failed_checks
    )
    changelog_hint = ""
    if any("changelog" in (c.get("name") or "").lower() for c in failed_checks):
        changelog_hint = f"""
Note: the "changelog" check is failing. That one specifically means this PR
touches source files with no changelog entry and isn't tagged `no-changelog`.
Use your own judgment on the actual diff: if the change is genuinely
customer-facing, add a real entry (`npm run changelog` or equivalent, check
`.agents/rules/changelog.md` if unsure of format); if it's internal/test/
tooling-only and too small to matter to users, tag the PR yourself
(`gh pr edit {pr_url} --add-label no-changelog`) with a one-line reason in a
PR comment. Don't guess if it's genuinely ambiguous — see below.
"""
    return f"""{soul_section()}You are addressing failing CI checks on Jira ticket {key}: {summary}'s
PR ({pr_url}), as part of an auto-merge sequence. You are in the same
worktree/branch as before — the existing implementation is already committed
and pushed. Do NOT start over or create a new branch.

These checks are currently failing (red, not just pending):
{checks_block}
{changelog_hint}
Use your own judgment, the same way you would investigate any CI failure —
read the actual failure output (`gh run view` / the check's details URL,
whatever applies) before touching anything. Common cases:
- A genuine test/lint/type failure caused by your own change — fix it.
- A flaky/unrelated failure (infra hiccup, a pre-existing failure on master
  unrelated to this diff) — do not just retry blindly; if you're confident
  it's unrelated, note that in a PR comment and leave it, since re-triggering
  a workflow run isn't necessarily something you can do from here.
- The changelog check — see the note above.

If you fix something, commit and push to the existing branch (this updates
the existing PR — do not open a new PR). If you're genuinely unsure what a
failure means or how to address it, do not guess — write status.json with
`state: "needs_input"` and `detail`: one line on which check and what's
unclear, then stop.

Otherwise, once you've taken whatever action applies, write status.json's
state to "complete".
"""


def _classify_checks(pr_details):
    """Splits statusCheckRollup into (red, pending) — green/skipped/neutral
    checks are dropped entirely, they need no action. A check with no
    conclusion yet (still running/queued) is pending, not failing — this
    pipeline should never treat "not done yet" as "broken"; only an
    explicit non-success conclusion counts as red and worth Claude's
    attention.

    De-dupes by check name first, keeping only the run with the latest
    completedAt/startedAt. GitHub's rollup does NOT drop a superseded run
    when a workflow is re-triggered by a label change (only a new push
    reliably does that) — a `labeled`/`unlabeled` re-run of the same check
    name can leave an old FAILURE run sitting in the rollup right alongside
    a newer SUCCESS/SKIPPED one for the identical check. Caught live on PR
    #5779/RND-14816: the "changelog" check had a stale FAILURE from before
    the `no-changelog` label was applied, and a fresh SKIPPED run from 3
    seconds after — both present in the same rollup response. Without this
    de-dupe, the stale entry alone caused an infinite relaunch loop (every
    ~3 min, forever) even though the check had already genuinely passed."""
    latest_by_name = {}
    for c in pr_details.get("statusCheckRollup", []):
        name = c.get("name")
        if not name:
            continue
        ts = c.get("completedAt") or c.get("startedAt") or ""
        if name not in latest_by_name or ts > (latest_by_name[name].get("completedAt") or latest_by_name[name].get("startedAt") or ""):
            latest_by_name[name] = c

    red, pending = [], []
    for c in latest_by_name.values():
        conclusion = (c.get("conclusion") or "").upper()
        status = (c.get("status") or "").upper()
        if conclusion in ("SUCCESS", "SKIPPED", "NEUTRAL"):
            continue
        if not conclusion and status and status != "COMPLETED":
            pending.append(c)
            continue
        if conclusion in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"):
            red.append(c)
        else:
            pending.append(c)
    return red, pending


def check_new_pr_feedback(ticket, pr_url, head_sha):
    """Judgment call, not a text/author heuristic (per CLAUDE.md's "never
    regex a running process's free text" rule): hands Claude every current
    PR comment and review verbatim and asks it to decide whether anything
    substantive is still unaddressed at the current HEAD — a human comment
    with no Jira status change (invisible to process_done_ticket's rework
    path), or a Bugbot finding that's about to become stale and get
    silently superseded by a fresh run without ever having been read.
    Always relaunches (the caller always returns right after calling this)
    — the SHA is marked checked later, in finish_ticket, once this
    relaunched session actually completes and pushes
    (_record_feedback_checked_sha), not here."""
    def prompt_builder(key, summary, pr_url):
        return build_pr_feedback_check_prompt(key, summary, pr_url, head_sha)

    log(f"{ticket['ticket_key']}: auto-merge — checking for unresolved PR feedback before continuing")
    relaunch_for_stage(ticket, "auto_merge_addressing_pr_feedback", pr_url, prompt_builder,
                        notice="auto-merge — checking whether any PR comment/review still needs addressing")


def build_pr_feedback_check_prompt(key, summary, pr_url, head_sha):
    return f"""{soul_section()}You are about to continue the auto-merge sequence for Jira ticket
{key}: {summary}, PR: {pr_url}, at commit {head_sha}. Before anything else runs (Bugbot,
approval), read every comment and review currently on this PR yourself:

    gh pr view {pr_url} --json comments,reviews

Some of these are your own past output (decisions-log comments, rework acknowledgments) —
you'll recognize your own voice/format; don't treat those as feedback to act on. Everything
else — a human's line comment or review, or a Bugbot/Cursor finding from an earlier commit
that nobody has actually addressed yet — is what you're checking for.

Decide for yourself, reading the actual content, whether there is real substantive feedback
here that hasn't been resolved by the current code at HEAD. Don't pattern-match specific
words or authors — read it the way a human reviewer coming back to this PR would, and use
your own judgment about what still needs doing versus what's already handled or superseded.

- If you find something real and unaddressed: fix it (or explain in a PR comment why you're
  not, if you genuinely disagree), commit, and push to this same branch — do not open a new
  PR or start over.
- If everything you find is either already resolved by the current code, is your own past
  output, or is genuinely stale (e.g. an old Bugbot finding whose flagged code no longer
  exists), say so briefly and do nothing else.

When done, write status.json's state to "complete".
"""


def process_auto_merge_ticket(ticket):
    """DONE tickets tagged `aidev-auto-merge` — see the skill's "Auto-merge
    a stacked PR chain" section for the full design and why. Runs entirely
    off `gh`/git; only spins up a Claude session (via relaunch_claude, same
    as the rework loop) for the two sub-steps that genuinely need judgment:
    resolving a real merge conflict, and fixing a real Bugbot finding.
    Everything else here is mechanical and safe to retry every cycle."""
    key = ticket["ticket_key"]
    pr_url = ticket.get("pr_url")
    repo_path = ticket["repo_path"]
    branch = ticket["branch"]

    try:
        cfg = config.load()
        issue = jira_client.get_issue(key, fields=["status", "labels"])
        status_name = issue["fields"]["status"]["name"]
        labels = issue["fields"]["labels"]
    except Exception as e:
        log(f"{key}: could not check status/labels for auto-merge: {e}")
        return
    if AUTO_MERGE_LABEL not in labels:
        return
    # Never act on a ticket that isn't genuinely done and handed off —
    # `state.all_in_state("DONE")` reflects the local state DB, which could
    # in principle drift from Jira (a rework got triggered concurrently, a
    # human moved it manually); re-verify against Jira itself before ever
    # touching the PR. Running work must never be auto-merged.
    if status_name.lower() != cfg["jira"]["review_status"].lower() or "aidev-done" not in labels:
        log(f"{key}: auto-merge label present but ticket is not In Review + aidev-done "
            f"(status={status_name}, labels={labels}) — skipping, not touching running work")
        return

    if not pr_url:
        escalate_auto_merge(ticket, "no PR URL recorded for this ticket")
        return

    details = github.get_pr_details(repo_path, pr_url)
    if not details:
        log(f"{key}: could not fetch PR details this cycle, will retry")
        return

    # --- already merged by a human, outside this flow ---------------------
    # A human can merge the PR directly on GitHub at any point (e.g. while
    # auto-merge is mid-sequence, or before it ever ran) — that's a valid
    # way to finish a ticket, not an error. Detect it directly from the PR's
    # own `state` field (never inferred from prose/comments), transition
    # Jira to Done the same way the bot's own merge path does below, and
    # clean up the worktree immediately rather than waiting for the
    # terminal-Jira-status cleanup sweep to notice on its next cycle.
    if details.get("state") == "MERGED":
        log(f"{key}: PR already merged (by a human, outside auto-merge) — finishing up")
        try:
            jira_client.transition_issue(key, "Done")
        except Exception as e:
            log(f"{key}: could not transition to Done after human merge: {e}")
        try:
            jira_client.remove_label(key, AUTO_MERGE_LABEL)
        except Exception as e:
            log(f"{key}: could not remove {AUTO_MERGE_LABEL} label after human merge: {e}")
        cleanup_worktree(ticket)
        state.set_archived(key)
        notify(f"aidev: {key} merged", "Merged directly on GitHub (not via auto-merge)",
               key=key, pr_url=pr_url)
        return

    if details["baseRefName"] != "master":
        log(f"{key}: auto-merge — base is {details['baseRefName']}, not master yet, skipping")
        return

    log(f"{key}: auto-merge — base is master, proceeding")

    # --- unresolved feedback check ---------------------------------------
    # Before touching Bugbot at all: is there human feedback, or a stale
    # Bugbot finding from before this pass, that nobody has actually acted
    # on yet? A GitHub-only comment (no Jira status change) is invisible to
    # process_done_ticket's rework path, so without this check it would
    # never be surfaced to Claude at all — self_review doesn't read PR
    # comments, and by the time auto-merge's own Bugbot loop runs, an old
    # Bugbot review is simply superseded by a fresh one, its actual text
    # never read by anything. Gated on head_sha, not a boolean, so a later
    # push re-triggers this — same pattern as last_reviewed_sha. The SHA is
    # marked checked from finish_ticket once the relaunched session
    # actually completes and pushes (_record_feedback_checked_sha), not
    # here — this call always relaunches and returns, so marking it here
    # would never run and this stage would loop forever.
    head_sha_for_feedback = details.get("headRefOid", "")
    if head_sha_for_feedback and ticket.get("last_feedback_checked_sha") != head_sha_for_feedback:
        check_new_pr_feedback(ticket, pr_url, head_sha_for_feedback)
        return

    # --- conflict check -------------------------------------------------
    if details["mergeable"] == "CONFLICTING" or details["mergeStateStatus"] == "DIRTY":
        log(f"{key}: auto-merge — merge conflict, relaunching Claude to resolve")
        relaunch_for_stage(ticket, "auto_merge_resolving_conflict", pr_url, build_auto_merge_conflict_prompt,
                            notice="auto-merge hit a conflict with master — resolving it")
        return

    # --- CI checks: red is Claude's to judge, yellow is just a wait ------
    # Runs BEFORE Bugbot deliberately: no point spending a Bugbot review
    # cycle (and burning its request quota/wait time) against code that's
    # already known-broken by the repo's own test suite — fix real
    # failures first, then let Bugbot review the actually-final code once.
    # Caught live on PR #5814/RND-14828: Bugbot was triggered while
    # run-tests/unit-ui was still FAILURE, because CI used to be checked
    # only after the Bugbot loop.
    # No hardcoded "changelog specifically means X" branch here — a check
    # being red (failing tests, lint, changelog enforcer, anything) is not
    # automatically "stuck"; Claude reads the actual failures and decides
    # what to do, same non-deterministic pattern as the Bugbot/approval
    # gates. Only a check still queued/running is a genuine "wait, not
    # broken" — never treated as needing Claude's attention.
    red_checks, pending_checks = _classify_checks(details)
    if red_checks:
        log(f"{key}: auto-merge — {len(red_checks)} check(s) red "
            f"({', '.join(c['name'] for c in red_checks)}), relaunching Claude to judge them")
        def ci_prompt_builder(key, summary, pr_url, _checks=red_checks):
            return build_auto_merge_ci_failure_prompt(key, summary, pr_url, _checks)
        relaunch_for_stage(ticket, "auto_merge_fixing_ci", pr_url, ci_prompt_builder,
                            notice=f"auto-merge — {len(red_checks)} CI check(s) red, judging and addressing them")
        return
    if pending_checks:
        log(f"{key}: auto-merge — {len(pending_checks)} check(s) still running "
            f"({', '.join(c['name'] for c in pending_checks)}), waiting")
        return

    # --- bugbot loop ------------------------------------------------------
    # Bugbot's own "clean" marker text, and its "real findings" text, both
    # live in the comment body — see SOUL.md's Cursor Bugbot section for the
    # exact conventions this mirrors. A "clean" review only counts if it was
    # actually run against the PR's CURRENT head commit — Bugbot's review
    # comment embeds the SHA it reviewed ("...for commit <sha>."), and a
    # push since then (e.g. this same sequence's own conflict-resolution
    # commit) makes an old clean verdict stale and worthless as a safety
    # check. Without this, a real bug: resolving a conflict, pushing a new
    # commit, then treating yesterday's "clean" Bugbot review of the
    # pre-conflict-fix commit as still valid.
    head_sha = details.get("headRefOid", "")
    latest_bugbot = _latest_review_matching(details, "BUGBOT_REVIEW")
    bugbot_reviewed_current_head = (
        bool(latest_bugbot)
        and bool(head_sha)
        and head_sha.startswith(_bugbot_review_commit(latest_bugbot) or "\0")
    )
    bugbot_clean = (
        bugbot_reviewed_current_head
        and "found no new issues" in (latest_bugbot.get("body") or "").lower()
    )
    bugbot_findings_text = (
        latest_bugbot.get("body") if bugbot_reviewed_current_head and not bugbot_clean else None
    )
    if bugbot_findings_text:
        log(f"{key}: auto-merge — Bugbot left findings, relaunching Claude to address them")
        def prompt_builder(key, summary, pr_url, _text=bugbot_findings_text):
            return build_auto_merge_bugbot_fix_prompt(key, summary, pr_url, _text)
        relaunch_for_stage(ticket, "auto_merge_fixing_bugbot", pr_url, prompt_builder,
                            notice="auto-merge — Bugbot left findings, fixing them")
        return

    if not bugbot_clean:
        # The "Cursor Bugbot" entry in statusCheckRollup is a real CI check
        # (not the review-comment text) with its own status/timestamps — use
        # that instead of guessing a fixed re-trigger timeout. Bugbot's own
        # response time varies a lot in practice (observed 5-28+ minutes
        # between a manual "@bugbot run" and its reply across past PRs), so
        # a flat 15-minute timer risks re-triggering while it's still
        # legitimately running (yellow/IN_PROGRESS), which just restarts the
        # wait and can loop. Only re-trigger if the check itself is not
        # currently in progress.
        bugbot_check = next(
            (c for c in details.get("statusCheckRollup", []) if c.get("name") == "Cursor Bugbot"),
            None,
        )
        bugbot_check_running = bool(bugbot_check) and (bugbot_check.get("status") or "").upper() in (
            "IN_PROGRESS", "QUEUED", "PENDING", "REQUESTED", "WAITING",
        )
        already_triggered = ticket.get("last_bugbot_trigger_sha") == head_sha
        if already_triggered and bugbot_check_running:
            log(f"{key}: auto-merge — Bugbot check still running for {head_sha[:8]}, waiting")
            return  # give it more time; re-checked next cycle, no re-trigger
        # Fallback for when there's no matching check at all (e.g. it hasn't
        # started yet, or the workflow name changes) — a time-based backstop
        # so this can't wait forever with nothing to poll.
        trigger_stale = True
        if already_triggered and not bugbot_check and ticket.get("last_bugbot_trigger_at"):
            try:
                triggered_at = datetime.strptime(ticket["last_bugbot_trigger_at"], "%Y-%m-%d %H:%M:%S")
                trigger_stale = (datetime.utcnow() - triggered_at).total_seconds() > 1800  # 30 min
            except ValueError:
                pass
        if already_triggered and not bugbot_check and not trigger_stale:
            log(f"{key}: auto-merge — already triggered Bugbot for {head_sha[:8]}, no check visible yet, waiting")
            return
        log(f"{key}: auto-merge — Bugbot hasn't reviewed the current commit yet, triggering it"
            + (" (re-trigger: not running per its check, or no response after 30min)" if already_triggered else ""))
        try:
            github.comment_on_pr(repo_path, pr_url, "@bugbot run")
            state.set_last_bugbot_trigger_sha(key, head_sha)
        except Exception as e:
            escalate_auto_merge(ticket, f"could not trigger Bugbot: {e}")
        return  # give it time; re-checked next cycle

    # --- gate checks --------------------------------------------------
    label_names = {l["name"] for l in details.get("labels", [])}
    if REQUIRED_LABEL not in label_names:
        escalate_auto_merge(
            ticket,
            f"PR missing the '{REQUIRED_LABEL}' label — the review skill may not have run. Not safe to auto-merge without it.",
        )
        return

    if not _custom_review_covers_head(ticket, details.get("headRefOid", "")):
        log(f"{key}: auto-merge — '{REQUIRED_LABEL}' label is stale (commits landed since the "
            f"last review pass, e.g. a conflict-resolution push) — re-running review")
        relaunch_for_stage(ticket, "auto_merge_recheck", pr_url, build_self_review_prompt,
                            notice=f"auto-merge — re-running {config.review_skill()}, PR changed since the last pass")
        return

    # --- approval ---------------------------------------------------------
    approved = any(r.get("state") == "APPROVED" for r in details.get("reviews", []))

    if approved:
        log(f"{key}: auto-merge — approved, merging")
        ok, out = github.merge_pr(repo_path, pr_url)
        if not ok:
            escalate_auto_merge(ticket, f"merge failed: {out}")
            return
        try:
            jira_client.transition_issue(key, "Done")
        except Exception as e:
            log(f"{key}: could not transition to Done after merge: {e}")
        try:
            jira_client.add_comment(key, f"[aidev] Merged: {pr_url}")
        except Exception as e:
            log(f"{key}: could not post merge comment: {e}")
        state.set_stage(key, "implement")
        notify(f"aidev: {key} auto-merged", "Merged to base branch", key=key, pr_url=pr_url)
        return

    if details.get("reviewRequests"):
        log(f"{key}: auto-merge — reviewer(s) assigned "
            f"({', '.join(r.get('login', '?') for r in details['reviewRequests'])}), "
            f"still trying /approve — an assigned reviewer alone doesn't block Cursor's own approval")

    latest_approval_verdict = _latest_review_matching(details, "Approval Agent")
    verdict_body = (latest_approval_verdict.get("body") or "") if latest_approval_verdict else ""

    last_request = _last_approve_request_time(repo_path, pr_url)
    verdict_is_current = (
        last_request
        and latest_approval_verdict
        and latest_approval_verdict.get("submittedAt", "") > last_request
    )
    bugbot_newer_than_verdict = (
        verdict_is_current
        and latest_bugbot
        and latest_bugbot.get("submittedAt", "") > latest_approval_verdict.get("submittedAt", "")
    )
    if verdict_is_current and not bugbot_newer_than_verdict:
        # We already asked since the last relevant change and got a non-approval
        # answer. Don't parse the wording ourselves (Cursor's phrasing varies
        # run to run, and a regex here already caused a real miss: "assign" was
        # meant to catch genuine human-deferral verdicts but also matched
        # "reviewers will be assigned", which is routine boilerplate, not a
        # deferral). Hand the verdict text to Claude and let it judge — same
        # pattern as the Bugbot-findings relaunch above.
        #
        # But a hard backstop first: caught live on PR #5759, the SAME
        # verdict (a structural "over the 1000-line size limit" gate) came
        # back 5 times over ~50 minutes across repeated /approve comments —
        # re-approving or re-judging text that hasn't changed was never
        # going to produce a different outcome, since a size gate isn't
        # something a re-read of the same words resolves. If the last few
        # verdicts are identical, stop cycling Claude/`/approve` and
        # actually escalate to a human instead.
        repeat_count = _repeated_approval_verdict_count(details)
        if repeat_count >= 2:
            escalate_auto_merge(
                ticket,
                f"Cursor's Approval Agent has given the identical verdict {repeat_count} times in a "
                f"row without change — re-approving isn't going to produce a different outcome. "
                f"Latest verdict: {verdict_body[:400]}",
                waiting_on_human=True,
            )
            return
        log(f"{key}: auto-merge — non-approval verdict, relaunching Claude to read and act on it")
        def approval_prompt_builder(key, summary, pr_url, _text=verdict_body, _repeat=repeat_count):
            return build_auto_merge_approval_prompt(key, summary, pr_url, _text, repeat_count=_repeat)
        relaunch_for_stage(ticket, "auto_merge_addressing_approval_feedback", pr_url, approval_prompt_builder,
                            notice="auto-merge — Cursor did not approve, reading its verdict and acting on it")
        return

    log(f"{key}: auto-merge — requesting Cursor approval")
    try:
        github.comment_on_pr(repo_path, pr_url, "/approve")
    except Exception as e:
        escalate_auto_merge(ticket, f"could not comment /approve: {e}")
    return  # give it time; re-checked next cycle


def continue_auto_merge(ticket, pr_url):
    """Called from finish_ticket when a relaunched auto-merge Claude session
    (conflict resolution, a Bugbot fix, or a stale-review re-check) finishes.
    Re-enters the same check from the top — the push it just did will be
    reflected in the next `gh pr view`.

    Must restore state to DONE first: finish_ticket already set it to
    POSTPROCESS (its very first line) by the time we're called, and nothing
    else on this path ever sets it back. Caught live: RND-14813 silently
    dropped out of every future `state.all_in_state("DONE")` auto-merge
    sweep after its conflict-resolution relaunch, since it was sitting in
    PR_OPENED, not DONE, and the sweep only iterates DONE tickets.

    Must ALSO restore the `aidev-done` Jira label: relaunch_for_stage (used
    by every auto_merge_* sub-stage, including this one's own relaunches)
    unconditionally calls set_state_label(key, "aidev-picked"), which is
    mutually exclusive with aidev-done and silently strips it. process_auto_
    merge_ticket's own entry gate requires aidev-done present (a deliberate
    safety check — never touch running work) — without restoring it here,
    the very first auto-merge relaunch permanently locks the ticket out of
    its own gate, and every future sweep logs "not In Review + aidev-done"
    and skips it forever. Caught live: RND-14818 and RND-14830 both stuck on
    exactly this silent loop after their first auto_merge relaunch."""
    key = ticket["ticket_key"]
    state.set_state(key, "DONE", pr_url=pr_url)
    try:
        jira_client.set_state_label(key, "aidev-done")
    except Exception as e:
        log(f"{key}: could not restore aidev-done label after auto-merge relaunch: {e}")
    ticket = state.get(key)
    state.set_stage(key, "auto_merge_recheck")
    process_auto_merge_ticket(ticket)


def cleanup_worktree(ticket):
    """Removes the git worktree and local branch for a ticket. Safe to call
    even if the worktree is already gone. Never touches the remote branch —
    that's GitHub's PR-merge cleanup, not ours."""
    key = ticket["ticket_key"]
    repo_path = ticket["repo_path"]
    worktree_path = ticket["worktree_path"]
    branch = ticket["branch"]
    tmux_name = ticket["tmux_session"]

    procs.tmux_kill(tmux_name)

    if os.path.isdir(worktree_path):
        result = procs.sh(f"git worktree remove {procs.shlex.quote(worktree_path)} --force", cwd=repo_path, check=False)
        log(f"{key}: removed worktree at {worktree_path}")
    else:
        log(f"{key}: worktree already gone at {worktree_path}")

    procs.sh(f"git branch -D {procs.shlex.quote(branch)}", cwd=repo_path, check=False)
    procs.sh("git worktree prune", cwd=repo_path, check=False)


TERMINAL_JIRA_STATUSES = ("done", "rejected", "cancelled", "closed")


def process_cleanup_candidate(ticket):
    """Checks whether a tracked ticket has reached a terminal Jira status
    (Done/Rejected/etc, set by a human — the pipeline never sets these) and,
    if so, removes its worktree/branch and archives it in the state DB so
    disk usage doesn't grow unbounded across weeks of tickets."""
    key = ticket["ticket_key"]
    try:
        issue = jira_client.get_issue(key, fields=["status"])
    except Exception as e:
        log(f"{key}: could not check status for cleanup: {e}")
        return

    status_name = issue["fields"]["status"]["name"]
    if status_name.lower() not in TERMINAL_JIRA_STATUSES:
        return

    log(f"{key}: Jira status is '{status_name}' — cleaning up worktree and archiving")
    cleanup_worktree(ticket)
    state.set_archived(key)


def main():
    try:
        with lockfile.Lock("monitor"):
            _run()
    except lockfile.LockHeld as e:
        log(f"skip run: {e}")


def _run():
    running = state.all_in_state("RUNNING")
    log(f"Checking {len(running)} running ticket(s)")
    for ticket in running:
        try:
            process_running_ticket(ticket)
        except Exception as e:
            log(f"ERROR processing {ticket['ticket_key']}: {e}")

    postprocess = state.all_in_state("POSTPROCESS")
    log(f"Checking {len(postprocess)} postprocess ticket(s) for PR retry")
    for ticket in postprocess:
        try:
            finish_ticket(ticket)
        except Exception as e:
            log(f"ERROR retrying postprocess {ticket['ticket_key']}: {e}")

    stuck = state.all_in_state("STUCK")
    log(f"Checking {len(stuck)} stuck ticket(s) for human replies")
    for ticket in stuck:
        try:
            process_stuck_ticket(ticket)
        except Exception as e:
            log(f"ERROR processing stuck {ticket['ticket_key']}: {e}")

    done = state.all_in_state("DONE")
    log(f"Checking {len(done)} done ticket(s) for review re-opens")
    for ticket in done:
        try:
            process_done_ticket(ticket)
        except Exception as e:
            log(f"ERROR processing done {ticket['ticket_key']}: {e}")

    auto_merge_candidates = state.all_in_state("DONE")
    log(f"Checking {len(auto_merge_candidates)} done ticket(s) for auto-merge")
    for ticket in auto_merge_candidates:
        try:
            process_auto_merge_ticket(ticket)
        except Exception as e:
            log(f"ERROR auto-merging {ticket['ticket_key']}: {e}")

    candidates = state.all_cleanup_candidates()
    log(f"Checking {len(candidates)} ticket(s) for worktree cleanup (terminal Jira status)")
    for ticket in candidates:
        try:
            process_cleanup_candidate(ticket)
        except Exception as e:
            log(f"ERROR cleaning up {ticket['ticket_key']}: {e}")


if __name__ == "__main__":
    main()
