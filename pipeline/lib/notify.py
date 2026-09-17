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


def notify(title, message):
    """Fire-and-forget notification across all configured backends."""
    _macos_notify(title, message)
    cfg = config.load()
    webhook = (cfg.get("notifications") or {}).get("slack_webhook_url")
    if webhook:
        _slack_notify(webhook, f"*{title}*\n{message}")
