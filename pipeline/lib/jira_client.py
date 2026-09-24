"""Minimal Jira Cloud REST v3 client, stdlib-only (urllib), auth via macOS Keychain."""
import base64
import json
import re
import subprocess
import urllib.request
import urllib.parse
import urllib.error

from . import config

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_URL_RE = re.compile(r"(https?://[^\s<>\[\]()]+)")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")


def _get_token():
    cfg = config.load()["jira"]
    out = subprocess.run(
        ["security", "find-generic-password", "-a", cfg["email"], "-s", cfg["keychain_service"], "-w"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _auth_header():
    cfg = config.load()["jira"]
    token = _get_token()
    raw = f"{cfg['email']}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _request(method, path, params=None, body=None):
    cfg = config.load()["jira"]
    url = cfg["base_url"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header())
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Jira API {method} {path} -> {e.code}: {err_body}") from e


def search_issues(jql, fields=None, max_results=50):
    fields = fields or ["summary", "status", "labels", "description"]
    body = {"jql": jql, "maxResults": max_results, "fields": fields}
    result = _request("POST", "/rest/api/3/search/jql", body=body)
    return result.get("issues", [])


def get_issue(key, fields=None):
    fields = fields or ["summary", "status", "labels", "description", "comment"]
    return _request("GET", f"/rest/api/3/issue/{key}", params={"fields": ",".join(fields)})


def get_blocking_issues(key, review_status=None):
    """Returns [(blocker_key, blocker_status_name, hard), ...] for issues
    that block `key` and are not yet in a terminal status.

    `hard=True` means the blocker has not yet reached `review_status` (or no
    review_status was given) — the ticket must wait, full stop.
    `hard=False` means the blocker is sitting exactly at `review_status`
    (e.g. "In Review") — not merged yet, but far enough along that a caller
    may choose to proceed by stacking a new branch on top of the blocker's
    own branch instead of waiting for it to reach a terminal status.
    """
    issue = get_issue(key, fields=["issuelinks"])
    blockers = []
    for link in issue["fields"].get("issuelinks", []):
        if link.get("type", {}).get("name") != "Blocks":
            continue
        # Jira's issuelinks payload for issue X never includes X itself —
        # it includes only the OTHER side of the link, under whichever key
        # ("inwardIssue" or "outwardIssue") X is NOT. Verified directly
        # against JQL linkedIssues(..., "is blocked by"): when the link on X
        # carries an "inwardIssue", that inwardIssue is the one blocking X.
        # When it carries an "outwardIssue" instead, X is not blocked by it
        # (X may block that other issue, but that's not a blocker of X).
        blocker = link.get("inwardIssue")
        if not blocker:
            continue
        status_name = blocker["fields"]["status"]["name"]
        if status_name.lower() in ("done", "rejected", "cancelled", "closed"):
            continue  # terminal — not a blocker at all
        hard = True
        if review_status and status_name.lower() == review_status.lower():
            hard = False
        blockers.append((blocker["key"], status_name, hard))
    return blockers


def _inline_nodes(text):
    """Splits one line of text into ADF inline nodes, recognizing markdown
    links `[text](url)`, bare URLs (both become real ADF link marks so Jira
    renders a clickable link instead of literal text like "PR #5751"), and
    `inline code` spans (rendered with the ADF code mark). Everything else
    is plain text. Deliberately not a full markdown parser — just the bits
    the pipeline's own comments actually use."""
    nodes = []

    def emit_plain_with_code(segment):
        # Split `segment` on inline code spans, emitting code-marked nodes.
        pos = 0
        for m in _CODE_SPAN_RE.finditer(segment):
            if m.start() > pos:
                nodes.append({"type": "text", "text": segment[pos:m.start()]})
            nodes.append({"type": "text", "text": m.group(1), "marks": [{"type": "code"}]})
            pos = m.end()
        if pos < len(segment):
            nodes.append({"type": "text", "text": segment[pos:]})

    # First split on markdown links, then bare URLs within the remainder.
    pos = 0
    for m in _MD_LINK_RE.finditer(text):
        if m.start() > pos:
            _split_bare_urls(text[pos:m.start()], nodes, emit_plain_with_code)
        nodes.append({"type": "text", "text": m.group(1), "marks": [{"type": "link", "attrs": {"href": m.group(2)}}]})
        pos = m.end()
    if pos < len(text):
        _split_bare_urls(text[pos:], nodes, emit_plain_with_code)

    return nodes or [{"type": "text", "text": ""}]


def _split_bare_urls(segment, nodes, emit_plain_with_code):
    pos = 0
    for m in _BARE_URL_RE.finditer(segment):
        if m.start() > pos:
            emit_plain_with_code(segment[pos:m.start()])
        url = m.group(1)
        nodes.append({"type": "text", "text": url, "marks": [{"type": "link", "attrs": {"href": url}}]})
        pos = m.end()
    if pos < len(segment):
        emit_plain_with_code(segment[pos:])


def _text_to_adf_content(text):
    """Converts plain/lightly-markdown text into ADF document content:
    ```lang fenced blocks become codeBlock nodes, everything else becomes
    paragraphs with linkified URLs and `code` spans via _inline_nodes."""
    content = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("```"):
            fence_lang = lines[i].strip()[3:].strip()
            body_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            code_attrs = {"language": fence_lang} if fence_lang else {}
            content.append({
                "type": "codeBlock",
                "attrs": code_attrs,
                "content": [{"type": "text", "text": "\n".join(body_lines)}] if body_lines else [],
            })
            continue
        line = lines[i]
        if line.strip() == "":
            content.append({"type": "paragraph"})
        else:
            content.append({"type": "paragraph", "content": _inline_nodes(line)})
        i += 1
    return content


def add_comment(key, text):
    body = {"body": {"type": "doc", "version": 1, "content": _text_to_adf_content(text)}}
    return _request("POST", f"/rest/api/3/issue/{key}/comment", body=body)


def get_attachments(key):
    """Returns the issue's current attachments (id, filename, content URL)."""
    issue = get_issue(key, fields=["attachment"])
    return issue["fields"].get("attachment", [])


def attach_file(key, file_path):
    """Uploads a local file (e.g. a screenshot) as an attachment on `key`.
    Multipart, not the JSON-body _request path — Jira's attachments endpoint
    also requires the X-Atlassian-Token: no-check header (CSRF check that
    otherwise rejects the upload). Returns the created attachment dict.

    The content download URL Jira returns 303-redirects to the actual file —
    follow it (curl -L, or requests' default) when fetching it back."""
    import mimetypes
    import os
    import uuid

    boundary = uuid.uuid4().hex
    filename = os.path.basename(file_path)
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()

    cfg = config.load()["jira"]
    url = cfg["base_url"].rstrip("/") + f"/rest/api/3/issue/{key}/attachments"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", _auth_header())
    req.add_header("X-Atlassian-Token", "no-check")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Jira attach_file {key} -> {e.code}: {err_body}") from e


def get_transitions(key):
    return _request("GET", f"/rest/api/3/issue/{key}/transitions").get("transitions", [])


def transition_issue(key, target_status_name):
    transitions = get_transitions(key)
    match = next((t for t in transitions if t["to"]["name"].lower() == target_status_name.lower()), None)
    if not match:
        available = [t["to"]["name"] for t in transitions]
        raise RuntimeError(f"No transition to '{target_status_name}' for {key}. Available: {available}")
    return _request("POST", f"/rest/api/3/issue/{key}/transitions", body={"transition": {"id": match["id"]}})


def assign_issue(key, account_id):
    """Assigns `key` to the Jira account with this accountId. Unused by the
    pipeline itself (tickets must already be assigned before pickup — see
    the aidev-jira-tickets skill), kept for manual/future use."""
    return _request("PUT", f"/rest/api/3/issue/{key}/assignee", body={"accountId": account_id})


def get_active_sprint_id(board_id):
    """Returns the id of the single active sprint on `board_id`, or None if
    there isn't exactly one (no active sprint, or board isn't sprint-based)."""
    result = _request("GET", f"/rest/agile/1.0/board/{board_id}/sprint", params={"state": "active"})
    sprints = result.get("values", [])
    if len(sprints) != 1:
        return None
    return sprints[0]["id"]


def add_issue_to_sprint(sprint_id, key):
    """Moves `key` into `sprint_id` (Jira replaces any current sprint —
    fine here since pipeline-picked tickets aren't in a sprint yet)."""
    return _request("POST", f"/rest/agile/1.0/sprint/{sprint_id}/issue", body={"issues": [key]})


STATE_LABELS = ("aidev-picked", "aidev-stuck", "aidev-done")


def set_state_label(key, label):
    """Adds `label` and removes any other aidev-* state label (mutually exclusive)."""
    if label not in STATE_LABELS:
        raise ValueError(f"Unknown state label: {label}")
    issue = get_issue(key, fields=["labels"])
    current = set(issue["fields"].get("labels", []))
    current -= set(STATE_LABELS)
    current.add(label)
    _request("PUT", f"/rest/api/3/issue/{key}", body={"fields": {"labels": sorted(current)}})


def remove_label(key, label):
    """Removes exactly one label, leaving every other label untouched. Uses
    Jira's `update.labels[].remove` op — NOT a read-modify-write of the
    whole label set — so a concurrent label change elsewhere can't get
    clobbered by this call (see the RND-14792 incident: setting the whole
    list wholesale once wiped a pipeline-managed label that had been added
    between the read and the write)."""
    _request("PUT", f"/rest/api/3/issue/{key}", body={"update": {"labels": [{"remove": label}]}})


def get_comments(key):
    data = _request("GET", f"/rest/api/3/issue/{key}/comment")
    return data.get("comments", [])


def plain_description(issue):
    """Best-effort flatten of Atlassian Document Format description to plain text."""
    desc = issue.get("fields", {}).get("description")
    if not desc:
        return ""
    if isinstance(desc, str):
        return desc

    def walk(node):
        out = []
        if node.get("type") == "text":
            out.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            out.extend(walk(child))
        if node.get("type") in ("paragraph", "heading"):
            out.append("\n")
        return out

    return "".join(walk(desc)).strip()


def format_comments_for_prompt(key=None, comments=None):
    """Fetch and flatten all comments on a ticket into plain text, oldest
    first, each tagged with its author and timestamp. Comments are where a
    human most often refines or overrides what the description says after
    the ticket was written or picked up — pickup.py's search_issues() call
    does not fetch the comment field by default, so callers that need the
    full picture (build_task_prompt) must fetch this separately, not assume
    the description alone is authoritative. Skips the pipeline's own
    [aidev]/✅ status comments (see set_state_label callers) — those are
    noise here, not human instructions.

    Pass `comments` (a comment list already fetched via get_issue, e.g. by
    build_jira_context_block) to format it without a second API round trip.
    Pass `key` alone only when no such list already exists — this makes its
    own get_comments(key) call in that case."""
    if comments is None:
        if key is None:
            raise ValueError("format_comments_for_prompt needs key or comments")
        comments = get_comments(key)
    if not comments:
        return ""
    blocks = []
    for c in comments:
        body = plain_description({"fields": {"description": c.get("body")}})
        if not body.strip():
            continue
        if body.strip().startswith("[aidev]") or body.strip().startswith("🤖") or body.strip().startswith("✅"):
            continue
        author = c.get("author", {}).get("displayName", "unknown")
        created = c.get("created", "")
        blocks.append(f"[{created} — {author}]\n{body.strip()}")
    return "\n\n".join(blocks)


def build_jira_context_block(key, base_url):
    """The ONE function every pipeline entry point that references a Jira
    ticket must call — pickup (first pickup), rework (review reopen),
    anywhere else a fresh session starts from a ticket reference. Single
    source of truth for what "the ticket's context" means, so there is
    exactly one place that decides which fields matter and how they're
    formatted — not pickup.py fetching description one way and monitor.py
    fetching comments another way, drifting out of sync over time.

    Does ONE get_issue call for description/comments/attachments/issuelinks
    (Jira lets multiple fields ride the same request — no reason to make
    four round trips for one ticket), then appends the ticket's blockers
    (who blocks it, and what it blocks) via a second call, since that's a
    separate endpoint concern (issuelinks alone doesn't resolve each
    blocker's live status without a second lookup already done by
    get_blocking_issues).

    Returns (link, context_text) — link is the bare ticket URL (always put
    it in the prompt yourself, this function doesn't repeat it inline),
    context_text is the full fallback context block: description, comments
    (oldest first, bot noise filtered), attachment names/URLs (images
    especially — the plain-text flatten cannot represent them, so list them
    explicitly and tell Claude to fetch them), and the blocking-issue
    relationship. This whole block is explicitly a FALLBACK — every caller
    must still tell Claude to live-fetch the ticket itself first (see
    jira_live_fetch_note in lib/soul.py) and use this only if that fails."""
    issue = get_issue(key, fields=["summary", "description", "comment", "attachment", "issuelinks"])
    link = f"{base_url}/browse/{key}"

    desc = plain_description(issue)
    comments = format_comments_for_prompt(comments=issue["fields"].get("comment", {}).get("comments", []))

    attachments = issue["fields"].get("attachment", [])
    attachments_block = ""
    if attachments:
        lines = [f"- {a.get('filename', 'unnamed')}: {a.get('content', '')}" for a in attachments]
        attachments_block = (
            "\nAttachments on this ticket (fetch and actually look at each one — "
            "screenshots and recordings are often the real bug report, not the prose):\n"
            + "\n".join(lines)
        )

    blockers = get_blocking_issues(key)
    blockers_block = ""
    if blockers:
        lines = [f"- blocked by {k} (status: {s}, {'must wait' if hard else 'in review, may stack'})"
                  for k, s, hard in blockers]
        blockers_block = "\nDependency links (who this ticket is blocked by):\n" + "\n".join(lines)

    parts = [f"Description:\n{desc or '(no description provided)'}"]
    if comments:
        parts.append(f"Comments (oldest first — a later comment overrides the description on conflict):\n{comments}")
    if attachments_block:
        parts.append(attachments_block.strip())
    if blockers_block:
        parts.append(blockers_block.strip())

    return link, "\n\n".join(parts)
