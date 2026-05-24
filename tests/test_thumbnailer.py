"""
tests/test_thumbnailer.py — unit tests for derivative_path()
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from poller.thumbnailer import derivative_path


class TestDerivativePath:
    def test_masters_path(self, tmp_path):
        """derivative found at resources/derivatives/masters/{shard}/"""
        uuid = "AAAA1234-0000-0000-0000-000000000000"
        shard = "a"
        d = tmp_path / "resources" / "derivatives" / "masters" / shard
        d.mkdir(parents=True)
        deriv = d / f"{uuid}_4_5005_c.jpeg"
        deriv.write_bytes(b"fake-jpeg")

        result = derivative_path(uuid, str(tmp_path))

        assert result == str(deriv)

    def test_shard_path_when_masters_missing(self, tmp_path):
        """masters/ missing → falls back to resources/derivatives/{shard}/"""
        uuid = "BBBB1234-0000-0000-0000-000000000000"
        shard = "b"
        d = tmp_path / "resources" / "derivatives" / shard
        d.mkdir(parents=True)
        deriv = d / f"{uuid}_1_105_c.jpeg"
        deriv.write_bytes(b"fake-jpeg")

        result = derivative_path(uuid, str(tmp_path))

        assert result == str(deriv)

    def test_momentshared_path_when_others_missing(self, tmp_path):
        """masters/ and shard/ missing → falls back to scopes/momentshared/"""
        uuid = "CCCC1234-0000-0000-0000-000000000000"
        shard = "c"
        d = tmp_path / "scopes" / "momentshared" / "resources" / "derivatives" / "masters" / shard
        d.mkdir(parents=True)
        deriv = d / f"{uuid}_4_5005_c.jpeg"
        deriv.write_bytes(b"fake-jpeg")

        result = derivative_path(uuid, str(tmp_path))

        assert result == str(deriv)

    def test_returns_none_when_no_candidate_exists(self, tmp_path):
        """No derivative file present → returns None."""
        uuid = "DDDD1234-0000-0000-0000-000000000000"

        result = derivative_path(uuid, str(tmp_path))

        assert result is None
