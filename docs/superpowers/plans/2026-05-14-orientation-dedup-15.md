# Orientation Duplicate Resolution — Implementation Plan (#15)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `bp dedup --flickr` with (1) `--include-approved` to detect orientation duplicates in `approved_public` records, and (2) `--delete-discards --apply` to delete confirmed discard records from Flickr.

**Architecture:** Task 1 adds `FlickrClient.delete_photo()` — a two-line method needed by both the existing `--confirm` path and the new `--delete-discards` path. Task 2 adds an `include_approved` parameter to `_fetch_reupload_candidates()` and a `--include-approved` CLI flag. Task 3 adds `_delete_discards()` — a new function that queries already-written `duplicate_groups` rows and calls `client.delete_photo()` per discard, then wires it to `--delete-discards`/`--apply` CLI flags.

**Tech Stack:** Python stdlib (`sqlite3`, `json`). `flickr.flickr_client.FlickrClient` and `FlickrError`. Depends on #17 Phase 1 (`_fetch_reupload_candidates()`, `--flickr` flag) being implemented first.

**Prerequisite:** #17 Phase 1 must be merged before starting this plan. After #17, `poller/deduplicator.py` has `_fetch_reupload_candidates()`, `_classify_reupload_pair()`, `_print_reupload_report()`, and the `--flickr`/`--limit` CLI flags. `tests/test_deduplicator.py` has `TestFetchReuploadCandidates`, `_make_db()`, and `_insert()`.

---

## Files

| File | Change |
|------|--------|
| `flickr/flickr_client.py` | Add `delete_photo(photo_id: str) -> None` after `rotate()` (~line 338) |
| `poller/deduplicator.py` | (1) Add `include_approved: bool = False` param to `_fetch_reupload_candidates()`; (2) Add `_delete_discards()` function; (3) Add `--include-approved`, `--delete-discards`, `--apply` CLI flags + dispatch in `main()` |
| `tests/test_core.py` | Add `test_delete_photo_calls_api` to `TestFlickrCollectionsClient` |
| `tests/test_deduplicator.py` | Add 3 `--include-approved` tests to `TestFetchReuploadCandidates`; add `_make_dedup_db()` + `_insert_group_and_discard()` helpers + `TestDeleteDiscards` class (7 tests) |

---

## Task 1: `FlickrClient.delete_photo()`

**Files:**
- Modify: `flickr/flickr_client.py` (after `rotate()`, ~line 337)
- Test: `tests/test_core.py` (append to `TestFlickrCollectionsClient`)

- [ ] **Step 1: Write the failing test**

Open `tests/test_core.py` and append to the `TestFlickrCollectionsClient` class (which ends around line 1590 — look for the last `test_` method in that class):

```python
    def test_delete_photo_calls_api(self):
        from unittest.mock import patch
        client = self._make_client()
        with patch.object(client, "_call", return_value={}) as mock_call:
            client.delete_photo("12345678")
        mock_call.assert_called_once_with(
            "flickr.photos.delete",
            {"photo_id": "12345678"},
            http_method="POST",
        )
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_core.py::TestFlickrCollectionsClient::test_delete_photo_calls_api -v
```

Expected: `AttributeError: 'FlickrClient' object has no attribute 'delete_photo'`

- [ ] **Step 3: Add `delete_photo()` to `flickr/flickr_client.py`**

In `flickr/flickr_client.py`, find `rotate()` (currently around line 328). Add immediately after its closing brace, before `get_photosets()`:

```python
    def delete_photo(self, photo_id: str) -> None:
        """Permanently delete a Flickr photo. Raises FlickrError on failure."""
        self._call(
            "flickr.photos.delete",
            {"photo_id": photo_id},
            http_method="POST",
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_core.py::TestFlickrCollectionsClient::test_delete_photo_calls_api -v
```

Expected: PASS

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all passed (one more test than before).

- [ ] **Step 6: Commit**

