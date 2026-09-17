"""Shared logging: prints to stdout AND appends to a rotating-by-date file
under config.yaml's log_dir, so cron runs (which usually discard stdout)
still leave a trail to debug from.
"""
import os
import datetime

from . import config


def get_logger(script_name):
    cfg = config.load()
    log_dir = cfg.get("log_dir")
    log_file = None
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        date_str = datetime.date.today().isoformat()
        log_file = os.path.join(log_dir, f"{script_name}-{date_str}.log")

    def log(msg):
        line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
        print(line)
        if log_file:
            try:
                with open(log_file, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # never let logging itself crash the pipeline

    return log
