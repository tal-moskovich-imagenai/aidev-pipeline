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


def _slack_notify(webhook_url, text, log=None):
    """Posts to the Slack incoming webhook. Returns True on success. Logs
    the real exception on failure (via the caller's own logger, when given)
    instead of swallowing it silently — a dropped Slack notification
    previously left no trace anywhere, indistinguishable from "notify()
    was never called." Caught live: RND-14661's own completion notify at
    17:35:51 landed in a window with repeated real DNS/SSL timeouts on this
    same machine (urlopen errors logged seconds earlier by an unrelated Jira
    call) — with no visibility into whether this specific webhook POST also
    failed, there was no way to tell a dropped notification from a bug
    upstream of notify() ever being called. Still never raises — a failed
    Slack post must not block or crash the pipeline."""
    try:
        data = json.dumps({"text": text}).encode()
        req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        if log:
            log(f"Slack notify failed: {e}")
        return False


def notify(title, message, key=None, pr_url=None, log=None):
    """Fire-and-forget notification across all configured backends.

    key/pr_url are appended as clickable links whenever available — a Slack
    message with only a bare ticket key in prose isn't a live link, and a
    human skimming Slack needs to jump straight to the ticket/PR without
    hunting through Jira or GitHub for it. Always pass key when the caller
    has one; pr_url whenever a PR already exists for this stage. Pass the
    caller's own `log` function so a failed Slack post actually gets
    recorded somewhere instead of vanishing silently."""
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
        _slack_notify(webhook, f"*{title}*\n{full_message}", log=log)
