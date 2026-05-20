# Re-upload Dedup Phase 3: Metadata Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `_sync_keeper_metadata()` in `poller/deduplicator.py` and wire it to a new `--sync-metadata` CLI flag, so that when a reupload group's keeper is the Flickr-only orphan and the linked record's Flickr photo has been deleted, the orphan's full Flickr presence transfers to the linked record and the orphan is soft-deleted.

**Architecture:** One new function added after `_mark_reupload_discards()` in `poller/deduplicator.py`; three SQL writes per group in a single transaction (update linked record, soft-delete orphan, resolve group); no Flickr API calls. Tests use a new in-memory DB helper with the extended column set needed for Phase 3.

**Tech Stack:** Python 3, SQLite (`sqlite3`), `argparse`, `pytest`/`unittest`

---

## File Map

| File | Change |
|------|--------|
| `poller/deduplicator.py` | Add `_sync_keeper_metadata()`; add `--sync-metadata` arg; update guards; add dispatch block |
| `tests/test_deduplicator.py` | Add `_make_db_for_sync()` helper; add `TestSyncKeeperMetadata` class (6 tests) |
| `README.md` | Update test count |
| `docs/superpowers/specs/2026-05-20-reupload-dedup-105-phase3-design.md` | Add `**Status:** ✓ done` |

---

## Task 1: Failing tests for `_sync_keeper_metadata()`

Write the test class and helper first. All tests will fail until Task 2 adds the function.

**Files:**
- Modify: `tests/test_deduplicator.py` (append after `TestDeleteDiscardsQuery`, before `if __name__ == "__main__":`)

---

- [ ] **Step 1: Locate the insertion point**

Open `tests/test_deduplicator.py`. The last class is `TestDeleteDiscardsQuery` ending around
line 1049. The file ends with:

```python
if __name__ == "__main__":
    unittest.main()
```

Append the new helper function and test class between `TestDeleteDiscardsQuery` and that final block.

- [ ] **Step 2: Append the DB helper**

The existing `_make_db_with_groups()` (line ~481) is too slim — it lacks the Flickr metadata
columns needed by `_sync_keeper_metadata()`. Add a new helper after `TestDeleteDiscardsQuery`:

```python
def _make_db_for_sync() -> _sqlite3.Connection:
    """In-memory DB with all columns needed by _sync_keeper_metadata() tests."""
    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    conn.execute("""
        CREATE TABLE duplicate_groups (
            id INTEGER PRIMARY KEY,
            match_key TEXT NOT NULL UNIQUE,
            group_type TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            resolved_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE photos (
            id INTEGER PRIMARY KEY,
            flickr_id TEXT,
            uuid TEXT,
            original_filename TEXT,
            duplicate_group_id INTEGER,
            duplicate_role TEXT,
            flickr_deleted INTEGER DEFAULT 0,
            flickr_secret TEXT,
            flickr_server TEXT,
            flickr_farm INTEGER,
            flickr_title TEXT,
            flickr_description TEXT,
            flickr_tags TEXT,
            flickr_tags_hash TEXT,
            flickr_last_updated TEXT,
            width INTEGER,
            height INTEGER,
            thumbnail_path TEXT,
            merged_into_id INTEGER,
            updated_at TEXT
        )
    """)
    return conn
```

- [ ] **Step 3: Append the test class**

