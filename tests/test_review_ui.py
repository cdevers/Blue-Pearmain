"""
tests/test_review_ui.py — integration tests for Review UI HTML/JS behaviour

Tests verify that the rendered HTML contains the expected elements and scripts
for pagination, scroll-to-top, and mobile focus fixes.

Run from repo root:
    python -m pytest tests/test_review_ui.py -v
"""

import json as _json
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
            test_db.upsert_photo(
                {
                    "uuid": f"uuid-{i:04d}",
                    "original_filename": f"IMG_{i:04d}.JPG",
                    "privacy_state": "needs_review",
                    "proposed_tags": ["tag1", "tag2"],
                    "apple_persons": [],
                    "apple_labels": [],
                    "apple_unknown_faces": 0,
                    "apple_named_faces": 0,
                }
            )

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

        photo_id = test_db.upsert_photo(
            {
                "uuid": "uuid-album-test",
                "original_filename": "IMG_album.JPG",
                "privacy_state": "candidate_public",
                "flickr_id": "flickr123",
                "proposed_tags": ["tag1"],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
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
# Back link — photo detail
# ---------------------------------------------------------------------------


class TestPhotoDetailBackLink:
    """Back link in photo detail page honours the ?back= query param."""

    def test_back_param_renders_library_link(self, client_with_albums):
        """?back=/library → back link points to /library, not the review queue."""
        c, photo_id = client_with_albums
        import urllib.parse

        back = urllib.parse.quote("/library?album_id=3", safe="")
        resp = c.get(f"/photo/{photo_id}?back={back}")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Back to Library" in html
        assert "/library?album_id=3" in html
        # Should NOT contain the review-queue back link when ?back= is set
        assert "url_for" not in html  # rendered HTML never contains template syntax

    def test_no_back_param_renders_review_link(self, client_with_albums):
        """No ?back= → back link still points to the review queue."""
        c, photo_id = client_with_albums
        resp = c.get(f"/photo/{photo_id}?state=candidate_public")
        assert resp.status_code == 200
        html = resp.data.decode()
        # The review-queue back link uses url_for('review', ...) which renders as /review
        assert "/review" in html
        assert "Back to Library" not in html


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
            test_db.upsert_photo(
                {
                    "uuid": f"uuid-pg-{i:04d}",
                    "original_filename": f"IMG_pg_{i:04d}.JPG",
                    "privacy_state": "needs_review",
                    "proposed_tags": [],
                    "apple_persons": [],
                    "apple_labels": [],
                    "apple_unknown_faces": 0,
                    "apple_named_faces": 0,
                }
            )

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
            pid = test_db.upsert_photo(
                {
                    "uuid": f"uuid-push-{i}",
                    "original_filename": f"IMG_push_{i}.JPG",
                    "privacy_state": "approved_public",
                    "flickr_id": f"5555000000{i}",
                    "proposed_tags": ["tag1"],
                    "apple_persons": [],
                    "apple_labels": [],
                }
            )
            # Mark as not yet pushed
            test_db.conn.execute("UPDATE photos SET perms_pushed_flickr = 0 WHERE id = ?", (pid,))
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
        mock_flickr.set_permissions.side_effect = FlickrError(1, "Photo not found")
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
        mock_flickr.set_permissions.side_effect = FlickrError(1, "Photo not found")
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
                raise FlickrError(1, "Photo not found")

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
        mock_flickr.set_permissions.side_effect = FlickrError(100, "Invalid API Key")
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
        pid = test_db.upsert_photo(
            {
                "uuid": "uuid-fd-leak",
                "original_filename": "IMG_fd.JPG",
                "privacy_state": "needs_review",
                "flickr_id": "9990000001",
                "proposed_tags": ["tag1"],
                "apple_persons": [],
                "apple_labels": [],
            }
        )

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

        resp = c.post(
            "/api/decide",
            json={
                "photo_id": photo_id,
                "decision": "make_public",
                "push": True,
            },
        )
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

        test_db.upsert_photo(
            {
                "uuid": "uuid-normal-001",
                "original_filename": "IMG_normal.JPG",
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
                "apple_unknown_faces": 0,
                "apple_named_faces": 0,
                "is_screenshot": 0,
            }
        )
        test_db.upsert_photo(
            {
                "uuid": "uuid-screenshot-001",
                "original_filename": "Screenshot_2024.PNG",
                "privacy_state": "candidate_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
                "apple_unknown_faces": 0,
                "apple_named_faces": 0,
                "is_screenshot": 1,
            }
        )
        # One screenshot in auto_private for the unreviewed queue
        test_db.upsert_photo(
            {
                "uuid": "uuid-screenshot-002",
                "original_filename": "Screenshot_2024b.PNG",
                "privacy_state": "auto_private",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
                "apple_unknown_faces": 0,
                "apple_named_faces": 0,
                "is_screenshot": 1,
            }
        )

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

        test_db.upsert_photo(
            {
                "uuid": "uuid-ss-approved",
                "original_filename": "SS_approved.PNG",
                "privacy_state": "approved_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
                "apple_unknown_faces": 0,
                "apple_named_faces": 0,
                "is_screenshot": 1,
            }
        )
        test_db.upsert_photo(
            {
                "uuid": "uuid-ss-confirmed",
                "original_filename": "SS_confirmed.PNG",
                "privacy_state": "already_public",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
                "apple_unknown_faces": 0,
                "apple_named_faces": 0,
                "is_screenshot": 1,
            }
        )

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
        resp = c.post(
            "/api/decide",
            json={
                "photo_id": photo["id"],
                "decision": "confirm_public",
            },
        )
        assert resp.status_code == 200
        row = test_db.conn.execute(
            "SELECT privacy_state FROM photos WHERE uuid='uuid-ss-approved'"
        ).fetchone()
        assert row["privacy_state"] == "already_public"


@pytest.fixture
def client_with_merge_group():
    """DB with one unresolved snapbridge group: Flickr-only donor + Photos-linked target."""
    with tempfile.TemporaryDirectory() as tmp:
        from db.migrations.migrate_003_dimensions_and_dedup import run as migrate_003

        db_path = Path(tmp) / "test.db"
        test_db = Database(db_path)
        migrate_003(
            str(db_path)
        )  # creates duplicate_groups table + duplicate_role/duplicate_group_id

        # Flickr-only donor
        donor_id = test_db.upsert_photo(
            {
                "flickr_id": "F001",
                "flickr_secret": "sec",
                "flickr_server": "65535",
                "original_filename": "IMG_999.JPG",
                "date_taken": "2024-06-15 12:00:00",
                "privacy_state": "candidate_public",
            }
        )

        # Photos-linked target (higher-res)
        target_id = test_db.upsert_photo(
            {
                "uuid": "U001",
                "original_filename": "IMG_999.JPG",
                "date_taken": "2024-06-15T12:00:00-04:00",
                "privacy_state": "candidate_public",
                "width": 4000,
                "height": 3000,
                "apple_labels": [],
                "apple_persons": [],
                "proposed_tags": [],
            }
        )

        # Link both to a duplicate group
        test_db.conn.execute(
            "INSERT INTO duplicate_groups (match_key, group_type, photo_count) VALUES (?,?,?)",
            ("IMG_999.JPG|2024-06-15 12:00:00", "snapbridge", 2),
        )
        group_id = test_db.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id = ?, duplicate_role = 'discard' WHERE id = ?",
            (group_id, donor_id),
        )
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id = ?, duplicate_role = 'keeper' WHERE id = ?",
            (group_id, target_id),
        )
        test_db.conn.commit()

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, test_db, group_id, donor_id, target_id

        app_module._db = None


