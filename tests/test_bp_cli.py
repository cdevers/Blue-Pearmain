"""Smoke tests for bp CLI dispatch (#220).

Validates that every subcommand is registered with argparse (--help exits 0)
and wired in the dispatch dict (--dry-run does not raise KeyError).

These tests use subprocess so import-time errors in the bp script are also caught.
Update ALL_SUBCOMMANDS and DRY_RUN_SUBCOMMANDS when adding a new subcommand.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
BP = str(ROOT / "bp")

# Every subcommand visible in `bp --help`. Keep sorted for easy diffing.
ALL_SUBCOMMANDS = [
    "all",
    "checkpoint",
    "deduplicator",
    "doctor",
    "export",
    "geocode",
    "import-contacts-birthdays",
    "index-legacy",
    "install-daemons",
    "link-orphans",
    "match-legacy",
    "migrate",
    "pipeline",
    "poll",
    "prune-proposals",
    "reconcile",
    "scan",
    "stats",
    "status",
    "sync-album-collections",
    "sync-albums",
    "sync-metadata",
    "sync-names-from-flickr",
    "tag-writeback",
    "thumbs",
    "ui",
    "uninstall-daemons",
]

# Subcommands that accept --dry-run (verified via `bp <cmd> --help | grep dry-run`).
DRY_RUN_SUBCOMMANDS = [
    "all",
    "deduplicator",
    "geocode",
    "import-contacts-birthdays",
    "install-daemons",
    "link-orphans",
    "migrate",
    "pipeline",
    "poll",
    "prune-proposals",
    "scan",
    "sync-album-collections",
    "sync-albums",
    "sync-metadata",
    "sync-names-from-flickr",
    "tag-writeback",
    "uninstall-daemons",
]


@pytest.mark.parametrize("subcommand", ALL_SUBCOMMANDS)
def test_subcommand_help(subcommand: str) -> None:
    """Every subcommand must accept --help and exit 0 (tests subparser registration)."""
    result = subprocess.run(
        [sys.executable, BP, subcommand, "--help"],
        capture_output=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, (
        f"`bp {subcommand} --help` exited {result.returncode}\nstderr: {result.stderr.decode()}"
    )


@pytest.mark.parametrize("subcommand", DRY_RUN_SUBCOMMANDS)
def test_subcommand_dry_run_accepted(subcommand: str) -> None:
    """Subcommands with --dry-run must not raise a dispatch error when invoked.

    Passes a guaranteed-nonexistent config path so the command exits quickly
    (config not found → exit 1) without touching the DB or network. This keeps
    the test fast while still exercising the argparse + dispatch layers.

    What must NOT happen:
      - exit 2: argparse rejected --dry-run as an unrecognized argument
      - 'KeyError' in stderr: the subcommand is missing from the dispatch dict
    """
    result = subprocess.run(
        [sys.executable, BP, "--config", "/dev/null/nonexistent.yml", subcommand, "--dry-run"],
        capture_output=True,
        cwd=ROOT,
    )
    stderr = result.stderr.decode()
    # Config/runtime errors are acceptable (fast exit, no real work done).
    # The three failure modes we're guarding against:
    assert "KeyError" not in stderr, (
        f"`bp {subcommand} --dry-run` raised KeyError — likely missing from dispatch dict\n"
        f"stderr: {stderr}"
    )
    assert "unrecognized arguments: --dry-run" not in stderr, (
        f"`bp {subcommand} --dry-run` reported --dry-run as unrecognized\nstderr: {stderr}"
    )
    assert f"invalid choice: '{subcommand}'" not in stderr, (
        f"`bp {subcommand} --dry-run` reported subcommand as invalid choice\nstderr: {stderr}"
    )