```python
class TestSyncKeeperMetadata(unittest.TestCase):
    """Tests for _sync_keeper_metadata() — Phase 3 metadata transfer."""

    def _setup(
        self,
        group_type: str = "reupload",
        resolved: int = 0,
        discard_flickr_deleted: int = 1,
        keeper_has_uuid: bool = False,
    ) -> _sqlite3.Connection:
        """Seed one reupload group with orphan keeper + linked discard."""
        conn = _make_db_for_sync()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, resolved)"
            " VALUES (1, 'reupload:48000:54000', ?, ?)",
            (group_type, resolved),
        )
        # Orphan (keeper): Flickr-only unless keeper_has_uuid=True
        conn.execute(
            """INSERT INTO photos
               (id, flickr_id, uuid, original_filename,
                duplicate_group_id, duplicate_role, flickr_deleted,
                flickr_secret, flickr_server, flickr_farm,
                flickr_title, flickr_description, flickr_tags, flickr_tags_hash,
                flickr_last_updated, width, height, thumbnail_path)
               VALUES (1, '54000', ?, 'IMG_001.JPG',
                       1, 'keeper', 0,
                       'sec1', '65535', 1,
                       'My Title', 'My Desc', '["a","b"]', 'hash1',
                       '2024-01-01T00:00:00Z', 4000, 3000, '/thumb/54000.jpg')""",
            ('keeper-uuid' if keeper_has_uuid else None,),
        )
        # Linked (discard): has uuid, Flickr photo deleted
        conn.execute(
            """INSERT INTO photos
               (id, flickr_id, uuid, original_filename,
                duplicate_group_id, duplicate_role, flickr_deleted,
                width, height)
               VALUES (2, '48000', 'photos-uuid-001', 'IMG_001.JPG',
                       1, 'discard', ?,
                       2000, 1500)""",
            (discard_flickr_deleted,),
        )
        conn.commit()
        return conn

    def test_syncs_all_fields_to_linked(self):
        from poller.deduplicator import _sync_keeper_metadata

        conn = self._setup()
        count = _sync_keeper_metadata(conn, dry_run=False)
        self.assertEqual(count, 1)

        linked = conn.execute("SELECT * FROM photos WHERE id = 2").fetchone()
        self.assertEqual(linked["flickr_id"], "54000")
        self.assertEqual(linked["flickr_secret"], "sec1")
        self.assertEqual(linked["flickr_server"], "65535")
        self.assertEqual(linked["flickr_farm"], 1)
        self.assertEqual(linked["flickr_title"], "My Title")
        self.assertEqual(linked["flickr_description"], "My Desc")
        self.assertEqual(linked["flickr_tags"], '["a","b"]')
        self.assertEqual(linked["flickr_tags_hash"], "hash1")
        self.assertEqual(linked["flickr_last_updated"], "2024-01-01T00:00:00Z")
        self.assertEqual(linked["width"], 4000)
        self.assertEqual(linked["height"], 3000)
        self.assertEqual(linked["thumbnail_path"], "/thumb/54000.jpg")
        self.assertEqual(linked["flickr_deleted"], 0)

        orphan = conn.execute("SELECT merged_into_id FROM photos WHERE id = 1").fetchone()
        self.assertEqual(orphan["merged_into_id"], 2)

        group = conn.execute("SELECT resolved FROM duplicate_groups WHERE id = 1").fetchone()
        self.assertEqual(group["resolved"], 1)

    def test_skips_when_keeper_is_linked(self):
        from poller.deduplicator import _sync_keeper_metadata

        conn = self._setup(keeper_has_uuid=True)
        count = _sync_keeper_metadata(conn, dry_run=False)
        self.assertEqual(count, 0)

        linked = conn.execute("SELECT flickr_id FROM photos WHERE id = 2").fetchone()
        self.assertEqual(linked["flickr_id"], "48000")  # unchanged

    def test_skips_when_discard_not_deleted(self):
        from poller.deduplicator import _sync_keeper_metadata

        conn = self._setup(discard_flickr_deleted=0)
        count = _sync_keeper_metadata(conn, dry_run=False)
        self.assertEqual(count, 0)

        linked = conn.execute("SELECT flickr_id FROM photos WHERE id = 2").fetchone()
        self.assertEqual(linked["flickr_id"], "48000")  # unchanged

    def test_skips_uncertain_groups(self):
        from poller.deduplicator import _sync_keeper_metadata

        conn = self._setup(group_type="reupload_uncertain")
        count = _sync_keeper_metadata(conn, dry_run=False)
        self.assertEqual(count, 0)

    def test_skips_resolved_groups(self):
        from poller.deduplicator import _sync_keeper_metadata

        conn = self._setup(resolved=1)
        count = _sync_keeper_metadata(conn, dry_run=False)
        self.assertEqual(count, 0)

    def test_dry_run_no_changes(self):
        from poller.deduplicator import _sync_keeper_metadata

        conn = self._setup()
        count = _sync_keeper_metadata(conn, dry_run=True)
        self.assertEqual(count, 1)  # eligible count returned in dry-run

        linked = conn.execute("SELECT flickr_id FROM photos WHERE id = 2").fetchone()
        self.assertEqual(linked["flickr_id"], "48000")  # DB unchanged

        orphan = conn.execute("SELECT merged_into_id FROM photos WHERE id = 1").fetchone()
        self.assertIsNone(orphan["merged_into_id"])  # not soft-deleted

        group = conn.execute("SELECT resolved FROM duplicate_groups WHERE id = 1").fetchone()
        self.assertEqual(group["resolved"], 0)  # not resolved
```

