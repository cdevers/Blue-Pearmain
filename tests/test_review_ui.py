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

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import reviewer.app as app_module
from db.db import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """Flask test client wired to a temporary in-memory-equivalent database."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")

        # Seed enough photos to get multiple pages (per_page default = 48)
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
