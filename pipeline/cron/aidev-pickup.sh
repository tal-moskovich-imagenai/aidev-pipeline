#!/bin/bash
# Hermes cron wrapper — see README.md "Scheduling". Registered filename-only
# under ~/.hermes/scripts/ (copy this there, or symlink it).
cd /Users/talmoskovich/jira-claude-pipeline || exit 1
/usr/bin/python3 pickup.py
