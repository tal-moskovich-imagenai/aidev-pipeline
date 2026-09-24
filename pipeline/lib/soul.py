"""Loads SOUL.md — the identity/judgment doc injected into every Claude
Code session prompt the pipeline builds (initial pickup and rework)."""
import os

SOUL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "SOUL.md")


def load_soul():
    try:
        with open(SOUL_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def soul_section():
    soul = load_soul()
    return f"\n---\n{soul}\n---\n\n" if soul else ""


def jira_live_fetch_note(key, link):
    """Shared instruction, injected into every prompt that references a
    Jira ticket: tells Claude to fetch the ticket's full live context
    itself — description, comments, attachments/images, blocking links,
    the wider linked-issue chain, and epic/parent context. Explicitly
    names `imagen-core:jira` as the preferred tool (globally installed,
    AWS-Secrets-Manager-credentialed via the SessionStart hook already
    configured on this machine — see internal-claude/setup.sh) since it
    fetches richer detail than a raw API call (visually analyzes image
    attachments, not just lists their URLs).

    Deliberately trusts Claude to do this itself rather than the pipeline
    mechanically pre-fetching and embedding a flattened snapshot in the
    prompt — an earlier version of this pipeline did both (a deterministic
    embed as a safety net, plus this instruction on top), but that
    duplicated the same fetch in two places for no real gain once the
    instruction here is explicit and complete: it names the exact tool,
    lists every category to fetch, and says why each matters. If a
    session's Jira access genuinely isn't working, that's a real failure
    worth surfacing plainly in the final summary, not something to paper
    over with a stale fallback snapshot the pipeline built minutes or
    hours before this session even started, so say so explicitly if that
    happens (e.g. "could not fetch Jira context this session") rather than
    quietly proceeding on whatever's already in this prompt's summary/title.

    `link` is the same ticket URL already printed elsewhere in the prompt
    (the caller builds it as {base_url}/browse/{key}) — passed in rather
    than re-derived here, so there is exactly one place per prompt that
    constructs it, not two copies that could drift."""
    return f"""## Before implementing — re-fetch the live ticket yourself

This prompt does NOT embed a pre-fetched snapshot of the ticket's
description/comments — that mechanical fetch was removed as redundant now
that you're expected to do the live fetch yourself. Before starting real
work, fetch the FULL live picture of {key} yourself:

- **Preferred tool**: the `imagen-core:jira` skill (invoke via
  `Skill(skill="imagen-core:jira")` — it's globally installed and
  already credentialed in this environment via AWS Secrets Manager). It
  fetches richer detail than a raw API call, including downloading and
  visually analyzing image attachments, not just listing their URLs. Fall
  back to `gh`/a direct Jira API call only if that skill genuinely isn't
  available in this session.
- Get ALL of the following, not just the description:
  - **Description** — the current live text, which may have been edited
    since this prompt was built.
  - **Every comment**, oldest to newest — a later comment overrides the
    description on conflict (a human correcting/narrowing scope after the
    fact is common and easy to miss if you only read the description).
  - **Images and attachments** — screenshots, recordings, anything
    attached to the ticket or pasted into a comment. These are often the
    actual bug report; the prose around them can be incomplete without
    them. Look at each one directly, don't just note that it exists.
  - **Blocking/blocked-by links** — is this ticket genuinely unblocked
    right now, and does it block anything that assumes it isn't done yet?
  - **The linked-issue chain** — any ticket this one references or is
    stacked on, not just direct blockers (a design decision recorded on a
    sibling ticket can matter here even without a formal Jira link type).
  - **Epic/parent context** — if this ticket has a parent epic, read it
    too; the epic often carries the actual goal and constraints an
    individual ticket assumes without repeating.

Read {link} for the ticket itself; follow whatever links/attachments/epic
reference you find from there. Do this before forming any plan — a
description alone, without its comments and attachments, is often
incomplete or stale.
"""