- [ ] **Step 4: Run the tests to confirm all 6 fail**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/test_deduplicator.py::TestSyncKeeperMetadata -v 2>&1 | tail -15
```

Expected: all 6 FAIL with `ImportError: cannot import name '_sync_keeper_metadata'`.
If any test errors in setup (not import error), read the traceback before continuing.

- [ ] **Step 5: Confirm no regressions**

```bash
python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: same count as before (794 passed), 6 new failures.

- [ ] **Step 6: Commit**

```bash
git add tests/test_deduplicator.py
git commit -m "test: add failing tests for _sync_keeper_metadata() (#105)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Implement `_sync_keeper_metadata()`

**Files:**
- Modify: `poller/deduplicator.py` (add function after `_mark_reupload_discards()`, around line 848)

---

- [ ] **Step 1: Find the insertion point**

Open `poller/deduplicator.py`. Locate `_mark_reupload_discards()` which ends around line 847
with `return len(rows)`. The next block is a comment `# ---------------------------------------------------------------------------\n# Report`.
Insert the new function between them.

- [ ] **Step 2: Write the function**

```python
def _sync_keeper_metadata(
    conn: sqlite3.Connection,
    dry_run: bool = True,
    verbose: bool = False,
) -> int:
    """Transfer Flickr metadata from orphan keeper to linked record.

    Applies to reupload groups where:
    - keeper is Flickr-only (uuid IS NULL)
    - discard is the linked Apple Photos record (uuid IS NOT NULL)
    - discard's Flickr photo has been deleted (flickr_deleted = 1)

    Per group: copies all Flickr fields to linked, soft-deletes orphan via
    merged_into_id, marks group resolved. DB-only — no Flickr API calls.

    Returns count of groups synced (or eligible in dry-run).
    """
    rows = conn.execute("""
        SELECT
            k.id          AS keeper_id,
            k.flickr_id   AS keeper_flickr_id,
            k.flickr_secret,
            k.flickr_server,
            k.flickr_farm,
            k.flickr_title,
            k.flickr_description,
            k.flickr_tags,
            k.flickr_tags_hash,
            k.flickr_last_updated,
            k.width        AS keeper_width,
            k.height       AS keeper_height,
            k.thumbnail_path AS keeper_thumb,
            d.id           AS linked_id,
            d.original_filename AS linked_filename,
            dg.id          AS group_id
        FROM photos k
        JOIN duplicate_groups dg ON k.duplicate_group_id = dg.id
        JOIN photos d ON d.duplicate_group_id = dg.id
        WHERE k.duplicate_role = 'keeper'
          AND k.uuid IS NULL
          AND d.duplicate_role = 'discard'
          AND d.uuid IS NOT NULL
          AND d.flickr_deleted = 1
          AND dg.group_type = 'reupload'
          AND dg.resolved = 0
    """).fetchall()

    if not rows:
        print("No reupload groups eligible for metadata sync.")
        return 0

    label = "to sync" if not dry_run else "eligible for metadata sync"
    print(f"\nReupload groups {label}: {len(rows)}")

    show = rows if verbose else rows[:10]
    for r in show:
        print(
            f"  group_id={r['group_id']}  flickr_id={r['keeper_flickr_id']}"
            f" → linked id={r['linked_id']} ({r['linked_filename']})"
        )
    if not verbose and len(rows) > 10:
        print(f"  ... and {len(rows) - 10} more (use --verbose to see all)")

    if dry_run:
        print("\nDry run — no changes written. Use --apply to persist.")
        return len(rows)

    for r in rows:
        conn.execute(
            """UPDATE photos
               SET flickr_id          = ?,
                   flickr_secret      = ?,
                   flickr_server      = ?,
                   flickr_farm        = ?,
                   flickr_title       = ?,
                   flickr_description = ?,
                   flickr_tags        = ?,
                   flickr_tags_hash   = ?,
                   flickr_last_updated = ?,
                   width              = ?,
                   height             = ?,
                   thumbnail_path     = ?,
                   flickr_deleted     = 0,
                   updated_at         = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
               WHERE id = ?""",
            (
                r["keeper_flickr_id"],
                r["flickr_secret"],
                r["flickr_server"],
                r["flickr_farm"],
                r["flickr_title"],
                r["flickr_description"],
                r["flickr_tags"],
                r["flickr_tags_hash"],
                r["flickr_last_updated"],
                r["keeper_width"],
                r["keeper_height"],
                r["keeper_thumb"],
                r["linked_id"],
            ),
        )
        conn.execute(
            """UPDATE photos
               SET merged_into_id = ?,
                   updated_at     = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
               WHERE id = ?""",
            (r["linked_id"], r["keeper_id"]),
        )
        conn.execute(
            "UPDATE duplicate_groups SET resolved = 1, resolved_at = datetime('now') WHERE id = ?",
            (r["group_id"],),
        )
        conn.commit()

    print(f"\nSynced metadata for {len(rows)} reupload groups.")
    return len(rows)
```

