# tests/test_cli_legacy.py
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
BP = ROOT / "bp"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(BP), *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


def test_index_legacy_registered():
    r = _run("index-legacy", "--help")
    assert r.returncode == 0
    assert "--library" in r.stdout
    assert "--no-thumbnails" in r.stdout
    assert "--refresh-cache" in r.stdout


def test_match_legacy_registered():
    r = _run("match-legacy", "--help")
    assert r.returncode == 0
    assert "--csv" in r.stdout
    assert "--apply" in r.stdout


def test_top_level_help_lists_commands():
    r = _run("--help")
    assert r.returncode == 0
    assert "index-legacy" in r.stdout
    assert "match-legacy" in r.stdout
