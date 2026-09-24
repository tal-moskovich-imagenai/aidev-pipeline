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


def jira_live_fetch_note(key, base_url):
    """Shared instruction, injected into every prompt that references a
    Jira ticket: tells Claude to independently re-fetch the ticket's live
    description/comments/attachments/linked-issues via its own Jira access
    (mcp__jira, gh, or whatever tool is actually available in this session)
    rather than trusting only the flattened text embedded below.

    Why both exist: the embedded text is a deterministic, code-level
    guarantee that at least a plain-text snapshot of the description and
    comments is present in the prompt no matter what — it does not depend
    on Claude remembering a step or a Jira tool actually working in this
    session. But it is a lossy flatten (no attachments, no images pasted
    into comments, no linked-issue context, and it can go stale if a
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
    you actually used, not just that Jira context existed somewhere."""
    return f"""## Before implementing — re-fetch the live ticket yourself

The description and comments embedded below are a plain-text snapshot the
orchestrator took when building this prompt — a fallback, not the primary
source. Use whatever Jira access you actually have in this session (the
Jira MCP/skill, `gh`, or a direct API call) to independently fetch {key}'s
live description, all comments, attachments, and any linked issues:
{base_url}/browse/{key}

This snapshot can already be stale (a comment posted after this prompt was
built, an image attached to a comment, a linked ticket) — the live fetch is
the source of truth if it disagrees with what's embedded below.
"""