- [ ] **Step 3: Run the new tests**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/test_deduplicator.py::TestSyncKeeperMetadata -v 2>&1 | tail -15
```

Expected: all 6 PASS.

- [ ] **Step 4: Run the full suite**

```bash
python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: 800 passed (794 + 6 new), 0 failed.

- [ ] **Step 5: Run lint**

```bash
make lint 2>&1 | tail -10
```

Expected: no errors. The function signature uses `sqlite3.Connection` which is already imported at the top of `deduplicator.py`. If mypy flags the `resolved_at` column in `duplicate_groups` (some in-memory test DBs lack it), that is a test concern not a lint concern — the production schema has it.

- [ ] **Step 6: Commit**

```bash
git add poller/deduplicator.py
git commit -m "feat: add _sync_keeper_metadata() for Phase 3 reupload dedup (#105)

Transfers orphan keeper's Flickr identity and all metadata fields to the
linked record, soft-deletes the orphan via merged_into_id, resolves the group.
DB-only — no Flickr API calls.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Wire `--sync-metadata` CLI flag in `main()`

**Files:**
- Modify: `poller/deduplicator.py` (the `main()` function, around lines 950–1068)

---

- [ ] **Step 1: Add the `--sync-metadata` argument**

Find the `--mark-discards` argument block (around line 991):

```python
    parser.add_argument(
        "--mark-discards",
        action="store_true",
        help=(
            "Mark confirmed reupload discards as duplicate_flickr in the DB "
            "(requires --flickr; use --apply to execute, default is dry-run)"
        ),
    )
```

Add the new argument immediately after it:

```python
    parser.add_argument(
        "--sync-metadata",
        action="store_true",
        help=(
            "Transfer Flickr metadata from orphan keeper to linked record "
            "(requires --flickr; use --apply to execute, default is dry-run)"
        ),
    )
```

- [ ] **Step 2: Update the `--apply` help text**

Find this line (around line 989):

```python
        help="Execute deletions (default is dry-run). Requires --delete-discards.",
```

Replace with:

```python
        help="Execute writes (default is dry-run). Requires --delete-discards, --mark-discards, or --sync-metadata.",
```

- [ ] **Step 3: Add guards**

Find this guard block (around line 1018):

```python
    if args.mark_discards and args.delete_discards:
        log.error("--mark-discards and --delete-discards cannot be used together")
        sys.exit(1)
    if args.apply and not args.delete_discards and not args.mark_discards:
        log.error("--apply requires --delete-discards or --mark-discards")
        sys.exit(1)
