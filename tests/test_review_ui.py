"""
tests/test_review_ui.py — integration tests for Review UI HTML/JS behaviour

Tests verify that the rendered HTML contains the expected elements and scripts
for pagination, scroll-to-top, and mobile focus fixes.

Run from repo root:
    python -m pytest tests/test_review_ui.py -v
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import reviewer.app as app_module
from db.db import Database
from flickr.flickr_client import FlickrError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """Flask test client wired to a temporary in-memory-equivalent database."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")

        # Seed enough photos to get multiple pages (per_page default = 120)
        for i in range(1, 26):
            test_db.upsert_photo({
                "uuid": f"uuid-{i:04d}",
                "original_filename": f"IMG_{i:04d}.JPG",
                "privacy_state": "needs_review",
                "proposed_tags": ["tag1", "tag2"],
                "apple_persons": [],
                "apple_labels": [],
                "apple_unknown_faces": 0,
                "apple_named_faces": 0,
            })

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c

        app_module._db = None


# ---------------------------------------------------------------------------
# Bug 1 — Reload ↺ pagination link (stays on current page, not page+1)
# ---------------------------------------------------------------------------

class TestPaginationReloadLink:
    # Use per_page=10 so that 25 seeded photos span 3 pages
    _URL = "/review?state=needs_review&page=1&per_page=10"

    def test_reload_link_present_not_skip(self, client):
        """Page 1 should show a Reload link, not just a bare page=2 link."""
        resp = client.get(self._URL)
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Reload" in html

    def test_reload_uses_js_not_href(self, client):
        """Reload link must call reloadPage() rather than navigate to same URL."""
        resp = client.get(self._URL)
        html = resp.data.decode()
        assert "reloadPage()" in html

    def test_reload_function_defined(self, client):
        """reloadPage() function must be defined in the page scripts."""
        resp = client.get(self._URL)
        html = resp.data.decode()
        assert "function reloadPage" in html

    def test_next_page_link_still_present(self, client):
        """A secondary 'Next page →' link to page+1 should still be present."""
        resp = client.get(self._URL)
        html = resp.data.decode()
        assert "Next page" in html

    def test_r_key_shortcut_in_handler(self, client):
        """Pressing R must call reloadPage() via the keyboard handler."""
        resp = client.get(self._URL)
        html = resp.data.decode()
        assert "'r'" in html or '"r"' in html
        assert "'R'" in html or '"R"' in html

    def test_r_key_shown_in_shortcuts_hint(self, client):
        """The toolbar shortcuts hint must list the R key."""
        resp = client.get(self._URL)
        html = resp.data.decode()
        assert ">R<" in html or "kbd>R" in html


# ---------------------------------------------------------------------------
# Bug 2 — Scroll to top on page load
# ---------------------------------------------------------------------------

class TestScrollToTop:
    def test_scroll_to_top_on_load(self, client):
        """The page must include a window.scrollTo call on load."""
        resp = client.get("/review?state=needs_review")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "scrollTo" in html

    def test_scroll_behavior_instant(self, client):
        """scrollTo must use behavior: 'instant' to avoid visible animation."""
        resp = client.get("/review?state=needs_review")
        html = resp.data.decode()
        assert "instant" in html

    def test_top_anchor_present(self, client):
        """The toolbar must carry id='top' so #top fragment links land correctly."""
        resp = client.get("/review?state=needs_review")
        html = resp.data.decode()
        assert 'id="top"' in html

    def test_scroll_restoration_disabled(self, client):
        """Page must set history.scrollRestoration='manual' to defeat browser restore."""
        resp = client.get("/review?state=needs_review")
        html = resp.data.decode()
        assert "scrollRestoration" in html
        assert "manual" in html

    def test_selected_reset_on_load(self, client):
        """selected must be cleared before selectCard(first) on reload."""
        resp = client.get("/review?state=needs_review")
        html = resp.data.decode()
        assert "selected = null" in html


# ---------------------------------------------------------------------------
# Bug 3 — Mobile / iOS focus behaviour
# ---------------------------------------------------------------------------

