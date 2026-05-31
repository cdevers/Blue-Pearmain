from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "poller"))

from legacy_normalize import (  # noqa: E402
    canonical_rel_path,
    head_hash,
    normalize_json_list,
    normalize_title,
    thumbnail_cache_key,
    thumbnail_path,
)


class TestNormalizeJsonList:
    def test_unique_and_sorted(self):
        assert json.loads(normalize_json_list(["b", "a", "b"])) == ["a", "b"]

    def test_empty_returns_empty_array(self):
        assert normalize_json_list([]) == "[]"
        assert normalize_json_list(None) == "[]"

    def test_reordered_input_is_identical(self):
        assert normalize_json_list(["x", "y"]) == normalize_json_list(["y", "x"])

    def test_strips_blank_entries(self):
        assert json.loads(normalize_json_list(["a", "", "  ", "b"])) == ["a", "b"]


class TestCanonicalRelPath:
    def test_backslashes_become_posix(self):
        assert canonical_rel_path("Masters\\2008\\img.jpg") == "Masters/2008/img.jpg"

    def test_duplicate_slashes_collapsed_after_separator_norm(self):
        assert canonical_rel_path("a\\\\b//c") == "a/b/c"

    def test_leading_dot_slash_stripped(self):
        assert canonical_rel_path("./a/b.jpg") == "a/b.jpg"

    def test_trailing_slash_stripped(self):
        assert canonical_rel_path("a/b/") == "a/b"

    def test_case_preserved(self):
        assert canonical_rel_path("Masters/IMG.JPG") == "Masters/IMG.JPG"

    def test_nfd_normalized_to_nfc(self):
        import unicodedata

        nfd = unicodedata.normalize("NFD", "café/photo.jpg")
        out = canonical_rel_path(nfd)
        assert out == unicodedata.normalize("NFC", "café/photo.jpg")

    def test_none_returns_none(self):
        assert canonical_rel_path(None) is None


class TestNormalizeTitle:
    def test_trim_casefold_nfc(self):
        assert normalize_title("  Hello ") == "hello"

    def test_empty_after_trim_is_none(self):
        assert normalize_title("   ") is None
        assert normalize_title("") is None
        assert normalize_title(None) is None


class TestThumbnailKeyAndPath:
    def test_key_is_stable_and_path_independent(self):
        k1 = thumbnail_cache_key("LIB", "ASSET")
        k2 = thumbnail_cache_key("LIB", "ASSET")
        assert k1 == k2 and len(k1) == 32

    def test_different_identity_different_key(self):
        assert thumbnail_cache_key("LIB", "A") != thumbnail_cache_key("LIB", "B")

    def test_path_built_from_root_at_read_time(self, tmp_path):
        key = thumbnail_cache_key("LIB", "ASSET")
        p = thumbnail_path(tmp_path, "LIB", key)
        assert p == tmp_path / "legacy" / "LIB" / f"{key}.jpg"


class TestHeadHash:
    def test_hashes_first_n_bytes_only(self, tmp_path):
        f = tmp_path / "db.sqlite"
        f.write_bytes(b"A" * 100 + b"B" * 100)
        h_all = head_hash(str(f))
        h_first = head_hash(str(f), n=100)
        assert h_first != h_all
        f.write_bytes(b"A" * 100 + b"C" * 100)
        assert head_hash(str(f), n=100) == h_first
