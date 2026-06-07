# tests/test_flickr_upload.py
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from flickr.flickr_client import FlickrClient, FlickrError  # noqa: E402


def _client() -> FlickrClient:
    c = FlickrClient.__new__(FlickrClient)
    c.api_key = "key"
    c.user_nsid = "nsid"
    c._rate_delay = 0
    c._session = MagicMock()
    return c


def _ok_response(flickr_id: str = "99887766") -> MagicMock:
    m = MagicMock()
    m.text = f'<?xml version="1.0" ?><rsp stat="ok"><photoid>{flickr_id}</photoid></rsp>'
    m.raise_for_status = MagicMock()
    return m


def _err_response(msg: str = "oops") -> MagicMock:
    m = MagicMock()
    m.text = f'<?xml version="1.0" ?><rsp stat="fail"><err code="5" msg="{msg}" /></rsp>'
    m.raise_for_status = MagicMock()
    return m


class TestUploadPhoto:
    def test_posts_to_upload_url(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response()
        with patch.object(c, "_call", return_value={}):
            c.upload_photo(photo)
        url = c._session.post.call_args[0][0]
        assert url == "https://up.flickr.com/services/upload/"

    def test_returns_flickr_id_and_date_set_ok_true(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response("42")
        with patch.object(c, "_call", return_value={}):
            flickr_id, date_set_ok = c.upload_photo(photo, date_taken="2005-06-01 12:00:00")
        assert flickr_id == "42"
        assert date_set_ok is True

    def test_always_uploads_private(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response()
        with patch.object(c, "_call", return_value={}):
            c.upload_photo(photo)
        data = c._session.post.call_args[1]["data"]
        assert data["is_public"] == "0"
        assert data["is_friend"] == "0"
        assert data["is_family"] == "0"

    def test_passes_metadata_fields(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response()
        with patch.object(c, "_call", return_value={}):
            c.upload_photo(photo, title="Beach", description="Sunny", tags="beach summer")
        data = c._session.post.call_args[1]["data"]
        assert data["title"] == "Beach"
        assert data["description"] == "Sunny"
        assert data["tags"] == "beach summer"

    def test_calls_set_dates_when_date_taken_provided(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response("55")
        with patch.object(c, "_call", return_value={}) as mock_call:
            c.upload_photo(photo, date_taken="2005-06-01 12:00:00")
        mock_call.assert_called_once_with(
            "flickr.photos.setDates",
            {"photo_id": "55", "date_taken": "2005-06-01 12:00:00", "date_taken_granularity": "0"},
            http_method="POST",
        )

    def test_skips_set_dates_when_no_date_taken(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response()
        with patch.object(c, "_call", return_value={}) as mock_call:
            c.upload_photo(photo)
        mock_call.assert_not_called()

    def test_returns_date_set_ok_false_when_set_dates_fails(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _ok_response("77")
        with patch.object(c, "_call", side_effect=FlickrError(1, "not found")):
            flickr_id, date_set_ok = c.upload_photo(photo, date_taken="2005-06-01 12:00:00")
        assert flickr_id == "77"
        assert date_set_ok is False

    def test_raises_flickr_error_on_stat_fail(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        c._session.post.return_value = _err_response("quota exceeded")
        with pytest.raises(FlickrError):
            c.upload_photo(photo)

    def test_raises_on_missing_photoid_element(self, tmp_path):
        photo = tmp_path / "img.jpg"
        photo.write_bytes(b"JPEG")
        c = _client()
        m = MagicMock()
        m.text = '<?xml version="1.0" ?><rsp stat="ok"></rsp>'  # no <photoid>
        m.raise_for_status = MagicMock()
        c._session.post.return_value = m
        with pytest.raises(FlickrError):
            c.upload_photo(photo)
