# Flickr Re-upload Duplicate Detection — Implementation Plan (#17, Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `bp dedup --flickr` to find Flickr-only re-upload orphans paired with linked records (same filename + timestamp, large Flickr ID gap), classify them into `duplicate_groups`, and flag uncertain cases for human review — without touching `privacy_state` or making any Flickr API calls.

**Architecture:** A new `_fetch_reupload_candidates()` function in `poller/deduplicator.py` loads orphans (`uuid IS NULL`) and linked records (`uuid IS NOT NULL AND flickr_id IS NOT NULL`) into memory and matches them via dict lookups (O(n+m), consistent with `link_orphans.py`). Each match is classified by `_classify_reupload_pair()` into `reupload` (auto-group) or `reupload_uncertain` (flag for review). Results are written via the existing `_write_groups()` infrastructure. A `--flickr` flag on the existing `bp dedup` CLI dispatches to this new path; `--limit N` caps writes for safe first runs.

**Tech Stack:** Python stdlib only — `sqlite3`, `json`, `collections.defaultdict`, `datetime`. No new dependencies.

---

## Files

| File | Change |
|------|--------|
| `poller/deduplicator.py` | Add `import json`, `timedelta`, `defaultdict`; add 2 constants; add `_normalise_to_utc_second()`, `_reupload_match_key()`, `_classify_reupload_pair()`, `_fetch_reupload_candidates()`, `_print_reupload_report()`; extend `main()` with `--flickr` / `--limit` flags |
| `tests/test_deduplicator.py` | Add `TestNormaliseUtcSecond`, `TestReuploadMatchKey`, `TestClassifyReuploadPair`, `TestFetchReuploadCandidates` |

---

## Task 1: Imports, constants, and timestamp/key helpers

**Files:**
- Modify: `poller/deduplicator.py` (import block ~lines 35–44; constants block ~lines 48–54)
- Modify: `tests/test_deduplicator.py` (append new test classes)

- [ ] **Step 1: Write failing tests for `_normalise_to_utc_second` and `_reupload_match_key`**

Append to `tests/test_deduplicator.py`:

```python
class TestNormaliseUtcSecond(unittest.TestCase):
    def test_iso_with_negative_offset_converts_to_utc(self):
        from poller.deduplicator import _normalise_to_utc_second
        # 14:12:43 at -04:00 is 18:12:43 UTC
        self.assertEqual(
            _normalise_to_utc_second("2024-09-28T14:12:43.000000-04:00"),
            "2024-09-28 18:12:43",
        )

    def test_naive_string_treated_as_utc(self):
        from poller.deduplicator import _normalise_to_utc_second
        self.assertEqual(
            _normalise_to_utc_second("2024-09-28 14:12:43"),
            "2024-09-28 14:12:43",
        )

    def test_truncation_not_rounding(self):
        from poller.deduplicator import _normalise_to_utc_second
        # .999999 should truncate to :43, not round to :44
        self.assertEqual(
            _normalise_to_utc_second("2024-09-28T14:12:43.999999+00:00"),
            "2024-09-28 14:12:43",
        )

    def test_invalid_returns_none(self):
        from poller.deduplicator import _normalise_to_utc_second
        self.assertIsNone(_normalise_to_utc_second("not-a-date"))

    def test_empty_returns_none(self):
        from poller.deduplicator import _normalise_to_utc_second
        self.assertIsNone(_normalise_to_utc_second(""))


class TestReuploadMatchKey(unittest.TestCase):
    def test_smaller_id_first(self):
        from poller.deduplicator import _reupload_match_key
        self.assertEqual(_reupload_match_key("54000", "48000"), "reupload:48000:54000")

    def test_already_in_order(self):
        from poller.deduplicator import _reupload_match_key
        self.assertEqual(_reupload_match_key("48000", "54000"), "reupload:48000:54000")

    def test_commutative(self):
        from poller.deduplicator import _reupload_match_key
        self.assertEqual(
            _reupload_match_key("54000", "48000"),
            _reupload_match_key("48000", "54000"),
        )
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_deduplicator.py::TestNormaliseUtcSecond tests/test_deduplicator.py::TestReuploadMatchKey -v
```

