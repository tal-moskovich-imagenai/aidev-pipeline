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
    Jira ticket: tells Claude to independently re-fetch the ticket's full
    live context — description, comments, attachments/images, blocking
    links, the wider linked-issue chain, and epic/parent context — rather
    than trusting only the flattened text embedded below. Explicitly names
    `imagen-core:jira` as the preferred tool (globally installed,
    AWS-Secrets-Manager-credentialed via the SessionStart hook already
    configured on this machine — see internal-claude/setup.sh) since it
    fetches richer detail than a raw API call (visually analyzes image
    attachments, not just lists their URLs).

    Why both exist: the embedded text is a deterministic, code-level
    guarantee that at least a plain-text snapshot of the description and
    comments is present in the prompt no matter what — it does not depend
    on Claude remembering a step or a Jira tool actually working in this
    session. But it is a lossy flatten (no attachments, no images pasted
    into comments, no linked-issue/epic context, and it can go stale if a
    comment is edited after this prompt was built). Live-fetching adds
    that richer, current context on top — it does not replace the
    guarantee, because a prompt instruction can be skipped under pressure
    or fail silently if Jira access isn't actually working in this
    session, and unlike the embedded text that failure would be invisible.

    If the live fetch succeeds, treat it as authoritative over the
    embedded text below when they disagree (attachments/images especially
    — the flattening below cannot represent them at all). If the live
    fetch fails or isn't available in this session for any reason, that's
    fine — use the embedded text below, but say so explicitly in the PR
    description or your final summary (e.g. "worked from the pipeline's
    embedded Jira snapshot; could not independently re-fetch the live
    ticket in this session") so a human reviewing later knows which source
    you actually used, not just that Jira context existed somewhere.

    `link` is the same ticket URL already printed elsewhere in the prompt
    (build_jira_context_block returns it) — passed in rather than
    re-derived here, so there is exactly one place per prompt that
    constructs {base_url}/browse/{key}, not two copies that could drift."""
    return f"""## Before implementing — re-fetch the live ticket yourself

The fallback context below is a plain-text snapshot the orchestrator took
when building this prompt — a fallback, not the primary source. Before
starting real work, independently fetch the FULL live picture of {key}
yourself:

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

This snapshot can already be stale (a comment posted after this prompt was
built, an image attached to a comment, a linked ticket, an edited
description) — the live fetch is the source of truth if it disagrees with
what's embedded below. Read {link} for the ticket itself; follow whatever
links/attachments/epic reference you find from there.
"""
