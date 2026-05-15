"""Shared logging configuration for Blue Pearmain subcommands."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path.home() / "Library" / "Logs" / "BluePearmain"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5
_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def configure(log_name: str, verbose: bool = False) -> None:
    """
    Replace root logger handlers with StreamHandler + RotatingFileHandler.
    Safe to call multiple times; always reconfigures from scratch so each
    subcommand logs to its own file even when called from 'bp all'.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    rotator = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{log_name}.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    rotator.setFormatter(formatter)
    root.addHandler(rotator)