Expected: `ImportError` — `_normalise_to_utc_second` and `_reupload_match_key` don't exist yet.

- [ ] **Step 3: Add imports and constants to `poller/deduplicator.py`**

Replace the import block (lines 35–44) with:

```python
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
```

After the existing constants (after `NOT_DUPLICATE_PIXEL_RATIO = 1.1`), add:

```python
# Re-upload detection: Flickr IDs this far apart indicate separate upload sessions
CROSS_SESSION_THRESHOLD = 100_000

# Re-upload detection: orphan must exceed linked pixel count by this ratio to displace it as keeper
REUPLOAD_KEEPER_PIXEL_RATIO = 1.5
```

- [ ] **Step 4: Add `_normalise_to_utc_second()` and `_reupload_match_key()` to `poller/deduplicator.py`**

Add after `_parse_dt()` (keep helpers grouped together):

```python
def _normalise_to_utc_second(s: str) -> str | None:
    """Parse date_taken, convert to UTC, truncate to whole second.

    Returns 'YYYY-MM-DD HH:MM:SS' in UTC, or None on parse failure.
    Uses truncation (not rounding) to match normalise_dt() in scanner.py.
    Both sides of the reupload join must use identical normalisation.
    """
    dt = _parse_dt(s)
    if dt is None:
        return None
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%d %H:%M:%S")


def _reupload_match_key(flickr_id_a: str, flickr_id_b: str) -> str:
    """Return canonical match key with smaller Flickr ID first.

    Ordering is independent of argument order so re-runs produce identical keys
    regardless of which record was discovered first.
    """
    a, b = int(flickr_id_a), int(flickr_id_b)
    lo, hi = min(a, b), max(a, b)
    return f"reupload:{lo}:{hi}"
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_deduplicator.py::TestNormaliseUtcSecond tests/test_deduplicator.py::TestReuploadMatchKey -v
```

Expected: 8 passed.

- [ ] **Step 6: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: 639 passed (same as before).

- [ ] **Step 7: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "feat: add CROSS_SESSION_THRESHOLD, _normalise_to_utc_second, _reupload_match_key (#17)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: `_classify_reupload_pair()`

