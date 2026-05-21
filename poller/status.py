"""
poller/status.py — collect operational status for bp status

All data-collection functions are pure I/O: they call launchctl,
stat log files, or query the DB. format_status() is pure text.
No side effects.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db.db import Database

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


def log_mtime_ago(log_path: Path) -> str:
    """
    Return a human-readable string for how long ago log_path was last written.
    Returns 'never' if the file does not exist.
    """
    if not log_path.exists():
        return "never"
    age_s = time.time() - log_path.stat().st_mtime
    if age_s < 120:
        return "just now"
    if age_s < 3600:
        return f"{int(age_s // 60)}m ago"
    if age_s < 86400:
        return f"{int(age_s // 3600)}h ago"
    return f"{int(age_s // 86400)}d ago"


def collect_status(config: dict, db: "Database") -> dict:
    """
    Collect operational status from launchctl and the DB.

    Returns:
        {
          "daemons": [
              {"name": "poller", "label": "com.blue-pearmain.poller",
               "state": "loaded"|"not loaded", "last_run": "2h ago"},
              ...
          ],
          "queue": {
              "needs_review": N, "candidate_public": N,
              "approved_public": N, "pushable": N,
          },
          "proposals": {"total": N, "collision": N, "non_conflict": N, "divergence": N},
        }
    """
    log_dir = Path.home() / "Library" / "Logs" / "BluePearmain"

    # Log file names by short daemon name (reviewer has no log)
    log_files: dict[str, str] = {
        "poller": "poller.log",
        "pipeline": "pipeline.log",
        "reconcile": "reconcile.log",
    }

    daemons = []
    for name, label in DAEMON_LABELS:
        state = check_daemon(label)
        log_name = log_files.get(name)
        if log_name:
            last_run = log_mtime_ago(log_dir / log_name)
        else:
            # Reviewer: running means it's serving; no log heuristic needed
            last_run = "serving" if state == "loaded" else "—"
        daemons.append({"name": name, "label": label, "state": state, "last_run": last_run})

    # Queue stats from DB
    stats = db.stats()
    by_state = stats.get("by_state", {})
    approved = by_state.get("approved_public", 0)
    try:
        pushable = db.conn.execute(
            "SELECT COUNT(*) AS n FROM photos"
            " WHERE privacy_state='approved_public'"
            "   AND flickr_id IS NOT NULL"
            "   AND (perms_pushed_flickr IS NULL OR perms_pushed_flickr = 0)"
        ).fetchone()["n"]
    except Exception:
        pushable = 0

    queue = {
        "needs_review": by_state.get("needs_review", 0),
        "candidate_public": by_state.get("candidate_public", 0),
        "approved_public": approved,
        "pushable": pushable,
    }

    proposals = db.get_proposal_counts()

    return {"daemons": daemons, "queue": queue, "proposals": proposals}
