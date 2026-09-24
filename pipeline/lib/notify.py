"""Notifications for stuck/done/failed tickets — best-effort, never raises.

Backends:
- macOS native notification (osascript) — always attempted, zero config.
- Slack incoming webhook — only if notifications.slack_webhook_url is set
  in config.yaml.
"""
import subprocess
import urllib.request
import json

from . import config


def _macos_notify(title, message):
    try:
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Glass"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass  # best-effort only


def _slack_notify(webhook_url, text):
    try:
        data = json.dumps({"text": text}).encode()
        req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # best-effort only


def notify(title, message, key=None, pr_url=None):
    """Fire-and-forget notification across all configured backends.

    key/pr_url are appended as clickable links whenever available — a Slack
    message with only a bare ticket key in prose isn't a live link, and a
    human skimming Slack needs to jump straight to the ticket/PR without
    hunting through Jira or GitHub for it. Always pass key when the caller
    has one; pr_url whenever a PR already exists for this stage."""
    cfg = config.load()
    lines = [message]
    if key:
        base_url = (cfg.get("jira") or {}).get("base_url", "")
        if base_url:
            lines.append(f"Ticket: {base_url}/browse/{key}")
    if pr_url:
        lines.append(f"PR: {pr_url}")
    full_message = "\n".join(lines)
    _macos_notify(title, full_message)
    webhook = (cfg.get("notifications") or {}).get("slack_webhook_url")
    if webhook:
        _slack_notify(webhook, f"*{title}*\n{full_message}")
