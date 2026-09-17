"""Minimal Jira Cloud REST v3 client, stdlib-only (urllib), auth via macOS Keychain."""
import base64
import json
import subprocess
import urllib.request
import urllib.parse
import urllib.error

from . import config


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


def get_blocking_issues(key):
    """Returns [(blocker_key, blocker_status_name), ...] for issues that block `key`
    and are not yet Done/Rejected (i.e. still actively blocking)."""
    issue = get_issue(key, fields=["issuelinks"])
    blockers = []
    for link in issue["fields"].get("issuelinks", []):
        link_type = link.get("type", {})
        # When the fetched issue is on the "inward" side of a Blocks link
        # (i.e. "is blocked by"), Jira puts the blocking issue under outwardIssue.
        if link_type.get("inward") != "is blocked by":
            continue
        blocker = link.get("outwardIssue")
        if not blocker:
            continue
        status_name = blocker["fields"]["status"]["name"]
        if status_name.lower() not in ("done", "rejected", "cancelled", "closed"):
            blockers.append((blocker["key"], status_name))
    return blockers


def add_comment(key, text):
    body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": line}]}
                for line in text.split("\n")
            ],
        }
    }
    return _request("POST", f"/rest/api/3/issue/{key}/comment", body=body)


def get_transitions(key):
    return _request("GET", f"/rest/api/3/issue/{key}/transitions").get("transitions", [])


def transition_issue(key, target_status_name):
    transitions = get_transitions(key)
    match = next((t for t in transitions if t["to"]["name"].lower() == target_status_name.lower()), None)
    if not match:
        available = [t["to"]["name"] for t in transitions]
        raise RuntimeError(f"No transition to '{target_status_name}' for {key}. Available: {available}")
    return _request("POST", f"/rest/api/3/issue/{key}/transitions", body={"transition": {"id": match["id"]}})


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
