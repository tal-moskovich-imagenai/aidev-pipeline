import os
import yaml

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")

_cache = None


def load():
    global _cache
    if _cache is None:
        with open(CONFIG_PATH, "r") as f:
            _cache = yaml.safe_load(f)
    return _cache


def repo_for(labels):
    cfg = load()
    for l in labels:
        if l.startswith("repo:"):
            key = l.split(":", 1)[1]
            if key in cfg["repos"]:
                return cfg["repos"][key]
    return cfg["repos"]["default"]