class TestMobileViewport:
    def test_prevent_scroll_focus(self, client):
        """selectCard must use preventScroll: true to avoid iOS scroll-on-focus."""
        resp = client.get("/review?state=needs_review")
        assert resp.status_code == 200
        assert "preventScroll" in resp.data.decode()

    def test_interactive_widget_viewport(self, client):
        """Base template viewport meta must include interactive-widget=resizes-content."""
        resp = client.get("/")
        assert resp.status_code == 200
        assert "interactive-widget" in resp.data.decode()

    def test_scroll_into_view_nearest(self, client):
        """selectCard must use block:'nearest' so already-visible cards don't scroll."""
        resp = client.get("/review?state=needs_review")
        html = resp.data.decode()
        assert "nearest" in html


# ---------------------------------------------------------------------------
# Album display — photo detail page
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client_with_albums():
    """Flask test client with a photo that has album membership."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")

        photo_id = test_db.upsert_photo({
            "uuid": "uuid-album-test",
            "original_filename": "IMG_album.JPG",
            "privacy_state": "candidate_public",
            "flickr_id": "flickr123",
            "proposed_tags": ["tag1"],
            "apple_persons": [],
            "apple_labels": [],
        })
        album_id = test_db.upsert_album("apple-uuid-album", "Summer 2024")
        test_db.upsert_photo_album(photo_id, album_id)

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, photo_id

        app_module._db = None


class TestPhotoDetailAlbums:
    def test_album_section_shown_on_detail_page(self, client_with_albums):
        c, photo_id = client_with_albums
        resp = c.get(f"/photo/{photo_id}?state=candidate_public")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Summer 2024" in html

    def test_album_section_heading(self, client_with_albums):
        c, photo_id = client_with_albums
        resp = c.get(f"/photo/{photo_id}?state=candidate_public")
        html = resp.data.decode()
        assert "Albums" in html and "Photosets" in html

    def test_pending_push_status_shown(self, client_with_albums):
        c, photo_id = client_with_albums
        resp = c.get(f"/photo/{photo_id}?state=candidate_public")
        html = resp.data.decode()
        assert "pending push" in html

    def test_make_public_button_mentions_photosets(self, client_with_albums):
        c, photo_id = client_with_albums
        resp = c.get(f"/photo/{photo_id}?state=candidate_public")
        html = resp.data.decode()
        assert "photosets" in html.lower()

    def test_keep_private_button_mentions_photosets(self, client_with_albums):
        c, photo_id = client_with_albums
        resp = c.get(f"/photo/{photo_id}?state=candidate_public")
        html = resp.data.decode()
        # The keep private button should also reference photosets
        assert "Keep private" in html
        assert "photosets" in html.lower()


# ---------------------------------------------------------------------------
# Album badge — review grid
# ---------------------------------------------------------------------------

class TestReviewGridAlbumBadge:
    def test_album_badge_shown_for_photos_with_albums(self, client_with_albums):
        """Grid should show an album count badge for photos that have albums."""
        c, photo_id = client_with_albums
        # The photo is candidate_public so it appears on the review grid
        resp = c.get("/review?state=candidate_public")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "album" in html.lower()

    def test_no_album_badge_when_queue_is_empty(self, client_with_albums):
        """When the queue is empty no album badge elements should be rendered."""
        c, _ = client_with_albums
        # needs_review state has no seeded photos in this fixture
        resp = c.get("/review?state=needs_review")
        html = resp.data.decode()
        # CSS class defined in <style> is fine; we check no *element* with that class is rendered
        assert '<span class="album-badge">' not in html


# ---------------------------------------------------------------------------
# Pagination page count — reflects per_page
# ---------------------------------------------------------------------------

@pytest.fixture
def client_for_pagination():
    """Isolated function-scoped client with a known photo count (25 needs_review)."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        for i in range(1, 26):
            test_db.upsert_photo({
                "uuid": f"uuid-pg-{i:04d}",
                "original_filename": f"IMG_pg_{i:04d}.JPG",
                "privacy_state": "needs_review",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
                "apple_unknown_faces": 0,
                "apple_named_faces": 0,
            })

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c

        app_module._db = None


