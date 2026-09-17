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
