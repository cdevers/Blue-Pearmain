"""
utils/notifier.py — macOS desktop notifications for Blue Pearmain daemons

Sends a macOS notification via osascript. Fire-and-forget: errors from
osascript (not installed, permission denied, timeout) are swallowed so
notification failures never affect the calling daemon.

Controlled by config:
    notifications:
      enabled: true   # set false to disable all notifications

If the notifications key is absent or config is None, notifications are on.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("blue-pearmain.notifier")

_DEFAULT_TITLE = "Blue Pearmain"


def _escape(s: str) -> str:
    """Escape double quotes for AppleScript string literals."""
    return s.replace('"', '\\"')


def notify(
    message: str,
    title: str = _DEFAULT_TITLE,
    config: dict | None = None,
) -> None:
    """Send a macOS desktop notification.

    Does nothing (silently) if:
    - notifications are disabled in config
    - osascript is unavailable or raises any error
    """
    if config is not None:
        enabled = config.get("notifications", {}).get("enabled", True)
        if not enabled:
            return

    cmd = [
        "osascript",
        "-e",
        f'display notification "{_escape(message)}" with title "{_escape(title)}"',
    ]
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=5)
    except Exception:
        # Never let notification failures surface to the caller.
        log.debug("Notification could not be sent (osascript unavailable?)")