**Files:**
- Modify: `poller/deduplicator.py` (add after `_classify_group()`)
- Modify: `tests/test_deduplicator.py` (append `TestClassifyReuploadPair`)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_deduplicator.py`:

```python
class TestClassifyReuploadPair(unittest.TestCase):
    """Tests for _classify_reupload_pair().

    linked  = record with both uuid and flickr_id (Photos-linked, possibly low-res)
    orphan  = Flickr-only record with no uuid (candidate_public, possibly re-upload)

    Flickr IDs: linked=48922000000, orphan=54060000000 → gap=5138000000 >> CROSS_SESSION_THRESHOLD
    """

    def _linked(self, **kwargs):
        return make_photo(
            id=1, flickr_id="48922000000", uuid="AAAA-1111",
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            **kwargs,
        )

    def _orphan(self, **kwargs):
        return make_photo(
            id=2, flickr_id="54060000000", uuid=None,
            original_filename="DSC_0042.JPG",
            date_taken="2022-08-14T10:23:11+00:00",
            privacy_state="candidate_public",
            **kwargs,
        )

    def test_filename_match_large_gap_is_reupload(self):
        from poller.deduplicator import _classify_reupload_pair
        group = _classify_reupload_pair(self._linked(), self._orphan(),
                                        filename_match=True,
                                        linked_match_count=1, orphan_match_count=1)
        self.assertEqual(group.group_type, "reupload")

    def test_small_gap_is_uncertain(self):
        from poller.deduplicator import _classify_reupload_pair
        # gap = 50, well below CROSS_SESSION_THRESHOLD=100_000
        orphan = self._orphan(flickr_id="48922000050")
        group = _classify_reupload_pair(self._linked(), orphan,
                                        filename_match=True,
                                        linked_match_count=1, orphan_match_count=1)
        self.assertEqual(group.group_type, "reupload_uncertain")

    def test_timestamp_only_fallback_always_uncertain(self):
        from poller.deduplicator import _classify_reupload_pair
        group = _classify_reupload_pair(self._linked(), self._orphan(),
                                        filename_match=False,
                                        linked_match_count=1, orphan_match_count=1)
        self.assertEqual(group.group_type, "reupload_uncertain")

    def test_multiple_linked_candidates_forces_uncertain(self):
        from poller.deduplicator import _classify_reupload_pair
        group = _classify_reupload_pair(self._linked(), self._orphan(),
                                        filename_match=True,
                                        linked_match_count=2, orphan_match_count=1)
        self.assertEqual(group.group_type, "reupload_uncertain")

    def test_multiple_orphan_candidates_forces_uncertain(self):
        from poller.deduplicator import _classify_reupload_pair
        group = _classify_reupload_pair(self._linked(), self._orphan(),
                                        filename_match=True,
                                        linked_match_count=1, orphan_match_count=2)
        self.assertEqual(group.group_type, "reupload_uncertain")

    def test_orphan_dramatically_larger_wins_keeper(self):
        from poller.deduplicator import _classify_reupload_pair
        # orphan 6000×4000 (24M px) vs linked 1620×1080 (1.75M px) → ratio ≈ 13.7×
        linked = self._linked(width=1620, height=1080)
        orphan = self._orphan(width=6000, height=4000)
        group = _classify_reupload_pair(linked, orphan,
                                        filename_match=True,
                                        linked_match_count=1, orphan_match_count=1)
        self.assertEqual(group.group_type, "reupload")
        self.assertIs(group.keeper, orphan)
        self.assertEqual(group.discards, [linked])

    def test_similar_sizes_linked_wins_and_group_is_uncertain(self):
        from poller.deduplicator import _classify_reupload_pair
        # ratio = 1050²/1000² ≈ 1.1, below REUPLOAD_KEEPER_PIXEL_RATIO=1.5
        linked = self._linked(width=1000, height=1000)
        orphan = self._orphan(width=1050, height=1050)
        group = _classify_reupload_pair(linked, orphan,
                                        filename_match=True,
                                        linked_match_count=1, orphan_match_count=1)
        self.assertEqual(group.group_type, "reupload_uncertain")
        self.assertIs(group.keeper, linked)

    def test_only_orphan_has_dims_linked_still_wins(self):
        from poller.deduplicator import _classify_reupload_pair
        linked = self._linked(width=None, height=None)
        orphan = self._orphan(width=6000, height=4000)
        group = _classify_reupload_pair(linked, orphan,
                                        filename_match=True,
                                        linked_match_count=1, orphan_match_count=1)
        self.assertEqual(group.group_type, "reupload_uncertain")
        self.assertIs(group.keeper, linked)

    def test_no_dims_keeper_assumed_true_in_notes(self):
        from poller.deduplicator import _classify_reupload_pair
        # make_photo() defaults width=None, height=None
        group = _classify_reupload_pair(self._linked(), self._orphan(),
                                        filename_match=True,
                                        linked_match_count=1, orphan_match_count=1)
        data = json.loads(group.notes)
        self.assertTrue(data["keeper_assumed"])

    def test_zero_width_treated_as_no_dims(self):
        from poller.deduplicator import _classify_reupload_pair
        # width=0 → pixels property returns None → treated as no dimensions
        linked = self._linked(width=0, height=0)
        orphan = self._orphan(width=6000, height=4000)
        group = _classify_reupload_pair(linked, orphan,
                                        filename_match=True,
                                        linked_match_count=1, orphan_match_count=1)
        self.assertIs(group.keeper, linked)
        self.assertEqual(group.group_type, "reupload_uncertain")

    def test_match_key_smaller_flickr_id_first(self):
        from poller.deduplicator import _classify_reupload_pair
        group = _classify_reupload_pair(self._linked(), self._orphan(),
                                        filename_match=True,
                                        linked_match_count=1, orphan_match_count=1)
        self.assertEqual(group.match_key, "reupload:48922000000:54060000000")

    def test_evidence_blob_contains_required_fields(self):
        from poller.deduplicator import _classify_reupload_pair
        linked = self._linked(width=1620, height=1080)
        orphan = self._orphan(width=6000, height=4000)
        group = _classify_reupload_pair(linked, orphan,
                                        filename_match=True,
                                        linked_match_count=1, orphan_match_count=1)
        data = json.loads(group.notes)
        for key in ("keeper_flickr_id", "discard_flickr_id", "filename_match",
                    "timestamp_delta_s", "upload_session_gap", "dimension_ratio",
                    "linked_match_count", "orphan_match_count", "keeper_assumed", "summary"):
            self.assertIn(key, data, f"missing key: {key}")
        self.assertEqual(data["keeper_flickr_id"], orphan.flickr_id)
        self.assertEqual(data["discard_flickr_id"], linked.flickr_id)
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_deduplicator.py::TestClassifyReuploadPair -v
```

Expected: `ImportError` — `_classify_reupload_pair` doesn't exist yet.

- [ ] **Step 3: Add `_classify_reupload_pair()` to `poller/deduplicator.py`**

Add after `_classify_group()`:

```python
def _classify_reupload_pair(
    linked: PhotoRow,
    orphan: PhotoRow,
    filename_match: bool,
    linked_match_count: int,
    orphan_match_count: int,
) -> DuplicateGroup:
    """Classify one linked+orphan pair as 'reupload' or 'reupload_uncertain'.

    linked  — record with both uuid and flickr_id (Photos-linked)
    orphan  — Flickr-only record (uuid IS NULL, candidate_public)
    filename_match  — True when original_filename matched; False = timestamp-only fallback
    linked_match_count  — how many linked records matched this orphan's key (>1 = Nikon collision)
    orphan_match_count  — how many orphans matched this linked record's key (>1 = Nikon collision)
    """
    upload_session_gap = abs(int(linked.flickr_id) - int(orphan.flickr_id))

    # Any of these conditions forces reupload_uncertain regardless of resolution
    force_uncertain = (
        not filename_match
        or upload_session_gap <= CROSS_SESSION_THRESHOLD
        or linked_match_count > 1
        or orphan_match_count > 1
    )

    # Keeper determination — resolution-first, with conservative bias toward linked record.
    # PhotoRow.pixels returns None when width/height is 0 or None, so the zero-dimension
    # guard is implicit.
    linked_px = linked.pixels
    orphan_px = orphan.pixels
    keeper_assumed = False

    if linked_px and orphan_px:
        ratio = max(linked_px, orphan_px) / min(linked_px, orphan_px)
        if ratio >= REUPLOAD_KEEPER_PIXEL_RATIO:
            keeper = linked if linked_px >= orphan_px else orphan
        else:
            keeper = linked
            force_uncertain = True
        dimension_ratio: float | None = round(ratio, 2)
    elif linked_px:
        # Only linked has valid dimensions — linked wins tentatively
        keeper = linked
        force_uncertain = True
        dimension_ratio = None
    else:
        # Orphan-only dims, or neither — linked wins conservatively.
        # Never auto-promote an orphan solely from its own unilateral dimension data.
        keeper = linked
        keeper_assumed = True
        force_uncertain = True
        dimension_ratio = None

    discard = orphan if keeper is linked else linked

    # Timestamp delta for the evidence blob
    linked_dt = _parse_dt(linked.date_taken)
    orphan_dt = _parse_dt(orphan.date_taken)
    timestamp_delta_s: int | None = None
    if linked_dt and orphan_dt:
        timestamp_delta_s = int(abs((linked_dt - orphan_dt).total_seconds()))

    notes = json.dumps({
        "keeper_flickr_id": keeper.flickr_id,
        "discard_flickr_id": discard.flickr_id,
        "filename_match": filename_match,
        "timestamp_delta_s": timestamp_delta_s,
        "upload_session_gap": upload_session_gap,
        "dimension_ratio": dimension_ratio,
        "linked_match_count": linked_match_count,
        "orphan_match_count": orphan_match_count,
        "keeper_assumed": keeper_assumed,
        "summary": (
            f"{linked.original_filename or '(no filename)'} | "
            f"{linked.date_taken} | "
            f"linked flickr_id={linked.flickr_id} → "
            f"orphan flickr_id={orphan.flickr_id} | "
            f"gap={upload_session_gap}"
            + (f" | ratio={dimension_ratio}×" if dimension_ratio else "")
        ),
    })

    return DuplicateGroup(
        match_key=_reupload_match_key(linked.flickr_id, orphan.flickr_id),
        group_type="reupload" if not force_uncertain else "reupload_uncertain",
        photos=[keeper, discard],
        keeper=keeper,
        discards=[discard],
        review=[],
        notes=notes,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_deduplicator.py::TestClassifyReuploadPair -v
```

Expected: 12 passed.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "feat: add _classify_reupload_pair for re-upload duplicate detection (#17)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: `_fetch_reupload_candidates()`

**Files:**
- Modify: `poller/deduplicator.py` (add after `_fetch_duplicate_candidates()`)
- Modify: `tests/test_deduplicator.py` (append `TestFetchReuploadCandidates`)

- [ ] **Step 1: Write failing tests**

These tests use an in-memory SQLite DB. Append to `tests/test_deduplicator.py`:

```python
import sqlite3 as _sqlite3


def _make_db() -> _sqlite3.Connection:
    """Return an in-memory DB with the minimal photos schema for reupload tests."""
    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    conn.execute("""
        CREATE TABLE photos (
            id INTEGER PRIMARY KEY,
            flickr_id TEXT,
            uuid TEXT,
            original_filename TEXT,
            date_taken TEXT,
            date_added_photos TEXT,
            date_uploaded_flickr TEXT,
            fingerprint TEXT,
            width INTEGER,
            height INTEGER,
            privacy_state TEXT DEFAULT 'candidate_public',
            duplicate_group_id INTEGER
        )
    """)
    return conn


def _insert(conn, **kwargs):
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" for _ in kwargs)
    conn.execute(f"INSERT INTO photos ({cols}) VALUES ({placeholders})", list(kwargs.values()))


class TestFetchReuploadCandidates(unittest.TestCase):

    def test_matched_pair_produces_one_group(self):
        from poller.deduplicator import _fetch_reupload_candidates
        conn = _make_db()
        _insert(conn, id=1, flickr_id="48922000000", uuid="AAAA",
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        _insert(conn, id=2, flickr_id="54060000000", uuid=None,
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="candidate_public")
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(conflicts), 0)
        self.assertEqual(groups[0].group_type, "reupload")

    def test_already_grouped_orphan_goes_to_conflicts(self):
        from poller.deduplicator import _fetch_reupload_candidates
        conn = _make_db()
        _insert(conn, id=1, flickr_id="48922000000", uuid="AAAA",
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        _insert(conn, id=2, flickr_id="54060000000", uuid=None,
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="candidate_public",
                duplicate_group_id=99)
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 0)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["flickr_id"], "54060000000")
        self.assertEqual(conflicts[0]["side"], "orphan")

    def test_null_filename_fallback_produces_uncertain(self):
        from poller.deduplicator import _fetch_reupload_candidates
        conn = _make_db()
        _insert(conn, id=1, flickr_id="48922000000", uuid="AAAA",
                original_filename=None,
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        _insert(conn, id=2, flickr_id="54060000000", uuid=None,
                original_filename=None,
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="candidate_public")
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].group_type, "reupload_uncertain")

    def test_no_timestamp_overlap_produces_no_groups(self):
        from poller.deduplicator import _fetch_reupload_candidates
        conn = _make_db()
        _insert(conn, id=1, flickr_id="48922000000", uuid="AAAA",
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        _insert(conn, id=2, flickr_id="54060000000", uuid=None,
                original_filename="DSC_0042.JPG",
                date_taken="2023-01-01T00:00:00+00:00",  # completely different date
                privacy_state="candidate_public")
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 0)

    def test_two_second_window_matches(self):
        from poller.deduplicator import _fetch_reupload_candidates
        conn = _make_db()
        _insert(conn, id=1, flickr_id="48922000000", uuid="AAAA",
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        _insert(conn, id=2, flickr_id="54060000000", uuid=None,
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:13+00:00",  # 2 seconds later
                privacy_state="candidate_public")
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 1)

    def test_three_second_gap_does_not_match(self):
        from poller.deduplicator import _fetch_reupload_candidates
        conn = _make_db()
        _insert(conn, id=1, flickr_id="48922000000", uuid="AAAA",
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        _insert(conn, id=2, flickr_id="54060000000", uuid=None,
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:14+00:00",  # 3 seconds later — outside window
                privacy_state="candidate_public")
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 0)
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_deduplicator.py::TestFetchReuploadCandidates -v
```

Expected: `ImportError` — `_fetch_reupload_candidates` doesn't exist yet.

- [ ] **Step 3: Add `_fetch_reupload_candidates()` to `poller/deduplicator.py`**

Add after `_fetch_duplicate_candidates()`:

```python
def _fetch_reupload_candidates(
    conn: sqlite3.Connection,
) -> tuple[list[DuplicateGroup], list[dict]]:
    """Find Flickr-only re-upload candidates matched to linked records.

    Matches on original_filename (exact) + date_taken within ±2 seconds.
    Falls back to timestamp-only when filename is NULL on either side.

    Returns:
        groups     — DuplicateGroup list (reupload or reupload_uncertain)
        conflicts  — dicts for pairs skipped because a record was already grouped
    """
    # Load orphans: Flickr-only candidate_public records
    orphan_rows = conn.execute("""
        SELECT id, flickr_id, uuid, original_filename, date_taken,
               date_added_photos, date_uploaded_flickr, fingerprint,
               width, height, privacy_state, duplicate_group_id
        FROM photos
        WHERE uuid IS NULL
          AND flickr_id IS NOT NULL
          AND privacy_state = 'candidate_public'
    """).fetchall()

    orphans = [
        PhotoRow(
            id=r["id"], flickr_id=r["flickr_id"], uuid=r["uuid"],
            original_filename=r["original_filename"], date_taken=r["date_taken"],
            date_added_photos=r["date_added_photos"],
            date_uploaded_flickr=r["date_uploaded_flickr"],
            fingerprint=r["fingerprint"],
            width=r["width"], height=r["height"],
            privacy_state=r["privacy_state"],
            duplicate_group_id=r["duplicate_group_id"],
        )
        for r in orphan_rows
    ]

    # Load linked records: have both uuid and flickr_id
    linked_rows = conn.execute("""
        SELECT id, flickr_id, uuid, original_filename, date_taken,
               date_added_photos, date_uploaded_flickr, fingerprint,
               width, height, privacy_state, duplicate_group_id
        FROM photos
        WHERE uuid IS NOT NULL
          AND flickr_id IS NOT NULL
    """).fetchall()

    linked_records = [
        PhotoRow(
            id=r["id"], flickr_id=r["flickr_id"], uuid=r["uuid"],
            original_filename=r["original_filename"], date_taken=r["date_taken"],
            date_added_photos=r["date_added_photos"],
            date_uploaded_flickr=r["date_uploaded_flickr"],
            fingerprint=r["fingerprint"],
            width=r["width"], height=r["height"],
            privacy_state=r["privacy_state"],
            duplicate_group_id=r["duplicate_group_id"],
        )
        for r in linked_rows
    ]

    log.info("Reupload scan: %d orphans, %d linked records", len(orphans), len(linked_records))

    # Build O(1) lookup indexes keyed by UTC second
    linked_by_filename_ts: dict[tuple[str, str], list[PhotoRow]] = defaultdict(list)
    linked_by_ts: dict[str, list[PhotoRow]] = defaultdict(list)

    for p in linked_records:
        ts = _normalise_to_utc_second(p.date_taken)
        if not ts:
            continue
        if p.original_filename:
            linked_by_filename_ts[(p.original_filename, ts)].append(p)
        linked_by_ts[ts].append(p)

    # Pass 1: match each orphan to candidates
    # Each entry: (orphan, ungrouped_candidates, filename_match)
    raw_matches: list[tuple[PhotoRow, list[PhotoRow], bool]] = []
    conflicts: list[dict] = []

    for orphan in orphans:
        ts = _normalise_to_utc_second(orphan.date_taken)
        if not ts:
            continue

        ts_dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)

        # Try filename+timestamp match first (±2 seconds)
        fn_candidates: list[PhotoRow] = []
        seen_ids: set[int] = set()
        if orphan.original_filename:
            for delta in range(-2, 3):
                shifted = (ts_dt + timedelta(seconds=delta)).strftime("%Y-%m-%d %H:%M:%S")
                for p in linked_by_filename_ts.get((orphan.original_filename, shifted), []):
                    if p.id not in seen_ids:
                        fn_candidates.append(p)
                        seen_ids.add(p.id)

        if fn_candidates:
            candidates, filename_match = fn_candidates, True
        else:
            # Timestamp-only fallback — will force reupload_uncertain in classification
            ts_candidates: list[PhotoRow] = []
            seen_ids = set()
            for delta in range(-2, 3):
                shifted = (ts_dt + timedelta(seconds=delta)).strftime("%Y-%m-%d %H:%M:%S")
                for p in linked_by_ts.get(shifted, []):
                    if p.id not in seen_ids:
                        ts_candidates.append(p)
                        seen_ids.add(p.id)
            candidates, filename_match = ts_candidates, False

        if not candidates:
            continue

        # Skip orphans already in a group
        if orphan.duplicate_group_id:
            conflicts.append({"flickr_id": orphan.flickr_id,
                               "existing_group_id": orphan.duplicate_group_id,
                               "side": "orphan"})
            continue

        # Filter already-grouped linked records; surface them as conflicts
        ungrouped: list[PhotoRow] = []
        for p in candidates:
            if p.duplicate_group_id:
                conflicts.append({"flickr_id": p.flickr_id,
                                   "existing_group_id": p.duplicate_group_id,
                                   "side": "linked"})
            else:
                ungrouped.append(p)

        if not ungrouped:
            continue

        raw_matches.append((orphan, ungrouped, filename_match))

    # Pass 2: count how many orphans each linked record was matched to
    orphan_count_by_linked_id: dict[int, int] = defaultdict(int)
    for _, candidates, _ in raw_matches:
        for linked in candidates:
            orphan_count_by_linked_id[linked.id] += 1

    # Pass 3: classify — one group per orphan, picking the best linked candidate
    groups: list[DuplicateGroup] = []
    used_ids: set[int] = set()  # prevent a record appearing in multiple groups

    for orphan, candidates, filename_match in raw_matches:
        if orphan.id in used_ids:
            continue

        # Best candidate = largest upload_session_gap (strongest evidence of different session)
        best_linked = max(
            candidates,
            key=lambda p: abs(int(orphan.flickr_id) - int(p.flickr_id)),
        )
        if best_linked.id in used_ids:
            continue

        group = _classify_reupload_pair(
            linked=best_linked,
            orphan=orphan,
            filename_match=filename_match,
            linked_match_count=len(candidates),
            orphan_match_count=orphan_count_by_linked_id[best_linked.id],
        )
        groups.append(group)
        used_ids.add(orphan.id)
        used_ids.add(best_linked.id)

    return groups, conflicts
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_deduplicator.py::TestFetchReuploadCandidates -v
```

Expected: 6 passed.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "feat: add _fetch_reupload_candidates for re-upload detection (#17)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: `_print_reupload_report()` and `main()` extension

**Files:**
- Modify: `poller/deduplicator.py` (add `_print_reupload_report()`; extend `main()`)

No new tests for the report function — it is display-only. The `main()` extension is
thin wiring; correctness is covered by the unit tests in Tasks 1–3.

- [ ] **Step 1: Add `_print_reupload_report()` to `poller/deduplicator.py`**

Add after `_print_report()`:

```python
def _print_reupload_report(
    groups: list[DuplicateGroup],
    conflicts: list[dict],
    verbose: bool = False,
) -> None:
    total = len(groups)
    if total == 0 and not conflicts:
        print("No re-upload pairs found.")
        return

    print(f"\nReupload pairs found: {total}")

    by_type: dict[str, list[DuplicateGroup]] = {}
    for g in groups:
        by_type.setdefault(g.group_type, []).append(g)

    for gtype, glist in sorted(by_type.items()):
        pct = 100.0 * len(glist) / total if total else 0.0
        label = (
            "auto-grouped" if gtype == "reupload"
            else "flagged — small gap, timestamp-only, or collision"
        )
        print(f"  {gtype:<22} {len(glist):>5} pairs  {pct:5.1f}%   ({label})")

    uncertain = by_type.get("reupload_uncertain", [])
    if uncertain:
        show = uncertain if verbose else uncertain[:10]
        print(f"\n── REUPLOAD_UNCERTAIN ({len(uncertain)} pairs) " + "─" * 40)
        for g in show:
            try:
                data = json.loads(g.notes)
                print(f"  {data['summary']}")
                if g.keeper:
                    print(f"    keeper:  flickr_id={g.keeper.flickr_id}  uuid={g.keeper.uuid}")
                if g.discards:
                    gap = data.get("upload_session_gap", "?")
                    print(f"    discard: flickr_id={g.discards[0].flickr_id}"
                          f"  upload_session_gap={gap}")
            except (json.JSONDecodeError, KeyError):
                print(f"  {g.match_key}  (notes unparseable)")
        if not verbose and len(uncertain) > 10:
            print(f"  ... and {len(uncertain) - 10} more (use --verbose to see all)")

    if conflicts:
        print(f"\n── CONFLICTS ({len(conflicts)} records already in a group) " + "─" * 30)
        for c in conflicts[:20]:
            print(f"  flickr_id={c['flickr_id']}"
                  f"  already in duplicate_group_id={c['existing_group_id']}"
                  f"  ({c['side']}) — skipped")
        if len(conflicts) > 20:
            print(f"  ... and {len(conflicts) - 20} more")

    print()