```bash
git add flickr/flickr_client.py tests/test_core.py
git commit -m "feat: add FlickrClient.delete_photo() (#15)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: `--include-approved` detection extension

**Files:**
- Modify: `poller/deduplicator.py` (`_fetch_reupload_candidates()` signature + query; `main()` CLI flags + validation + pass-through)
- Test: `tests/test_deduplicator.py` (append 3 tests to `TestFetchReuploadCandidates`)

- [ ] **Step 1: Write the failing tests**

Open `tests/test_deduplicator.py` and append these 3 tests inside the `TestFetchReuploadCandidates` class (after the last existing test method in that class):

```python
    def test_include_approved_adds_approved_public(self):
        from poller.deduplicator import _fetch_reupload_candidates
        conn = _make_db()
        # linked record (approved_public, has uuid)
        _insert(conn, id=1, flickr_id="48922000000", uuid="AAAA",
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        # orphan with approved_public (orientation duplicate)
        _insert(conn, id=2, flickr_id="54060000000", uuid=None,
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        groups, conflicts = _fetch_reupload_candidates(conn, include_approved=True)
        self.assertEqual(len(groups), 1)

    def test_include_approved_off_excludes_approved_public(self):
        from poller.deduplicator import _fetch_reupload_candidates
        conn = _make_db()
        _insert(conn, id=1, flickr_id="48922000000", uuid="AAAA",
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        _insert(conn, id=2, flickr_id="54060000000", uuid=None,
                original_filename="DSC_0042.JPG",
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        # Without include_approved, the approved_public orphan is excluded
        groups, conflicts = _fetch_reupload_candidates(conn)
        self.assertEqual(len(groups), 0)

    def test_include_approved_null_filename_classified_uncertain(self):
        from poller.deduplicator import _fetch_reupload_candidates
        conn = _make_db()
        _insert(conn, id=1, flickr_id="48922000000", uuid="AAAA",
                original_filename=None,
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        _insert(conn, id=2, flickr_id="54060000000", uuid=None,
                original_filename=None,
                date_taken="2022-08-14T10:23:11+00:00",
                privacy_state="approved_public")
        groups, conflicts = _fetch_reupload_candidates(conn, include_approved=True)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].group_type, "reupload_uncertain")
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_deduplicator.py::TestFetchReuploadCandidates::test_include_approved_adds_approved_public tests/test_deduplicator.py::TestFetchReuploadCandidates::test_include_approved_off_excludes_approved_public tests/test_deduplicator.py::TestFetchReuploadCandidates::test_include_approved_null_filename_classified_uncertain -v
```

Expected: the first two tests FAIL (approved_public orphan is never returned since the current query hardcodes `candidate_public`). The third test may also fail or error.

- [ ] **Step 3: Add `include_approved` parameter to `_fetch_reupload_candidates()`**

In `poller/deduplicator.py`, find `_fetch_reupload_candidates()`. Change its signature from:

```python
def _fetch_reupload_candidates(
    conn: sqlite3.Connection,
) -> tuple[list[DuplicateGroup], list[dict]]:
```

to:

```python
def _fetch_reupload_candidates(
    conn: sqlite3.Connection,
    include_approved: bool = False,
) -> tuple[list[DuplicateGroup], list[dict]]:
```

Then find the orphan query (it starts with `# Load orphans`). Replace the query block to use a dynamic privacy filter:

```python
    # Load orphans: Flickr-only records needing review
    privacy_clause = (
        "AND privacy_state IN ('candidate_public', 'approved_public')"
        if include_approved
        else "AND privacy_state = 'candidate_public'"
    )
    orphan_rows = conn.execute(f"""
        SELECT id, flickr_id, uuid, original_filename, date_taken,
               date_added_photos, date_uploaded_flickr, fingerprint,
               width, height, privacy_state, duplicate_group_id
        FROM photos
        WHERE uuid IS NULL
          AND flickr_id IS NOT NULL
          {privacy_clause}
    """).fetchall()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_deduplicator.py::TestFetchReuploadCandidates -v
```

Expected: all pass (including the 3 new tests and all existing tests).

- [ ] **Step 5: Add `--include-approved` to `main()` in `poller/deduplicator.py`**

In the `argparse` block of `main()`, add after the `--limit` argument:

```python
parser.add_argument("--include-approved", action="store_true",
                    help="Include approved_public Flickr-only records in detection (catches orientation duplicates)")
parser.add_argument("--delete-discards", action="store_true",
                    help="Act on already-grouped discards: call flickr.photos.delete on approved_public discards")
parser.add_argument("--apply", action="store_true",
                    help="Execute deletions (default is dry-run). Requires --delete-discards.")
```

Then add a validation block early in `main()`, after `args = parser.parse_args()` and before the config load:

```python
    if args.include_approved and not args.flickr:
        log.error("--include-approved requires --flickr")
        sys.exit(1)
    if args.delete_discards and not args.flickr:
        log.error("--delete-discards requires --flickr")
        sys.exit(1)
    if args.apply and not args.delete_discards:
        log.error("--apply requires --delete-discards")
        sys.exit(1)
```

Then find the `--flickr` dispatch in `main()`. In the detection path, change:

```python
        groups, conflicts = _fetch_reupload_candidates(conn)
```

to:

```python
        groups, conflicts = _fetch_reupload_candidates(conn, include_approved=args.include_approved)
```

- [ ] **Step 6: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all passed.

- [ ] **Step 7: Add detection scope line to `_print_reupload_report()` in `poller/deduplicator.py`**

In `main()`, in the `if args.flickr:` detection path (not the `--delete-discards` path), add one line immediately before the `_print_reupload_report(...)` call:

```python
        if args.include_approved:
            print("Detection scope: candidate_public + approved_public (--include-approved)")
```

- [ ] **Step 8: Verify the flag works end-to-end**

```bash
python poller/deduplicator.py --config config/config.yml --flickr --include-approved --dry-run
```

Expected: first output line reads `Detection scope: candidate_public + approved_public (--include-approved)`, followed by "Reupload pairs found: N". No errors.

- [ ] **Step 9: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "feat: add --include-approved to bp dedup --flickr for orientation duplicates (#15)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: `--delete-discards` action

**Files:**
- Modify: `poller/deduplicator.py` (add `_delete_discards()` before `_print_report()`; add dispatch in `main()`)
- Test: `tests/test_deduplicator.py` (append `_make_dedup_db()` + `_insert_group_and_discard()` + `TestDeleteDiscards`)

- [ ] **Step 1: Write the failing tests**

Open `tests/test_deduplicator.py`. After all existing test classes, append the following (including the helper functions):

```python
# ---------------------------------------------------------------------------
# _delete_discards helpers
# ---------------------------------------------------------------------------

def _make_dedup_db():
    """In-memory DB with photos + duplicate_groups tables for delete-discards tests."""
    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    conn.execute("""
        CREATE TABLE photos (
            id INTEGER PRIMARY KEY,
            flickr_id TEXT,
            uuid TEXT,
            privacy_state TEXT DEFAULT 'candidate_public',
            duplicate_role TEXT,
            duplicate_group_id INTEGER,
            flickr_deleted INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE duplicate_groups (
            id INTEGER PRIMARY KEY,
            match_key TEXT UNIQUE,
            group_type TEXT,
            keeper_id INTEGER,
            photo_count INTEGER DEFAULT 0,
            resolved INTEGER DEFAULT 0,
            notes TEXT,
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)
    return conn


def _insert_group_and_discard(
    conn,
    group_id: int = 1,
    discard_flickr_id: str = "54060000000",
    privacy_state: str = "approved_public",
    flickr_deleted: int = 0,
    resolved: int = 0,
    notes: str | None = None,
):
    """Insert a keeper + discard pair into a duplicate_group for testing."""
    import json as _json
    default_notes = _json.dumps({
        "keeper_flickr_id": "48922000000",
        "discard_flickr_id": discard_flickr_id,
        "summary": f"DSC_0042.JPG | 2022-08-14T10:23:11 | linked=48922000000 → orphan={discard_flickr_id}",
    })
    conn.execute(
        "INSERT INTO duplicate_groups (id, match_key, group_type, keeper_id, photo_count, resolved, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (group_id, f"reupload:48922000000:{discard_flickr_id}", "reupload_uncertain", 10, 2, resolved, notes or default_notes),
    )
    conn.execute(
        "INSERT INTO photos (id, flickr_id, uuid, privacy_state, duplicate_role, duplicate_group_id, flickr_deleted) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (10, "48922000000", "AAAA", "approved_public", "keeper", group_id, 0),
    )
    conn.execute(
        "INSERT INTO photos (id, flickr_id, uuid, privacy_state, duplicate_role, duplicate_group_id, flickr_deleted) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (20, discard_flickr_id, None, privacy_state, "discard", group_id, flickr_deleted),
    )


# ---------------------------------------------------------------------------
# TestDeleteDiscards
# ---------------------------------------------------------------------------

class TestDeleteDiscards(unittest.TestCase):

    def test_dry_run_no_api_calls(self):
        from unittest.mock import MagicMock
        from poller.deduplicator import _delete_discards
        conn = _make_dedup_db()
        _insert_group_and_discard(conn)
        client = MagicMock()
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=True)
        self.assertEqual(deleted, 0)
        self.assertEqual(already_gone, 0)
        self.assertEqual(errors, 0)
        client.delete_photo.assert_not_called()

    def test_apply_success_sets_flickr_deleted_and_resolved(self):
        from unittest.mock import MagicMock
        from poller.deduplicator import _delete_discards
        conn = _make_dedup_db()
        _insert_group_and_discard(conn)
        client = MagicMock()
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted, 1)
        self.assertEqual(already_gone, 0)
        self.assertEqual(errors, 0)
        client.delete_photo.assert_called_once_with("54060000000")
        row = conn.execute(
            "SELECT flickr_deleted FROM photos WHERE flickr_id = '54060000000'"
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 1)
        group = conn.execute(
            "SELECT resolved FROM duplicate_groups WHERE id = 1"
        ).fetchone()
        self.assertEqual(group["resolved"], 1)

    def test_flickr_error_1_treated_as_success(self):
        from unittest.mock import MagicMock
        from flickr.flickr_client import FlickrError
        from poller.deduplicator import _delete_discards
        conn = _make_dedup_db()
        _insert_group_and_discard(conn)
        client = MagicMock()
        client.delete_photo.side_effect = FlickrError(1, "Photo not found")
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted, 0)
        self.assertEqual(already_gone, 1)
        self.assertEqual(errors, 0)
        row = conn.execute(
            "SELECT flickr_deleted FROM photos WHERE flickr_id = '54060000000'"
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 1)
        group = conn.execute(
            "SELECT resolved FROM duplicate_groups WHERE id = 1"
        ).fetchone()
        self.assertEqual(group["resolved"], 1)

    def test_other_flickr_error_leaves_record_untouched(self):
        from unittest.mock import MagicMock
        from flickr.flickr_client import FlickrError
        from poller.deduplicator import _delete_discards
        conn = _make_dedup_db()
        _insert_group_and_discard(conn)
        client = MagicMock()
        client.delete_photo.side_effect = FlickrError(99, "Insufficient permissions")
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted, 0)
        self.assertEqual(already_gone, 0)
        self.assertEqual(errors, 1)
        row = conn.execute(
            "SELECT flickr_deleted FROM photos WHERE flickr_id = '54060000000'"
        ).fetchone()
        self.assertEqual(row["flickr_deleted"], 0)
        group = conn.execute(
            "SELECT resolved FROM duplicate_groups WHERE id = 1"
        ).fetchone()
        self.assertEqual(group["resolved"], 0)

    def test_candidate_public_discard_excluded(self):
        from unittest.mock import MagicMock
        from poller.deduplicator import _delete_discards
        conn = _make_dedup_db()
        _insert_group_and_discard(conn, privacy_state="candidate_public")
        client = MagicMock()
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted + already_gone + errors, 0)
        client.delete_photo.assert_not_called()

    def test_already_flickr_deleted_excluded(self):
        from unittest.mock import MagicMock
        from poller.deduplicator import _delete_discards
        conn = _make_dedup_db()
        _insert_group_and_discard(conn, flickr_deleted=1)
        client = MagicMock()
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted + already_gone + errors, 0)
        client.delete_photo.assert_not_called()

    def test_resolved_group_excluded(self):
        from unittest.mock import MagicMock
        from poller.deduplicator import _delete_discards
        conn = _make_dedup_db()
        _insert_group_and_discard(conn, resolved=1)
        client = MagicMock()
        deleted, already_gone, errors = _delete_discards(conn, client, dry_run=False)
        self.assertEqual(deleted + already_gone + errors, 0)
        client.delete_photo.assert_not_called()
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_deduplicator.py::TestDeleteDiscards -v
```

Expected: `ImportError` — `_delete_discards` doesn't exist yet.

- [ ] **Step 3: Add `_delete_discards()` to `poller/deduplicator.py`**

Find `_print_report()` in `poller/deduplicator.py`. Add the following function immediately before it:

```python
def _delete_discards(
    conn: sqlite3.Connection,
    client: Any,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Delete approved_public discard records from Flickr.

    Queries duplicate_groups for unresolved groups whose discard is
    approved_public and not yet flickr_deleted. Calls client.delete_photo()
    for each. Treats FlickrError(1) (photo not found) as success.

    Returns (deleted, already_gone, errors).
    """
    from flickr.flickr_client import FlickrError

    rows = conn.execute("""
        SELECT p.id, p.flickr_id, p.privacy_state,
               dg.id AS group_id, dg.group_type, dg.notes
        FROM photos p
        JOIN duplicate_groups dg ON p.duplicate_group_id = dg.id
        WHERE p.duplicate_role = 'discard'
          AND p.privacy_state = 'approved_public'
          AND (p.flickr_deleted IS NULL OR p.flickr_deleted = 0)
          AND dg.resolved = 0
    """).fetchall()

    if not rows:
        print("No discards eligible for deletion.")
        return 0, 0, 0

    label = "to delete" if not dry_run else "eligible for deletion"
    print(f"\nDiscards {label}: {len(rows)}")
    for r in rows:
        try:
            summary = json.loads(r["notes"]).get("summary", "") if r["notes"] else ""
        except (json.JSONDecodeError, TypeError):
            summary = r["notes"] or ""
        print(f"  flickr_id={r['flickr_id']}  group_type={r['group_type']}  privacy={r['privacy_state']}")
        if summary:
            print(f"    {summary}")

    if dry_run:
        print("\nDry run — no Flickr API calls made. Use --apply to delete.")
        return 0, 0, 0

    deleted = already_gone = errors = 0
    for r in rows:
        photo_id  = r["id"]
        flickr_id = r["flickr_id"]
        group_id  = r["group_id"]
        try:
            client.delete_photo(flickr_id)
            conn.execute(
                "UPDATE photos SET flickr_deleted = 1,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (photo_id,),
            )
            conn.execute("UPDATE duplicate_groups SET resolved = 1 WHERE id = ?", (group_id,))
            conn.commit()
            deleted += 1
            print(f"  deleted  flickr_id={flickr_id}")
        except FlickrError as exc:
            if exc.code == 1:
                conn.execute(
                    "UPDATE photos SET flickr_deleted = 1,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                    (photo_id,),
                )
                conn.execute("UPDATE duplicate_groups SET resolved = 1 WHERE id = ?", (group_id,))
                conn.commit()
                already_gone += 1
                print(f"  already gone (Flickr error 1)  flickr_id={flickr_id}")
            else:
                errors += 1
                log.error("Failed to delete flickr_id=%s: %s", flickr_id, exc)
                print(f"  error (code {exc.code})  flickr_id={flickr_id}")

    print(f"\nDone: {deleted} deleted, {already_gone} already gone, {errors} error(s)")
    return deleted, already_gone, errors
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_deduplicator.py::TestDeleteDiscards -v
```

Expected: 7 passed.

- [ ] **Step 5: Wire `--delete-discards` into `main()`**

In `poller/deduplicator.py`, find the `if args.flickr:` dispatch block. It currently reads (after Task 2):

```python
    if args.flickr:
        log.info("Scanning for re-upload duplicates in %s …", db_path)
        groups, conflicts = _fetch_reupload_candidates(conn, include_approved=args.include_approved)
        ...
        return
```

Prepend a `--delete-discards` early-return path at the very top of the `if args.flickr:` block:

```python
    if args.flickr:
        if args.delete_discards:
            log.info("Loading Flickr client for delete-discards …")
            from flickr.flickr_client import FlickrClient
            client = FlickrClient.from_config(config)
            dry_run = not args.apply
            _delete_discards(conn, client, dry_run=dry_run)
            conn.close()
            return

        log.info("Scanning for re-upload duplicates in %s …", db_path)
        groups, conflicts = _fetch_reupload_candidates(conn, include_approved=args.include_approved)
        ...
```

- [ ] **Step 6: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all passed (7 more tests than before Task 3).

- [ ] **Step 7: Verify dry-run end-to-end**

```bash
python poller/deduplicator.py --config config/config.yml --flickr --delete-discards
```

Expected: prints "No discards eligible for deletion." (or a list of discards if any `approved_public` records with `duplicate_role='discard'` exist in the DB). No errors. No Flickr API calls.

- [ ] **Step 8: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "feat: add --delete-discards to bp dedup --flickr (#15)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: README + GitHub issue

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update test count in README**

Run the suite to get the current count:

```bash
python -m pytest tests/ -q 2>&1 | tail -3
```

Find the line in `README.md` that reads `python -m pytest tests/ -q  # NNN tests` (or similar) and update the count to match.

- [ ] **Step 2: Apply `has-plan` label and comment on GH #15**

```bash
gh issue edit 15 --add-label "has-plan"
gh issue comment 15 --body "Implementation plan written: \`docs/superpowers/plans/2026-05-14-orientation-dedup-15.md\`. Ready to implement after #17 Phase 1 lands."
```

- [ ] **Step 3: Commit README**

```bash
git add README.md
git commit -m "docs: update test count after #15 implementation

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
