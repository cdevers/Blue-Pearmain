"""FlickrClient.set_location() (#145)."""

from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest
from flickr.flickr_client import FlickrClient, FlickrError


def _mock_client() -> FlickrClient:
    c = FlickrClient.__new__(FlickrClient)
    c.api_key = "key"
    c.api_secret = "secret"
    c.user_nsid = "nsid"
    c._oauth = MagicMock()
    return c


class TestSetLocation:
    def test_set_location_calls_correct_api_method(self):
        c = _mock_client()
        with patch.object(c, "_call", return_value={}) as mock_call:
            c.set_location("12345", 42.3601, -71.0589)
        mock_call.assert_called_once_with(
            "flickr.photos.geo.setLocation",
            {"photo_id": "12345", "lat": 42.3601, "lon": -71.0589, "accuracy": 16},
            http_method="POST",
        )

    def test_set_location_raises_flickr_error_on_failure(self):
        c = _mock_client()
        with patch.object(c, "_call", side_effect=FlickrError(6, "write error")):
            with pytest.raises(FlickrError):
                c.set_location("12345", 42.3601, -71.0589)

    def test_set_location_accepts_latitude_zero(self):
        """Latitude=0 (Null Island) must not be treated as falsy."""
        c = _mock_client()
        with patch.object(c, "_call", return_value={}) as mock_call:
            c.set_location("999", 0.0, 0.0)
        args = mock_call.call_args[0][1]
        assert args["lat"] == 0.0
        assert args["lon"] == 0.0