class TestMergeUI:
    """API and UI tests for the duplicate merge action."""

    def test_merge_action_returns_ok(self, client_with_merge_group):
        c, db, group_id, donor_id, target_id = client_with_merge_group
        resp = c.post(
            f"/api/duplicates/{group_id}/assign",
            json={"action": "merge", "donor_id": donor_id, "target_id": target_id},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_merge_with_photo_not_in_group_returns_400(self, client_with_merge_group):
        c, db, group_id, donor_id, target_id = client_with_merge_group
        resp = c.post(
            f"/api/duplicates/{group_id}/assign",
            json={"action": "merge", "donor_id": 99999, "target_id": target_id},
        )
        assert resp.status_code == 400

    def test_merge_with_donor_having_uuid_returns_400(self, client_with_merge_group):
        c, db, group_id, donor_id, target_id = client_with_merge_group
        # target_id has a uuid — passing it as the donor must be rejected
        resp = c.post(
            f"/api/duplicates/{group_id}/assign",
            json={"action": "merge", "donor_id": target_id, "target_id": donor_id},
        )
        assert resp.status_code == 400

    def test_merge_button_shown_on_flickr_only_card(self, client_with_merge_group):
        c, db, group_id, donor_id, target_id = client_with_merge_group
        resp = c.get("/duplicates")
        assert resp.status_code == 200
        assert b"Merge into Photos record" in resp.data

    def test_merge_button_appears_exactly_once(self, client_with_merge_group):
        c, db, group_id, donor_id, target_id = client_with_merge_group
        resp = c.get("/duplicates")
        # Only the Flickr-only card (donor) should have the button; the Photos-linked card should not
        assert resp.data.decode().count("Merge into Photos record") == 1


# ---------------------------------------------------------------------------
# TestProposalJsDefensiveHandling — GH #79
# ---------------------------------------------------------------------------


class TestProposalJsDefensiveHandling:
    """JS handlers in proposals.html must have try-catch, AbortController timeout,
    and correct button-text restoration on error."""

    @pytest.fixture(scope="class")
    def proposals_src(self):
        path = Path(__file__).parent.parent / "reviewer" / "templates" / "proposals.html"
        return path.read_text()

    def test_approve_proposal_has_abort_controller(self, proposals_src):
        # approveProposal must create an AbortController to unblock the UI on server hang
        assert "AbortController" in proposals_src

    def test_approve_proposal_has_try_catch(self, proposals_src):
        # approveProposal must wrap the fetch in try-catch so network errors re-enable the button
        assert "} catch" in proposals_src

    def test_approve_proposal_restores_orig_text_on_error(self, proposals_src):
        # Error path must restore the button's original text, not hardcode 'Approve ✓'
        # (collision buttons say 'Use Flickr ✓' / 'Use Photos ✓')
        assert "origText" in proposals_src
        assert "btn.textContent = 'Approve ✓'" not in proposals_src

    def test_bulk_approve_has_abort_controller(self, proposals_src):
        # bulkApprove must also have AbortController timeout protection
        idx = proposals_src.find("async function bulkApprove")
        assert idx != -1
        assert "AbortController" in proposals_src[idx : idx + 600]

    def test_approve_reverse_has_try_catch(self, proposals_src):
        # approveReverse must also wrap in try-catch
        idx = proposals_src.find("async function approveReverse")
        assert idx != -1
        assert "} catch" in proposals_src[idx : idx + 900]


# ---------------------------------------------------------------------------
# TestProposalRoutes — GH #80
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_proposals():
    """Flask test client seeded with non-conflict, divergence, and collision proposals."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        now = "2026-01-01T00:00:00+00:00"

        photo_id = test_db.upsert_photo(
            {
                "uuid": "uuid-prop-001",
                "flickr_id": "flickr-prop-001",
                "original_filename": "IMG_prop.JPG",
                "privacy_state": "needs_review",
                "proposed_tags": [],
                "apple_persons": [],
                "apple_labels": [],
                "photos_tags": ["nature", "travel"],
            }
        )

        def _ins(proposed_value, source, target, conflict_type):
            test_db.conn.execute(
                """INSERT INTO metadata_proposals
                   (photo_id, field, proposed_value, source, target, conflict_type, status, created_at)
                   VALUES (?, 'tags', ?, ?, ?, ?, 'pending', ?)""",
                (photo_id, proposed_value, source, target, conflict_type, now),
            )
            return test_db.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

        non_conflict_id = _ins('["flickrtag"]', "flickr", "photos", "non_conflict")
        divergence_id = _ins('["flickrtag","extra"]', "flickr", "photos", "divergence")
        collision_f2p_id = _ins('["flickrcol"]', "flickr", "photos", "collision")
        collision_p2f_id = _ins('["photoscol"]', "photos", "flickr", "collision")
        test_db.conn.commit()

        mock_flickr = MagicMock()
        app_module._db = test_db
        app_module._client = mock_flickr
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield (
                c,
                test_db,
                non_conflict_id,
                divergence_id,
                collision_f2p_id,
                collision_p2f_id,
                mock_flickr,
            )

        app_module._db = None
        app_module._client = None


class TestProposalRoutes:
    """Route-level tests for the proposal API endpoints (GH #80)."""

    def test_approve_non_conflict_returns_ok(self, client_with_proposals):
        from unittest.mock import patch

        c, db, nc_id, div_id, col_f2p, col_p2f, _ = client_with_proposals
        with (
            patch.dict("sys.modules", {"photoscript": MagicMock()}),
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch(
                "flickr.proposal_applier._run_with_timeout",
                return_value={"ok": True, "written": ["flickrtag"]},
            ),
        ):
            resp = c.post(f"/api/proposals/{nc_id}/approve")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_approve_returns_not_ok_when_photos_not_responding(self, client_with_proposals):
        from unittest.mock import patch

        c, db, nc_id, div_id, col_f2p, col_p2f, _ = client_with_proposals
        with patch("flickr.proposal_applier._photos_is_responsive", return_value=False):
            resp = c.post(f"/api/proposals/{nc_id}/approve")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert "not responding" in data.get("reason", "").lower()

    def test_approve_resolves_collision_sibling(self, client_with_proposals):
        from unittest.mock import patch

        c, db, nc_id, div_id, col_f2p, col_p2f, _ = client_with_proposals
        with (
            patch.dict("sys.modules", {"photoscript": MagicMock()}),
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch(
                "flickr.proposal_applier._run_with_timeout",
                return_value={"ok": True, "written": ["flickrcol"]},
            ),
        ):
            resp = c.post(f"/api/proposals/{col_f2p}/approve")
        assert resp.get_json()["ok"] is True
        row = db.conn.execute(
            "SELECT status FROM metadata_proposals WHERE id=?", (col_p2f,)
        ).fetchone()
        assert row["status"] == "rejected"

    def test_approve_reverse_returns_ok(self, client_with_proposals):
        c, db, nc_id, div_id, col_f2p, col_p2f, mock_flickr = client_with_proposals
        resp = c.post(f"/api/proposals/{col_f2p}/approve-reverse")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        mock_flickr.set_tags.assert_called_once()

    def test_bulk_approve_non_conflict(self, client_with_proposals):
        from unittest.mock import patch

        c, db, nc_id, div_id, col_f2p, col_p2f, _ = client_with_proposals
        with (
            patch.dict("sys.modules", {"photoscript": MagicMock()}),
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch(
                "flickr.proposal_applier._run_with_timeout",
                return_value={"ok": True, "written": ["flickrtag"]},
            ),
        ):
            resp = c.post("/api/proposals/bulk-approve", json={"conflict_type": "non_conflict"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["applied"] >= 1

    def test_bulk_approve_divergence(self, client_with_proposals):
        from unittest.mock import patch

        c, db, nc_id, div_id, col_f2p, col_p2f, _ = client_with_proposals
        with (
            patch.dict("sys.modules", {"photoscript": MagicMock()}),
            patch("flickr.proposal_applier._photos_is_responsive", return_value=True),
            patch(
                "flickr.proposal_applier._run_with_timeout",
                return_value={"ok": True, "written": ["flickrtag", "extra"]},
            ),
        ):
            resp = c.post("/api/proposals/bulk-approve", json={"conflict_type": "divergence"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["applied"] >= 1


# ---------------------------------------------------------------------------
# GH #3 — mDNS/Bonjour registration
# ---------------------------------------------------------------------------


class TestMDnsRegistration:
    """Tests for _start_mdns: Bonjour _http._tcp registration on LAN startup."""

    def _call(self, host, port, lan_ip, *, mock_zc_module=None):
        from unittest.mock import patch

        if mock_zc_module is None:
            mock_zc_module = MagicMock()
        with patch.dict("sys.modules", {"zeroconf": mock_zc_module}):
            app_module._start_mdns(host, port, lan_ip)
        return mock_zc_module

    def test_skips_when_localhost(self):
        """No mDNS registration when host is 127.0.0.1."""
        m = self._call("127.0.0.1", 5173, "192.168.1.100")
        m.Zeroconf.assert_not_called()

    def test_skips_when_localhost_string(self):
        """No mDNS registration when host is 'localhost'."""
        m = self._call("localhost", 5173, "192.168.1.100")
        m.Zeroconf.assert_not_called()

    def test_skips_when_no_lan_ip(self):
        """No mDNS registration when lan_ip is None."""
        m = self._call("0.0.0.0", 5173, None)
        m.Zeroconf.assert_not_called()

    def test_registers_http_tcp_service(self):
        """Registers a _http._tcp.local. service when binding on LAN."""
        m = self._call("0.0.0.0", 5173, "192.168.1.100")
        m.Zeroconf.return_value.register_service.assert_called_once()
        type_arg = m.ServiceInfo.call_args[0][0]
        assert type_arg == "_http._tcp.local."

    def test_registers_blue_pearmain_name(self):
        """Service name contains 'blue-pearmain'."""
        m = self._call("0.0.0.0", 5173, "192.168.1.100")
        name_arg = m.ServiceInfo.call_args[0][1]
        assert "blue-pearmain" in name_arg

    def test_registers_blue_pearmain_local_server(self):
        """ServiceInfo server= is 'blue-pearmain.local.' so the hostname resolves."""
        m = self._call("0.0.0.0", 5173, "192.168.1.100")
        kwargs = m.ServiceInfo.call_args[1]
        assert kwargs.get("server") == "blue-pearmain.local."

    def test_registers_correct_port(self):
        """ServiceInfo receives the port passed to _start_mdns."""
        m = self._call("0.0.0.0", 8888, "10.0.0.5")
        kwargs = m.ServiceInfo.call_args[1]
        assert kwargs.get("port") == 8888

    def test_survives_import_error(self):
        """Missing zeroconf package is handled without raising."""
        from unittest.mock import patch

        with patch.dict("sys.modules", {"zeroconf": None}):
            app_module._start_mdns("0.0.0.0", 5173, "192.168.1.100")


# ---------------------------------------------------------------------------
# TestFriendsVisibilityUI — GH #19 (Tasks 3 & 7)
# ---------------------------------------------------------------------------


class TestFriendsVisibilityUI:
    """review.html, photo.html, and base.html must contain Friends/Family UI elements."""

    @pytest.fixture(scope="class")
    def review_src(self):
        return (Path(__file__).parent.parent / "reviewer" / "templates" / "review.html").read_text()

    @pytest.fixture(scope="class")
    def photo_src(self):
        return (Path(__file__).parent.parent / "reviewer" / "templates" / "photo.html").read_text()

    @pytest.fixture(scope="class")
    def base_src(self):
        return (Path(__file__).parent.parent / "reviewer" / "templates" / "base.html").read_text()

    # --- review.html: state filter dropdown ---

    def test_review_filter_has_approved_friends(self, review_src):
        assert 'value="approved_friends"' in review_src

    def test_review_filter_has_approved_family(self, review_src):
        assert 'value="approved_family"' in review_src

    def test_review_filter_has_approved_friends_family(self, review_src):
        assert 'value="approved_friends_family"' in review_src

    # --- review.html: JS _decisionToState map ---

    def test_review_js_map_has_make_friends(self, review_src):
        assert "make_friends" in review_src

    def test_review_js_map_has_make_family(self, review_src):
        assert "make_family" in review_src

    def test_review_js_map_has_make_friends_family(self, review_src):
        assert "make_friends_family" in review_src

    # --- review.html: More toggle and restricted buttons ---

    def test_review_card_has_more_toggle(self, review_src):
        assert "btn-more" in review_src

    def test_review_card_has_friends_button(self, review_src):
        assert "'make_friends'" in review_src

    def test_review_card_has_family_button(self, review_src):
        assert "'make_family'" in review_src

    def test_review_card_has_friends_family_button(self, review_src):
        assert "'make_friends_family'" in review_src

    # --- photo.html: decision buttons ---

    def test_photo_detail_has_make_friends(self, photo_src):
        assert "make_friends" in photo_src

    def test_photo_detail_has_make_family(self, photo_src):
        assert "make_family" in photo_src

    def test_photo_detail_has_make_friends_family(self, photo_src):
        assert "make_friends_family" in photo_src

    # --- base.html: toast messages ---

    def test_base_toast_handles_make_friends(self, base_src):
        assert "make_friends" in base_src


# ---------------------------------------------------------------------------
# TestApiPushApprovedWritesPushedTags — GH #99
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_push_approved_photo():
    """Flask test client with a single approved_public photo ready for push."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")

        pid = test_db.upsert_photo(
            {
                "uuid": "uuid-pa-tags-001",
                "original_filename": "IMG_pa_tags.JPG",
                "privacy_state": "approved_public",
                "flickr_id": "8880000001",
                "proposed_tags": ["alpha", "beta"],
                "apple_persons": [],
                "apple_labels": [],
            }
        )
        test_db.conn.execute("UPDATE photos SET perms_pushed_flickr = 0 WHERE id = ?", (pid,))
        test_db.conn.commit()

        mock_flickr = MagicMock()
        app_module._db = test_db
        app_module._client = mock_flickr
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, test_db, mock_flickr

        app_module._db = None
        app_module._client = None


class TestApiPushApprovedWritesPushedTags:
    """api/push_approved must record pushed_tags in the DB after a successful add_tags call."""

    def test_pushed_tags_written_after_add_tags_succeeds(self, client_with_push_approved_photo):
        """pushed_tags must be set to proposed_tags JSON after a successful add_tags call."""
        c, test_db, _ = client_with_push_approved_photo

        resp = c.post("/api/push_approved")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        row = test_db.conn.execute(
            "SELECT pushed_tags FROM photos WHERE flickr_id = '8880000001'"
        ).fetchone()
        assert row is not None
        assert row["pushed_tags"] is not None
        import json as _json

        pushed = _json.loads(row["pushed_tags"])
        assert sorted(pushed) == ["alpha", "beta"]

    def test_pushed_tags_null_when_add_tags_fails(self, client_with_push_approved_photo):
        """pushed_tags must remain NULL when add_tags raises a FlickrError."""
        c, test_db, mock_flickr = client_with_push_approved_photo
        mock_flickr.add_tags.side_effect = FlickrError(100, "Invalid API Key")
        # Reset mock state from any prior test in this fixture
        mock_flickr.set_permissions.side_effect = None

        resp = c.post("/api/push_approved")
        assert resp.status_code == 200

        row = test_db.conn.execute(
            "SELECT pushed_tags FROM photos WHERE flickr_id = '8880000001'"
        ).fetchone()
        assert row is not None
        assert row["pushed_tags"] is None


# ---------------------------------------------------------------------------
# TestReuploadDuplicatesUI — GH #106
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_reupload_group():
    """DB with one unresolved reupload group: keeper + discard with notes JSON."""
    with tempfile.TemporaryDirectory() as tmp:
        from db.migrations.migrate_003_dimensions_and_dedup import run as migrate_003

        db_path = Path(tmp) / "test.db"
        test_db = Database(db_path)
        migrate_003(str(db_path))

        notes = _json.dumps(
            {
                "summary": "Higher-res Flickr copy of a local photo",
                "upload_session_gap": "14 days",
                "filename_match": True,
                "dimension_ratio": 0.85,
                "keeper_flickr_id": "48910000",
                "discard_flickr_id": "48900000",
            }
        )

        # Keeper (higher-res Flickr-only)
        keeper_id = test_db.upsert_photo(
            {
                "flickr_id": "48910000",
                "flickr_secret": "sec1",
                "flickr_server": "65535",
                "original_filename": "IMG_001.JPG",
                "date_taken": "2024-06-01 12:00:00",
                "privacy_state": "candidate_public",
                "width": 4000,
                "height": 3000,
            }
        )

        # Discard (lower-res, already marked)
        discard_id = test_db.upsert_photo(
            {
                "flickr_id": "48900000",
                "flickr_secret": "sec2",
                "flickr_server": "65535",
                "original_filename": "IMG_001.JPG",
                "date_taken": "2024-06-01 12:00:00",
                "privacy_state": "duplicate_flickr",
            }
        )

        test_db.conn.execute(
            "INSERT INTO duplicate_groups (match_key, group_type, photo_count, notes)"
            " VALUES (?,?,?,?)",
            ("reupload:48900000:48910000", "reupload", 2, notes),
        )
        group_id = test_db.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id=?, duplicate_role='keeper' WHERE id=?",
            (group_id, keeper_id),
        )
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id=?, duplicate_role='discard' WHERE id=?",
            (group_id, discard_id),
        )
        test_db.conn.commit()

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, test_db, group_id

        app_module._db = None


@pytest.fixture
def client_with_reupload_uncertain_group():
    """DB with one unresolved reupload_uncertain group (no notes JSON)."""
    with tempfile.TemporaryDirectory() as tmp:
        from db.migrations.migrate_003_dimensions_and_dedup import run as migrate_003

        db_path = Path(tmp) / "test.db"
        test_db = Database(db_path)
        migrate_003(str(db_path))

        keeper_id = test_db.upsert_photo(
            {
                "flickr_id": "48920000",
                "flickr_secret": "sec3",
                "flickr_server": "65535",
                "original_filename": "IMG_002.JPG",
                "date_taken": "2024-07-01 10:00:00",
                "privacy_state": "candidate_public",
            }
        )
        discard_id = test_db.upsert_photo(
            {
                "flickr_id": "48910001",
                "flickr_secret": "sec4",
                "flickr_server": "65535",
                "original_filename": "IMG_002.JPG",
                "date_taken": "2024-07-01 10:00:00",
                "privacy_state": "candidate_public",
            }
        )

        test_db.conn.execute(
            "INSERT INTO duplicate_groups (match_key, group_type, photo_count) VALUES (?,?,?)",
            ("reupload:48910001:48920000", "reupload_uncertain", 2),
        )
        group_id = test_db.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id=?, duplicate_role='keeper' WHERE id=?",
            (group_id, keeper_id),
        )
        test_db.conn.execute(
            "UPDATE photos SET duplicate_group_id=?, duplicate_role='discard' WHERE id=?",
            (group_id, discard_id),
        )
        test_db.conn.commit()

        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"

        with app_module.app.test_client() as c:
            yield c, test_db, group_id

        app_module._db = None


class TestReuploadDuplicatesUI:
    """GH #106 — reupload/reupload_uncertain groups appear in /duplicates."""

    def test_reupload_group_appears_in_duplicates_page(self, client_with_reupload_group):
        c, _, _ = client_with_reupload_group
        resp = c.get("/duplicates")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Re-upload duplicate" in html
        assert "48910000" in html
        assert "48900000" in html

    def test_reupload_uncertain_group_appears_in_duplicates_page(
        self, client_with_reupload_uncertain_group
    ):
        c, _, _ = client_with_reupload_uncertain_group
        resp = c.get("/duplicates")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Possible re-upload" in html
        assert "Mark reviewed" in html

    def test_reupload_notes_fields_rendered(self, client_with_reupload_group):
        c, _, _ = client_with_reupload_group
        resp = c.get("/duplicates")
        html = resp.data.decode()
        assert "14 days" in html
        assert "Yes" in html  # filename_match=True → "Yes"
        assert "0.85" in html

    def test_reupload_match_key_no_crash(self, client_with_reupload_group):
        """match_key 'reupload:{id1}:{id2}' must not cause a 500."""
        c, _, _ = client_with_reupload_group
        resp = c.get("/duplicates")
        assert resp.status_code == 200

    def test_reupload_null_notes_no_crash(self, client_with_reupload_uncertain_group):
        """NULL notes must not cause a 500."""
        c, _, _ = client_with_reupload_uncertain_group
        resp = c.get("/duplicates")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Thumb route — live derivative fallback
# ---------------------------------------------------------------------------


def _make_thumb_test_db(tmp_path, uuid):
    """Helper: create an isolated DB with one Photos-only record."""
    test_db = Database(Path(tmp_path) / "thumb_test.db")
    test_db.upsert_photo(
        {
            "uuid": uuid,
            "original_filename": "IMG_0001.JPG",
            "privacy_state": "candidate_public",
            "apple_persons": [],
            "apple_labels": [],
        }
    )
    photo_id = test_db.conn.execute("SELECT id FROM photos WHERE uuid = ?", (uuid,)).fetchone()[
        "id"
    ]
    return test_db, photo_id


def test_thumb_live_fallback_writes_thumbnail_path(tmp_path):
    """
    Photo with uuid but no thumbnail_path: if a derivative file exists in the
    Photos library, /thumb/<id> serves it and writes thumbnail_path to the DB.
    """
    import reviewer.app as _app

    uuid = "FFFF1234-0000-0000-0000-000000000000"
    shard = "f"

    # Create a minimal JPEG derivative on disk
    deriv_dir = tmp_path / "resources" / "derivatives" / "masters" / shard
    deriv_dir.mkdir(parents=True)
    deriv = deriv_dir / f"{uuid}_4_5005_c.jpeg"
    # Minimal valid JPEG bytes (SOI marker + EOI marker)
    deriv.write_bytes(b"\xff\xd8\xff\xd9")

    test_db, photo_id = _make_thumb_test_db(tmp_path, uuid)

    old_db = _app._db
    old_config = _app._config.copy()
    _app._db = test_db
    _app._config = {"photos_library": {"path": str(tmp_path)}}
    _app.app.config["TESTING"] = True
    _app.app.config["SECRET_KEY"] = "test-secret"

    try:
        with _app.app.test_client() as c:
            resp = c.get(f"/thumb/{photo_id}")
        assert resp.status_code == 200
        assert resp.content_type == "image/jpeg"
        # thumbnail_path written back to DB
        row = test_db.conn.execute(
            "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        assert row["thumbnail_path"] == str(deriv)
    finally:
        _app._db = old_db
        _app._config = old_config


def test_thumb_live_fallback_writes_sentinel_on_miss(tmp_path):
    """
    Photo with uuid but no thumbnail_path and no derivative on disk:
    /thumb/<id> writes the '__none__' sentinel so future requests skip
    the filesystem probe entirely.
    """
    import reviewer.app as _app

    uuid = "EEEE1234-0000-0000-0000-000000000000"

    # No derivative created on disk — tmp_path is an empty Photos library.
    test_db, photo_id = _make_thumb_test_db(tmp_path, uuid)

    old_db = _app._db
    old_config = _app._config.copy()
    _app._db = test_db
    _app._config = {"photos_library": {"path": str(tmp_path)}}
    _app.app.config["TESTING"] = True
    _app.app.config["SECRET_KEY"] = "test-secret"

    try:
        with _app.app.test_client() as c:
            resp = c.get(f"/thumb/{photo_id}")
        # Falls through to placeholder SVG (no derivative, no Flickr metadata)
        assert resp.status_code == 200
        # Sentinel written to DB so future requests skip probing
        row = test_db.conn.execute(
            "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        assert row["thumbnail_path"] == "__none__"
    finally:
        _app._db = old_db
        _app._config = old_config