```

- [ ] **Step 2: Add `--flickr` and `--limit` flags to `main()`**

In the `main()` function, in the `argparse` block, add after the `--confirm` argument:

```python
parser.add_argument("--flickr", action="store_true",
                    help="Detect Flickr re-upload duplicates (orphan paired with linked record)")
parser.add_argument("--limit", type=int, default=None,
                    help="Maximum pairs to write (recommended for first live runs)")
```

- [ ] **Step 3: Add `--flickr` dispatch to `main()`**

In `main()`, after the `conn` is opened and before `log.info("Scanning for duplicates …")`,
add the `--flickr` early-return path:

```python
    if args.flickr:
        log.info("Scanning for re-upload duplicates in %s …", db_path)
        groups, conflicts = _fetch_reupload_candidates(conn)
        _print_reupload_report(groups, conflicts, verbose=args.verbose)

        if args.dry_run:
            print("Dry run — no changes written. Use --write to persist.")
            conn.close()
            return

        if args.limit is not None:
            groups = groups[: args.limit]
            log.info("--limit %d: writing first %d pairs", args.limit, len(groups))

        log.info("Writing %d reupload group(s) to DB …", len(groups))
        conn.execute("BEGIN")
        try:
            counts = _write_groups(conn, groups)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        print(f"Written: {counts}")
        conn.close()
        return
