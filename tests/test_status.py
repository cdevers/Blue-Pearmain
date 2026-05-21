"""
tests/test_status.py — unit tests for poller.status

Run from repo root:
    python -m pytest tests/test_status.py -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from poller.status import check_daemon


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
