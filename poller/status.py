"""
poller/status.py — collect operational status for bp status

All data-collection functions are pure I/O: they call launchctl,
stat log files, or query the DB. format_status() is pure text.
No side effects.
"""

from __future__ import annotations

import subprocess

# Launchd service labels for BP's four daemons
DAEMON_LABELS: list[tuple[str, str]] = [
    ("poller", "com.blue-pearmain.poller"),
    ("pipeline", "com.blue-pearmain.pipeline"),
    ("reviewer", "com.blue-pearmain.reviewer"),
    ("reconcile", "com.blue-pearmain.reconcile"),
]


def check_daemon(label: str) -> str:
    """
    Return 'loaded' if the launchd service is registered, 'not loaded' otherwise.
    Swallows errors (e.g. launchctl missing) and returns 'not loaded'.
    """
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
        )
        return "loaded" if result.returncode == 0 else "not loaded"
    except Exception:
        return "not loaded"
