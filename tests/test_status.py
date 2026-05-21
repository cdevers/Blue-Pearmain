"""
tests/test_status.py — unit tests for poller.status

Run from repo root:
    python -m pytest tests/test_status.py -v
"""

import subprocess as proc
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from poller.status import (
    check_daemon,
    log_mtime_ago,
    collect_status,
    format_status,
    status_exit_code,
)
from db.db import Database


class TestCheckDaemon(unittest.TestCase):
    @patch("subprocess.run")
    def test_returns_loaded_when_launchctl_exits_zero(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertEqual(check_daemon("com.blue-pearmain.poller"), "loaded")

    @patch("subprocess.run")
    def test_returns_not_loaded_when_launchctl_exits_nonzero(self, mock_run):
        mock_run.return_value = MagicMock(returncode=113)
        self.assertEqual(check_daemon("com.blue-pearmain.poller"), "not loaded")

    @patch("subprocess.run")
    def test_passes_label_to_launchctl(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        check_daemon("com.blue-pearmain.pipeline")
        cmd = mock_run.call_args[0][0]
        self.assertIn("com.blue-pearmain.pipeline", cmd)
        self.assertEqual(cmd[0], "launchctl")

    @patch("subprocess.run", side_effect=FileNotFoundError("launchctl not found"))
    def test_returns_not_loaded_when_launchctl_missing(self, _):
        # On non-macOS or when launchctl absent, treat as not loaded
        self.assertEqual(check_daemon("com.blue-pearmain.poller"), "not loaded")


class TestLogMtimeAgo(unittest.TestCase):
    def test_returns_never_when_file_does_not_exist(self):
        p = Path("/nonexistent/no.log")
        self.assertEqual(log_mtime_ago(p), "never")

    @patch("poller.status.time")
    def test_returns_just_now_when_under_two_minutes(self, mock_time):
        mock_time.time.return_value = 1000.0
        p = MagicMock(spec=Path)
        p.exists.return_value = True
        p.stat.return_value = MagicMock(st_mtime=999.0)  # 1 second ago
        self.assertEqual(log_mtime_ago(p), "just now")

    @patch("poller.status.time")
    def test_returns_minutes_when_under_one_hour(self, mock_time):
        mock_time.time.return_value = 1000.0
        p = MagicMock(spec=Path)
        p.exists.return_value = True
        p.stat.return_value = MagicMock(st_mtime=1000.0 - 150)  # 2.5 min ago
        self.assertEqual(log_mtime_ago(p), "2m ago")

    @patch("poller.status.time")
    def test_returns_hours_when_under_one_day(self, mock_time):
        mock_time.time.return_value = 1000.0
        p = MagicMock(spec=Path)
        p.exists.return_value = True
        p.stat.return_value = MagicMock(st_mtime=1000.0 - 7500)  # 2.08h ago
        self.assertEqual(log_mtime_ago(p), "2h ago")

    @patch("poller.status.time")
    def test_returns_days_when_over_one_day(self, mock_time):
        mock_time.time.return_value = 1000.0
        p = MagicMock(spec=Path)
        p.exists.return_value = True
        p.stat.return_value = MagicMock(st_mtime=1000.0 - 90000)  # ~1 day ago
        self.assertEqual(log_mtime_ago(p), "1d ago")


def _make_db() -> Database:
    """Create a minimal temp DB for testing."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Database(Path(f.name))


class TestCollectStatus(unittest.TestCase):
    @patch("poller.status.check_daemon", return_value="loaded")
    @patch("poller.status.log_mtime_ago", return_value="5m ago")
    def test_returns_dict_with_required_keys(self, _mtime, _daemon):
        db = _make_db()
        config = {"logging": {"file": "/tmp/bp.log"}}
        result = collect_status(config, db)
        db.close()
        self.assertIn("daemons", result)
        self.assertIn("queue", result)
        self.assertIn("proposals", result)

    @patch("poller.status.check_daemon", return_value="loaded")
    @patch("poller.status.log_mtime_ago", return_value="1h ago")
    def test_daemons_list_has_four_entries(self, _mtime, _daemon):
        db = _make_db()
        config = {"logging": {"file": "/tmp/bp.log"}}
        result = collect_status(config, db)
        db.close()
        self.assertEqual(len(result["daemons"]), 4)

    @patch("poller.status.check_daemon", return_value="not loaded")
    @patch("poller.status.log_mtime_ago", return_value="never")
    def test_daemon_state_propagated(self, _mtime, _daemon):
        db = _make_db()
        config = {"logging": {"file": "/tmp/bp.log"}}
        result = collect_status(config, db)
        db.close()
        states = {d["name"]: d["state"] for d in result["daemons"]}
        self.assertEqual(states["poller"], "not loaded")

    @patch("poller.status.check_daemon", return_value="loaded")
    @patch("poller.status.log_mtime_ago", return_value="2h ago")
    def test_queue_has_required_keys(self, _mtime, _daemon):
        db = _make_db()
        config = {"logging": {"file": "/tmp/bp.log"}}
        result = collect_status(config, db)
        db.close()
        for key in ("needs_review", "candidate_public", "approved_public"):
            self.assertIn(key, result["queue"])

    @patch("poller.status.check_daemon", return_value="loaded")
    @patch("poller.status.log_mtime_ago", return_value="2h ago")
    def test_proposals_has_required_keys(self, _mtime, _daemon):
        db = _make_db()
        config = {"logging": {"file": "/tmp/bp.log"}}
        result = collect_status(config, db)
        db.close()
        for key in ("total", "collision", "non_conflict"):
            self.assertIn(key, result["proposals"])


class TestFormatStatus(unittest.TestCase):
    def _sample_status(self, daemon_state="loaded", collision=0):
        return {
            "daemons": [
                {"name": "poller", "state": daemon_state, "last_run": "2h ago"},
                {"name": "pipeline", "state": daemon_state, "last_run": "5h ago"},
                {"name": "reviewer", "state": daemon_state, "last_run": "serving"},
                {"name": "reconcile", "state": daemon_state, "last_run": "1d ago"},
            ],
            "queue": {
                "needs_review": 10,
                "candidate_public": 5,
                "approved_public": 3,
                "pushable": 1,
            },
            "proposals": {
                "total": collision,
                "collision": collision,
                "non_conflict": 0,
                "divergence": 0,
            },
        }

    def test_output_contains_daemon_names(self):
        out = format_status(self._sample_status())
        for name in ("poller", "pipeline", "reviewer", "reconcile"):
            self.assertIn(name, out)

    def test_output_contains_loaded_state(self):
        out = format_status(self._sample_status(daemon_state="loaded"))
        self.assertIn("loaded", out)

    def test_output_contains_not_loaded_state(self):
        out = format_status(self._sample_status(daemon_state="not loaded"))
        self.assertIn("not loaded", out)

    def test_output_contains_queue_numbers(self):
        out = format_status(self._sample_status())
        self.assertIn("10", out)  # needs_review
        self.assertIn("5", out)  # candidate_public

    def test_output_contains_proposals_section(self):
        out = format_status(self._sample_status(collision=2))
        self.assertIn("2", out)


class TestStatusExitCode(unittest.TestCase):
    def _sample_status(self, daemon_state="loaded", collision=0):
        return {
            "daemons": [
                {"name": n, "state": daemon_state, "last_run": "ok"}
                for n in ("poller", "pipeline", "reviewer", "reconcile")
            ],
            "queue": {
                "needs_review": 0,
                "candidate_public": 0,
                "approved_public": 0,
                "pushable": 0,
            },
            "proposals": {
                "total": collision,
                "collision": collision,
                "non_conflict": 0,
                "divergence": 0,
            },
        }

    def test_exit_zero_when_all_healthy(self):
        self.assertEqual(status_exit_code(self._sample_status()), 0)

    def test_exit_one_when_daemon_not_loaded(self):
        self.assertEqual(status_exit_code(self._sample_status(daemon_state="not loaded")), 1)

    def test_exit_one_when_collisions_pending(self):
        self.assertEqual(status_exit_code(self._sample_status(collision=3)), 1)


class TestBpStatusCommand(unittest.TestCase):
    """Smoke-test bp status via the CLI entry point."""

    def test_bp_status_exits_without_crashing_when_no_config(self):
        result = proc.run(
            ["python", "bp", "status", "--config", "/nonexistent/config.yml"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotEqual(result.returncode, None)
