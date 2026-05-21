"""
tests/test_notifications.py — unit tests for utils.notifier

Run from repo root:
    python -m pytest tests/test_notifications.py -v
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.notifier import notify


class TestNotifyCallsOsascript(unittest.TestCase):
    """notify() fires osascript when enabled."""

    @patch("subprocess.run")
    def test_sends_notification_by_default(self, mock_run):
        """notify() calls osascript when no config is given."""
        notify("something happened")
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_osascript_is_the_command(self, mock_run):
        """The subprocess command starts with osascript."""
        notify("something happened")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "osascript")

    @patch("subprocess.run")
    def test_message_appears_in_command(self, mock_run):
        """The notification message appears in the osascript -e argument."""
        notify("test message payload")
        cmd_arg = mock_run.call_args[0][0][-1]
        self.assertIn("test message payload", cmd_arg)

    @patch("subprocess.run")
    def test_default_title_appears_in_command(self, mock_run):
        """The default title 'Blue Pearmain' appears in the osascript argument."""
        notify("msg")
        cmd_arg = mock_run.call_args[0][0][-1]
        self.assertIn("Blue Pearmain", cmd_arg)

    @patch("subprocess.run")
    def test_custom_title_appears_in_command(self, mock_run):
        """A custom title is passed through to the osascript argument."""
        notify("msg", title="BP Daemon")
        cmd_arg = mock_run.call_args[0][0][-1]
        self.assertIn("BP Daemon", cmd_arg)


class TestNotifyEnabledDisabled(unittest.TestCase):
    """Config-driven enable/disable."""

    @patch("subprocess.run")
    def test_disabled_by_config_skips_osascript(self, mock_run):
        """notify() does nothing when notifications.enabled is false."""
        notify("msg", config={"notifications": {"enabled": False}})
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_enabled_explicitly_by_config(self, mock_run):
        """notify() fires when notifications.enabled is true."""
        notify("msg", config={"notifications": {"enabled": True}})
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_enabled_by_default_when_notifications_key_absent(self, mock_run):
        """notify() fires when the notifications key is missing from config."""
        notify("msg", config={"flickr": {}, "database": {}})
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_enabled_by_default_when_config_is_none(self, mock_run):
        """notify() fires when config=None (no config passed)."""
        notify("msg", config=None)
        mock_run.assert_called_once()


class TestNotifyFireAndForget(unittest.TestCase):
    """Errors from osascript must never propagate."""

    @patch("subprocess.run", side_effect=FileNotFoundError("osascript not found"))
    def test_osascript_missing_does_not_raise(self, _mock_run):
        """If osascript is not installed, notify() silently does nothing."""
        notify("msg")  # must not raise

    @patch("subprocess.run", side_effect=OSError("permission denied"))
    def test_os_error_does_not_raise(self, _mock_run):
        """An OS-level error from subprocess does not propagate."""
        notify("msg")  # must not raise

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=5))
    def test_timeout_does_not_raise(self, _mock_run):
        """A subprocess timeout does not propagate."""
        notify("msg")  # must not raise


class TestNotifyEscaping(unittest.TestCase):
    """Special characters in the message are safely escaped for AppleScript."""

    @patch("subprocess.run")
    def test_double_quotes_in_message_are_escaped(self, mock_run):
        """Double quotes in the message are backslash-escaped for AppleScript."""
        notify('photo "sunset" approved')
        cmd_arg = mock_run.call_args[0][0][-1]
        # The literal " must not appear unescaped inside the AppleScript string
        self.assertIn('\\"sunset\\"', cmd_arg)

    @patch("subprocess.run")
    def test_double_quotes_in_title_are_escaped(self, mock_run):
        """Double quotes in the title are backslash-escaped."""
        notify("msg", title='BP "Daemon"')
        cmd_arg = mock_run.call_args[0][0][-1]
        self.assertIn('\\"Daemon\\"', cmd_arg)


if __name__ == "__main__":
    unittest.main()
