"""
tests/test_template_head.py — verify rendered HTML <head> structure is valid

Catches the class of bug where tags like <link> or nested <style> are
inadvertently placed inside a <style> block (invalid HTML), causing
stylesheets to silently fail to load.

Run from repo root:
    python -m pytest tests/test_template_head.py -v
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

import reviewer.app as app_module
from db.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _style_block_contents(html: str) -> list[str]:
    """Return the inner text of every <style> block in the document."""
    return re.findall(r"<style[^>]*>(.*?)</style>", html, re.DOTALL | re.IGNORECASE)


def _head_section(html: str) -> str:
    """Return everything between <head> and </head> (inclusive)."""
    m = re.search(r"<head\b[^>]*>.*?</head>", html, re.DOTALL | re.IGNORECASE)
    return m.group(0) if m else ""


def _link_hrefs_in_head(html: str) -> list[str]:
    """Return href values of all <link> tags in <head>."""
    head = _head_section(html)
    return re.findall(r'<link[^>]+href=["\']([^"\']+)["\']', head, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_photo(i: int, **kwargs) -> dict:
    base: dict = {
        "uuid": f"head-u{i}",
        "original_filename": f"IMG_{i:04d}.JPG",
        "privacy_state": "needs_review",
        "apple_persons": [],
        "apple_labels": [],
        "apple_unknown_faces": 0,
        "apple_named_faces": 0,
    }
    base.update(kwargs)
    return base


@pytest.fixture(scope="module")
def client():
    """Minimal Flask test client — one geotagged photo so /map renders."""
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Database(Path(tmp) / "test.db")
        test_db.upsert_photo(
            _base_photo(1, latitude=48.8566, longitude=2.3522, date_taken="2023-10-15T12:00:00")
        )
        app_module._db = test_db
        app_module.app.config["TESTING"] = True
        app_module.app.config["SECRET_KEY"] = "test-secret"
        with app_module.app.test_client() as c:
            yield c
        app_module._db = None


# ---------------------------------------------------------------------------
# Pages under test
# ---------------------------------------------------------------------------

PAGES = [
    ("/", "dashboard"),
    ("/map", "map"),
    ("/library", "library"),
    ("/albums", "albums"),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoTagsInsideStyleBlocks:
    """No <link> or nested <style> tags should appear inside a <style> block."""

    @pytest.mark.parametrize("path,name", PAGES)
    def test_no_link_inside_style(self, client, path, name):
        html = client.get(path).data.decode()
        for block in _style_block_contents(html):
            assert "<link" not in block.lower(), (
                f"<link> found inside a <style> block on {name} ({path}). "
                "Use {{% block extra_head %}} for stylesheet links."
            )

    @pytest.mark.parametrize("path,name", PAGES)
    def test_no_nested_style_tag(self, client, path, name):
        html = client.get(path).data.decode()
        for block in _style_block_contents(html):
            assert "<style" not in block.lower(), (
                f"Nested <style> tag found inside a <style> block on {name} ({path}). "
                "Put raw CSS directly in {{% block extra_style %}} without a wrapping <style> tag."
            )


class TestMapLeafletCssInHead:
    """Leaflet stylesheet links must appear in <head>, not buried in CSS."""

    def test_leaflet_css_link_in_head(self, client):
        html = client.get("/map").data.decode()
        hrefs = _link_hrefs_in_head(html)
        leaflet_links = [h for h in hrefs if "leaflet" in h.lower()]
        assert leaflet_links, (
            "No Leaflet CSS <link> found in <head> on /map. "
            "Leaflet CSS must be loaded via <link> in <head> for the map to render correctly."
        )

    def test_leaflet_css_not_in_style_block(self, client):
        html = client.get("/map").data.decode()
        for block in _style_block_contents(html):
            assert "leaflet" not in block.lower(), (
                "Leaflet reference found inside a <style> block on /map — "
                "the <link> tags are not reaching <head>."
            )
