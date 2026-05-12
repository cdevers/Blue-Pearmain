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


# ---------------------------------------------------------------------------
# Background push — file descriptor leak
# ---------------------------------------------------------------------------

@pytest.fixture
def client_for_fd_leak():
    """Flask test client with a single needs_review photo that has a Flickr ID."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        pid = test_db.upsert_photo({
            "uuid": "uuid-fd-leak",
            "original_filename": "IMG_fd.JPG",
            "privacy_state": "needs_review",
            "flickr_id": "9990000001",
            "proposed_tags": ["tag1"],
            "apple_persons": [],
            "apple_labels": [],
        })

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, pid, test_db

        app_module._db = None
        app_module._client = None


class TestBackgroundPushClosesConnection:
    """Background push threads must close their SQLite connection to avoid FD leaks."""

    def _run_decide_and_wait(self, c, photo_id, test_db, mock_flickr):
        """
        Post to /api/decide with push=True, wait for the background thread to call
        db().close(), and return the list of (thread_name, thread_ident) pairs that
        called close — filtering to only non-main, non-request threads.
        """
        import threading

        app_module._client = mock_flickr

        push_thread_closes = []
        close_done = threading.Event()
        original_close = test_db.close

        def tracking_close():
            t = threading.current_thread()
            # Only count closes from the background _push thread, not request threads
            if t is not threading.main_thread() and t.name == "_push":
                push_thread_closes.append(t.ident)
                close_done.set()
            original_close()

        test_db.close = tracking_close

        resp = c.post("/api/decide", json={
            "photo_id": photo_id,
            "decision": "make_public",
            "push": True,
        })
        assert resp.status_code == 200

        close_done.wait(timeout=5)
        return push_thread_closes

    def test_background_thread_closes_db_connection(self, client_for_fd_leak):
        """After the background push thread finishes, its DB connection must be closed."""
        c, photo_id, test_db = client_for_fd_leak
        mock_flickr = MagicMock()
        mock_flickr.set_permissions.return_value = None
        mock_flickr.add_tags.return_value = None

        closed = self._run_decide_and_wait(c, photo_id, test_db, mock_flickr)
        assert len(closed) >= 1, "background push thread did not close its DB connection"

    def test_background_thread_closes_connection_on_exception(self, client_for_fd_leak):
        """Connection must be closed even when the push raises an unexpected exception."""
        c, photo_id, test_db = client_for_fd_leak
        mock_flickr = MagicMock()
        mock_flickr.set_permissions.side_effect = RuntimeError("simulated crash")

        closed = self._run_decide_and_wait(c, photo_id, test_db, mock_flickr)
        assert len(closed) >= 1, "connection not closed after exception in background push thread"


# ---------------------------------------------------------------------------
# GH #76 — Screenshot special-casing in review UI
# ---------------------------------------------------------------------------

@pytest.fixture
def client_with_screenshots():
    """DB with one normal candidate_public and one screenshot candidate_public."""
    with tempfile.TemporaryDirectory() as tmp:
        from db.migrations.migrate_013_screenshot_flag import run as migrate
        db_path = Path(tmp) / "test.db"
        test_db = Database(db_path)
        migrate(str(db_path))

        test_db.upsert_photo({
            "uuid": "uuid-normal-001",
            "original_filename": "IMG_normal.JPG",
            "privacy_state": "candidate_public",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
            "apple_unknown_faces": 0,
            "apple_named_faces": 0,
            "is_screenshot": 0,
        })
        test_db.upsert_photo({
            "uuid": "uuid-screenshot-001",
            "original_filename": "Screenshot_2024.PNG",
            "privacy_state": "candidate_public",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
            "apple_unknown_faces": 0,
            "apple_named_faces": 0,
            "is_screenshot": 1,
        })
        # One screenshot in auto_private for the unreviewed queue
        test_db.upsert_photo({
            "uuid": "uuid-screenshot-002",
            "original_filename": "Screenshot_2024b.PNG",
            "privacy_state": "auto_private",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
            "apple_unknown_faces": 0,
            "apple_named_faces": 0,
            "is_screenshot": 1,
        })

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, test_db

        app_module._db = None


class TestCandidatePublicExcludesScreenshots:
    """candidate_public queue must not show is_screenshot=1 photos."""

    def test_screenshot_absent_from_candidate_public(self, client_with_screenshots):
        c, _ = client_with_screenshots
        resp = c.get("/review?state=candidate_public")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Screenshot_2024.PNG" not in html

    def test_normal_photo_present_in_candidate_public(self, client_with_screenshots):
        c, _ = client_with_screenshots
        resp = c.get("/review?state=candidate_public")
        html = resp.data.decode()
        assert "IMG_normal.JPG" in html


class TestScreenshotBadge:
    """is_screenshot=1 photos show a screenshot badge; others do not."""

    def test_screenshot_badge_shown_in_screenshot_queue(self, client_with_screenshots):
        c, _ = client_with_screenshots
        resp = c.get("/review?state=screenshot_unreviewed")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "screenshot" in html.lower()

    def test_no_screenshot_badge_for_normal_photo(self, client_with_screenshots):
        c, _ = client_with_screenshots
        resp = c.get("/review?state=candidate_public")
        html = resp.data.decode()
        # The badge class/text should not appear for the normal-only queue
        assert 'class="screenshot-badge"' not in html


class TestScreenshotQueueButtons:
    """In screenshot queues the Private button reads 'Confirm private'."""

    def test_confirm_private_label_in_screenshot_unreviewed(self, client_with_screenshots):
        c, _ = client_with_screenshots
        resp = c.get("/review?state=screenshot_unreviewed")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Confirm private" in html

    def test_normal_private_label_in_candidate_public(self, client_with_screenshots):
        c, _ = client_with_screenshots
        resp = c.get("/review?state=candidate_public")
        html = resp.data.decode()
        # Standard label, not the screenshot-specific one
        assert "✗ Private" in html

    def test_shortcuts_hint_says_confirm_private_in_screenshot_queue(self, client_with_screenshots):
        c, _ = client_with_screenshots
        resp = c.get("/review?state=screenshot_unreviewed")
        html = resp.data.decode()
        assert "confirm private" in html.lower()


@pytest.fixture
def client_with_confirmed_screenshot():
    """DB with one approved_public screenshot and one already_public screenshot."""
    with tempfile.TemporaryDirectory() as tmp:
        from db.migrations.migrate_013_screenshot_flag import run as migrate
        db_path = Path(tmp) / "test.db"
        test_db = Database(db_path)
        migrate(str(db_path))

        test_db.upsert_photo({
            "uuid": "uuid-ss-approved",
            "original_filename": "SS_approved.PNG",
            "privacy_state": "approved_public",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
            "apple_unknown_faces": 0,
            "apple_named_faces": 0,
            "is_screenshot": 1,
        })
        test_db.upsert_photo({
            "uuid": "uuid-ss-confirmed",
            "original_filename": "SS_confirmed.PNG",
            "privacy_state": "already_public",
            "proposed_tags": [],
            "apple_persons": [],
            "apple_labels": [],
            "apple_unknown_faces": 0,
            "apple_named_faces": 0,
            "is_screenshot": 1,
        })

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, test_db

        app_module._db = None


class TestScreenshotPublicQueue:
    """screenshot_public shows only approved_public; already_public falls off."""

    def test_approved_public_screenshot_appears(self, client_with_confirmed_screenshot):
        c, _ = client_with_confirmed_screenshot
        resp = c.get("/review?state=screenshot_public")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "SS_approved.PNG" in html

    def test_already_public_screenshot_absent(self, client_with_confirmed_screenshot):
        c, _ = client_with_confirmed_screenshot
        resp = c.get("/review?state=screenshot_public")
        html = resp.data.decode()
        assert "SS_confirmed.PNG" not in html

    def test_confirm_public_button_present(self, client_with_confirmed_screenshot):
        c, _ = client_with_confirmed_screenshot
        resp = c.get("/review?state=screenshot_public")
        html = resp.data.decode()
        assert "Confirm public" in html

    def test_confirm_public_hint_in_shortcuts(self, client_with_confirmed_screenshot):
        c, _ = client_with_confirmed_screenshot
        resp = c.get("/review?state=screenshot_public")
        html = resp.data.decode()
        assert "confirm public" in html.lower()

    def test_confirm_public_api_sets_already_public(self, client_with_confirmed_screenshot):
        c, test_db = client_with_confirmed_screenshot
        photo = test_db.conn.execute(
            "SELECT id FROM photos WHERE uuid='uuid-ss-approved'"
        ).fetchone()
        resp = c.post("/api/decide", json={
            "photo_id": photo["id"],
            "decision": "confirm_public",
        })
        assert resp.status_code == 200
        row = test_db.conn.execute(
            "SELECT privacy_state FROM photos WHERE uuid='uuid-ss-approved'"
        ).fetchone()
        assert row["privacy_state"] == "already_public"