```

- [ ] **Step 4: Verify the CLI works end-to-end in dry-run mode**

```bash
python poller/deduplicator.py --config config/config.yml --flickr --dry-run
```

Expected: prints "Reupload pairs found: N" (N may be 0 if the live DB has no matches
yet — that's fine). No errors.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add poller/deduplicator.py
git commit -m "feat: add --flickr flag to bp dedup with report and --limit (#17)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: README, docs, and GitHub issue

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Get final test count**

```bash
python -m pytest tests/ -q 2>&1 | tail -1
```

Note the number.

- [ ] **Step 2: Update README**

Find the test count in `README.md` (appears in the Components table and/or Tests section)
and update it to the new number.

- [ ] **Step 3: Run full suite one final time**

```bash
python -m pytest tests/ -q
```

Expected: all passed; count matches README.

- [ ] **Step 4: Final commit**

```bash
git add README.md
git commit -m "Docs: update README test count for #17 Phase 1

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 5: Comment on GitHub issue #17**

```bash
gh issue comment 17 --repo cdevers/Blue-Pearmain --body "Phase 1 implementation plan written.

Design spec: \`docs/superpowers/specs/2026-05-13-reupload-dedup-17-design.md\`
Implementation plan: \`docs/superpowers/plans/2026-05-13-reupload-dedup-17.md\`

Phase 1 scope: detection + DB grouping only (\`bp dedup --flickr\`). No Flickr API calls, no privacy_state changes.

Phases 2 (privacy enforcement), 3 (metadata sync), and 4 (UI cross-linking) are deferred to separate issues."
```