```

Replace with:

```python
    if args.sync_metadata and not args.flickr:
        log.error("--sync-metadata requires --flickr")
        sys.exit(1)
    if args.mark_discards and args.delete_discards:
        log.error("--mark-discards and --delete-discards cannot be used together")
        sys.exit(1)
    if args.sync_metadata and (args.mark_discards or args.delete_discards):
        log.error("--sync-metadata cannot be combined with --mark-discards or --delete-discards")
        sys.exit(1)
    if args.apply and not args.delete_discards and not args.mark_discards and not args.sync_metadata:
        log.error("--apply requires --delete-discards, --mark-discards, or --sync-metadata")
        sys.exit(1)
```

- [ ] **Step 4: Add the dispatch block**

Find this block inside `if args.flickr:` (around line 1043):

```python
        if args.mark_discards:
            log.info("Marking reupload discards in %s …", db_path)
            _mark_reupload_discards(conn, dry_run=not args.apply, verbose=args.verbose)
            conn.close()
            return
        if args.delete_discards:
```

Add the `sync_metadata` dispatch before `mark_discards`:

```python
        if args.sync_metadata:
            log.info("Syncing keeper metadata in %s …", db_path)
            _sync_keeper_metadata(conn, dry_run=not args.apply, verbose=args.verbose)
            conn.close()
            return
        if args.mark_discards:
            log.info("Marking reupload discards in %s …", db_path)
            _mark_reupload_discards(conn, dry_run=not args.apply, verbose=args.verbose)
            conn.close()
            return
        if args.delete_discards:
```

- [ ] **Step 5: Run the full test suite**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: 800 passed, 0 failed.

- [ ] **Step 6: Run lint**

```bash
make lint 2>&1 | tail -5
```

Expected: no errors.

- [ ] **Step 7: Smoke-test the CLI flag (dry-run against live DB)**

```bash
python -m bp dedup --flickr --sync-metadata 2>&1 | tail -10
```

Expected: prints "No reupload groups eligible for metadata sync." (no live data yet) or
a count if any eligible groups exist. Must not crash. Exit code 0.

If `bp` is not available, run directly:
```bash
python poller/deduplicator.py --flickr --sync-metadata 2>&1 | tail -10
```

- [ ] **Step 8: Commit**

```bash
git add poller/deduplicator.py
git commit -m "feat: wire --sync-metadata CLI flag for Phase 3 (#105)

Adds --sync-metadata argument to bp dedup --flickr with dry-run default,
--apply to execute. Mutually exclusive with --mark-discards and --delete-discards.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Update README, mark spec done, close issue

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-20-reupload-dedup-105-phase3-design.md`

---

- [ ] **Step 1: Confirm current test count**

```bash
cd "/Users/cdevers/Documents/GitHub/Blue Pearmain"
python -m pytest tests/ -q 2>&1 | tail -3
```

Note the count (expected: 800 passed).

- [ ] **Step 2: Update README.md**

Find both places where the test count appears:
```bash
grep -n "794\|800\|tests" README.md | grep -i "test" | head -10
```

Update both instances to the new count (e.g. 794 → 800). The two lines are around line 219
(`| tests/ | Unit tests (N tests) |`) and line 543 (the long description sentence starting
`N tests covering...`).

On line 543, also append to the coverage description: add `", reupload metadata sync
(keeper→linked transfer)"` after `"mark/delete discards"`.

- [ ] **Step 3: Mark spec done**

Open `docs/superpowers/specs/2026-05-20-reupload-dedup-105-phase3-design.md`. Add after the
`**GitHub issue:** #105` line:

```markdown
**Status:** ✓ done
```

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-05-20-reupload-dedup-105-phase3-design.md
git commit -m "docs: update test count and mark Phase 3 spec done (#105)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 5: Push**

```bash
git push
```

- [ ] **Step 6: Close the GitHub issue**

```bash
gh issue close 105 --comment "Phase 3 complete. Added _sync_keeper_metadata(): transfers orphan keeper's flickr_id, flickr_secret/server/farm, title, description, tags, tag hash, last-updated, width, height, and thumbnail_path to the linked record; soft-deletes orphan via merged_into_id; resolves the group. CLI flag: bp dedup --flickr --sync-metadata [--apply]. 6 new tests."
```