class TestPaginationPageCount:
    """The 'N photos · page X/Y' header and bottom pager must use per_page."""

    def test_default_per_page_is_120(self, client_for_pagination):
        """With 25 photos and the default per_page (120), total pages must be 1."""
        resp = client_for_pagination.get("/review?state=needs_review")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "page 1/1" in html

    def test_total_pages_reflects_per_page(self, client_for_pagination):
        """With 25 photos and per_page=10, total pages must be 3."""
        resp = client_for_pagination.get("/review?state=needs_review&per_page=10")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "page 1/3" in html

    def test_total_pages_single_page(self, client_for_pagination):
        """With per_page larger than total, total pages must be 1."""
        resp = client_for_pagination.get("/review?state=needs_review&per_page=200")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "page 1/1" in html

    def test_per_page_two_gives_correct_count(self, client_for_pagination):
        """With 25 photos and per_page=2, total pages must be 13 (ceil(25/2))."""
        resp = client_for_pagination.get("/review?state=needs_review&per_page=2")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "page 1/13" in html


# ---------------------------------------------------------------------------
# Push approved — graceful handling of Flickr "photo not found"
# ---------------------------------------------------------------------------

@pytest.fixture
def client_with_approved_photos():
    """Flask test client with two approved_public photos that have Flickr IDs."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")

        for i in range(1, 3):
            pid = test_db.upsert_photo({
                "uuid": f"uuid-push-{i}",
                "original_filename": f"IMG_push_{i}.JPG",
                "privacy_state": "approved_public",
                "flickr_id": f"5555000000{i}",
                "proposed_tags": ["tag1"],
                "apple_persons": [],
                "apple_labels": [],
            })
            # Mark as not yet pushed
            test_db.conn.execute(
                "UPDATE photos SET perms_pushed_flickr = 0 WHERE id = ?", (pid,)
            )
            test_db.conn.commit()

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, test_db

        app_module._db = None
        app_module._client = None


class TestPushApprovedNotFound:
    """api/push_approved must treat Flickr 'photo not found' as a skipped warning."""

    def test_not_found_counted_as_skipped_not_failed(self, client_with_approved_photos):
        """When Flickr returns error 1 (not found), the photo is skipped, not failed."""
        c, _ = client_with_approved_photos
        mock_flickr = MagicMock()
        mock_flickr.set_permissions.side_effect = FlickrError(1, 'Photo not found')
        app_module._client = mock_flickr

        resp = c.post("/api/push_approved")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["failed"] == 0
        assert data["skipped"] == 2

    def test_not_found_marks_photo_as_done(self, client_with_approved_photos):
        """A not-found photo must be flagged pushed so it is not retried next time."""
        c, test_db = client_with_approved_photos
        mock_flickr = MagicMock()
        mock_flickr.set_permissions.side_effect = FlickrError(1, 'Photo not found')
        app_module._client = mock_flickr

        c.post("/api/push_approved")

        rows = test_db.conn.execute(
            "SELECT perms_pushed_flickr FROM photos WHERE flickr_id LIKE '55550%'"
        ).fetchall()
        assert all(row["perms_pushed_flickr"] == 1 for row in rows)

    def test_not_found_does_not_block_other_photos(self, client_with_approved_photos):
        """Photos after a not-found photo must still be processed."""
        c, test_db = client_with_approved_photos
        call_count = 0

        def fake_set_permissions(flickr_id, **kwargs):
            nonlocal call_count
            call_count += 1
            # Only fail the first photo
            if call_count == 1:
                raise FlickrError(1, 'Photo not found')

        mock_flickr = MagicMock()
        mock_flickr.set_permissions.side_effect = fake_set_permissions
        app_module._client = mock_flickr

        resp = c.post("/api/push_approved")
        data = resp.get_json()
        assert data["skipped"] == 1
        assert data["pushed"] == 1
        assert data["failed"] == 0

    def test_other_flickr_errors_still_count_as_failures(self, client_with_approved_photos):
        """Non-404 Flickr errors must still be counted as failures, not skipped."""
        c, _ = client_with_approved_photos
        mock_flickr = MagicMock()
        mock_flickr.set_permissions.side_effect = FlickrError(100, 'Invalid API Key')
        app_module._client = mock_flickr

        resp = c.post("/api/push_approved")
        data = resp.get_json()
        assert data["failed"] == 2
        assert data["skipped"] == 0
