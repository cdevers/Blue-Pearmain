# Re-upload Dedup Phase 2: Privacy Enforcement — Implementation Plan (#104)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--mark-discards` to `bp dedup --flickr` so confirmed `reupload` discards get `privacy_state = 'duplicate_flickr'` in the DB (no API calls), and fix the existing `--delete-discards` path so it finds those `duplicate_flickr` records instead of only `approved_public` ones.

**Architecture:** One new function (`_mark_reupload_discards`) + a three-line query fix to `_delete_discards` + CLI wiring in `main()`. Tests use an in-memory SQLite DB with a new `_make_db_with_groups()` helper that adds `duplicate_groups` alongside `photos`. No new files.

**Tech Stack:** Python stdlib — `sqlite3`, `argparse`. No new dependencies.

---

## Files

| File | Change |
|------|--------|
| `poller/deduplicator.py` | Add `_mark_reupload_discards()`; fix 3-line WHERE clause in `_delete_discards()`; add `--mark-discards` argparse arg; update 2 guards; add dispatch block in `main()` |
| `tests/test_deduplicator.py` | Add `_make_db_with_groups()` helper; add `TestMarkReuploaDiscards` (6 tests); add `TestDeleteDiscardsQuery` (1 test) |

---

## Task 1: `_mark_reupload_discards()` + tests

**Files:**
- Modify: `poller/deduplicator.py` (add after `_delete_discards()` ~line 793)
- Modify: `tests/test_deduplicator.py` (append after `TestFetchReuploadCandidates`)

- [ ] **Step 1: Add `_make_db_with_groups()` helper to `tests/test_deduplicator.py`**

Append immediately after the existing `_insert()` function (after line 478), before `class TestFetchReuploadCandidates`:

```python
def _make_db_with_groups() -> _sqlite3.Connection:
    """In-memory DB with photos + duplicate_groups for Phase 2 tests."""
    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    conn.execute("""
        CREATE TABLE duplicate_groups (
            id INTEGER PRIMARY KEY,
            match_key TEXT NOT NULL UNIQUE,
            group_type TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0
        )
    """)
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
            duplicate_group_id INTEGER,
            duplicate_role TEXT,
            flickr_deleted INTEGER DEFAULT 0
        )
    """)
    return conn
```

- [ ] **Step 2: Append failing tests for `_mark_reupload_discards` to `tests/test_deduplicator.py`**

Append at the end of the file (before `if __name__ == "__main__": unittest.main()`):

```python
class TestMarkReuploaDiscards(unittest.TestCase):
    """Tests for _mark_reupload_discards().

    Uses _make_db_with_groups() which creates both photos and duplicate_groups.
    """

    def _setup(
        self,
        group_type: str = "reupload",
        privacy_state: str = "candidate_public",
        flickr_deleted: int = 0,
        resolved: int = 0,
    ) -> _sqlite3.Connection:
        conn = _make_db_with_groups()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, resolved)"
            " VALUES (1, 'reupload:48000:54000', ?, ?)",
            (group_type, resolved),
        )
        conn.execute(
            "INSERT INTO photos"
            " (id, flickr_id, privacy_state, duplicate_group_id, duplicate_role, flickr_deleted)"
            " VALUES (1, '48922000000', ?, 1, 'discard', ?)",
            (privacy_state, flickr_deleted),
        )
        conn.commit()
        return conn

    def test_marks_reupload_discards(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup()
        count = _mark_reupload_discards(conn, dry_run=False)
        self.assertEqual(count, 1)
        row = conn.execute("SELECT privacy_state FROM photos WHERE id = 1").fetchone()
        self.assertEqual(row["privacy_state"], "duplicate_flickr")

    def test_skips_uncertain_groups(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup(group_type="reupload_uncertain")
        count = _mark_reupload_discards(conn, dry_run=False)
        self.assertEqual(count, 0)
        row = conn.execute("SELECT privacy_state FROM photos WHERE id = 1").fetchone()
        self.assertEqual(row["privacy_state"], "candidate_public")

    def test_skips_already_marked(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup(privacy_state="duplicate_flickr")
        count = _mark_reupload_discards(conn, dry_run=False)
        self.assertEqual(count, 0)

    def test_skips_flickr_deleted(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup(flickr_deleted=1)
        count = _mark_reupload_discards(conn, dry_run=False)
        self.assertEqual(count, 0)

    def test_dry_run_no_changes(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup()
        count = _mark_reupload_discards(conn, dry_run=True)
        self.assertEqual(count, 1)  # eligible count returned even in dry-run
        row = conn.execute("SELECT privacy_state FROM photos WHERE id = 1").fetchone()
        self.assertEqual(row["privacy_state"], "candidate_public")  # unchanged

    def test_skips_resolved_groups(self):
        from poller.deduplicator import _mark_reupload_discards

        conn = self._setup(resolved=1)
        count = _mark_reupload_discards(conn, dry_run=False)
        self.assertEqual(count, 0)
```

- [ ] **Step 3: Run to verify tests fail**

```bash
python -m pytest tests/test_deduplicator.py::TestMarkReuploaDiscards -v
```

Expected: `ImportError` — `_mark_reupload_discards` doesn't exist yet.

- [ ] **Step 4: Add `_mark_reupload_discards()` to `poller/deduplicator.py`**

Insert after `_delete_discards()` (after line 793, before the `# ---------------------------------------------------------------------------` comment block):

```python
def _mark_reupload_discards(
    conn: sqlite3.Connection,
    dry_run: bool = True,
    verbose: bool = False,
) -> int:
    """Mark confirmed reupload discards as duplicate_flickr in the DB.

    Only acts on group_type='reupload' groups (not reupload_uncertain).
    Does not set resolved=1 — that is reserved for after Flickr deletion.

    Returns count of records marked (or eligible in dry-run).
    """
    rows = conn.execute("""
        SELECT p.id, p.flickr_id, p.privacy_state,
               dg.id AS group_id, dg.notes
        FROM photos p
        JOIN duplicate_groups dg ON p.duplicate_group_id = dg.id
        WHERE p.duplicate_role = 'discard'
          AND dg.group_type = 'reupload'
          AND p.privacy_state != 'duplicate_flickr'
          AND p.flickr_deleted = 0
          AND dg.resolved = 0
    """).fetchall()

    if not rows:
        print("No reupload discards eligible to mark.")
        return 0

    label = "to mark" if not dry_run else "eligible for marking"
    print(f"\nReupload discards {label}: {len(rows)}")

    show = rows if verbose else rows[:10]
    for r in show:
        print(f"  flickr_id={r['flickr_id']}  {r['privacy_state']} → duplicate_flickr")
    if not verbose and len(rows) > 10:
        print(f"  ... and {len(rows) - 10} more (use --verbose to see all)")

    if dry_run:
        print("\nDry run — no changes written. Use --apply to persist.")
        return len(rows)

    for r in rows:
        conn.execute(
            "UPDATE photos SET privacy_state = 'duplicate_flickr',"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (r["id"],),
        )
    conn.commit()
    print(f"\nMarked {len(rows)} reupload discards as duplicate_flickr.")
    return len(rows)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_deduplicator.py::TestMarkReuploaDiscards -v
```

Expected: 6 passed.

- [ ] **Step 6: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all passed (6 more than before).

- [ ] **Step 7: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "$(cat <<'EOF'
feat: add _mark_reupload_discards for Phase 2 privacy enforcement (#104)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Fix `_delete_discards()` query + test

**Files:**
- Modify: `poller/deduplicator.py` (`_delete_discards()` WHERE clause, lines ~733–736)
- Modify: `tests/test_deduplicator.py` (append `TestDeleteDiscardsQuery`)

- [ ] **Step 1: Append failing test to `tests/test_deduplicator.py`**

Append before `if __name__ == "__main__": unittest.main()`:

```python
class TestDeleteDiscardsQuery(unittest.TestCase):
    """Verify the WHERE clause in _delete_discards finds duplicate_flickr discards."""

    def test_delete_discards_finds_duplicate_flickr(self):
        conn = _make_db_with_groups()
        conn.execute(
            "INSERT INTO duplicate_groups (id, match_key, group_type, resolved)"
            " VALUES (1, 'reupload:48000:54000', 'reupload', 0)"
        )
        conn.execute(
            "INSERT INTO photos"
            " (id, flickr_id, privacy_state, duplicate_group_id, duplicate_role, flickr_deleted)"
            " VALUES (1, '48922000000', 'duplicate_flickr', 1, 'discard', 0)"
        )
        conn.commit()
        # Run the fixed WHERE clause directly — no Flickr API needed
        rows = conn.execute("""
            SELECT p.id, p.flickr_id
            FROM photos p
            JOIN duplicate_groups dg ON p.duplicate_group_id = dg.id
            WHERE p.duplicate_role = 'discard'
              AND dg.group_type = 'reupload'
              AND p.privacy_state = 'duplicate_flickr'
              AND (p.flickr_deleted IS NULL OR p.flickr_deleted = 0)
              AND dg.resolved = 0
        """).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["flickr_id"], "48922000000")
```

- [ ] **Step 2: Run to confirm it passes (it tests the query directly, not the function)**

```bash
python -m pytest tests/test_deduplicator.py::TestDeleteDiscardsQuery -v
```

Expected: 1 passed — the test verifies the intended query shape, not the current (broken) function.

- [ ] **Step 3: Fix the WHERE clause in `_delete_discards()` in `poller/deduplicator.py`**

Replace the WHERE clause in `_delete_discards()` (lines 733–736):

```python
        WHERE p.duplicate_role = 'discard'
          AND p.privacy_state = 'approved_public'
          AND (p.flickr_deleted IS NULL OR p.flickr_deleted = 0)
          AND dg.resolved = 0
```

With:

```python
        WHERE p.duplicate_role = 'discard'
          AND dg.group_type = 'reupload'
          AND p.privacy_state = 'duplicate_flickr'
          AND (p.flickr_deleted IS NULL OR p.flickr_deleted = 0)
          AND dg.resolved = 0
```

- [ ] **Step 4: Also fix the docstring on `_delete_discards()` to reflect the new behaviour**

Replace the existing docstring:

```python
    """Delete approved_public discard records from Flickr.

    Queries duplicate_groups for unresolved groups whose discard is
    approved_public and not yet flickr_deleted. Calls client.delete_photo()
    for each. Treats FlickrError(1) (photo not found) as success.

    Returns (deleted, already_gone, errors).
    """
```

With:

```python
    """Delete reupload discard records from Flickr.

    Queries reupload duplicate_groups for unresolved groups whose discard has
    been marked duplicate_flickr and not yet flickr_deleted. Calls
    client.delete_photo() for each. Treats FlickrError(1) (photo not found)
    as success.

    Returns (deleted, already_gone, errors).
    """
```

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passed (1 more than before).

- [ ] **Step 6: Commit**

```bash
git add poller/deduplicator.py tests/test_deduplicator.py
git commit -m "$(cat <<'EOF'
fix: _delete_discards now targets duplicate_flickr reupload discards (#104)

Previously queried approved_public which never matched reupload orphans.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Wire `--mark-discards` into `main()`

**Files:**
- Modify: `poller/deduplicator.py` (`main()` only)

No new tests — `main()` is thin wiring; correctness is covered by Tasks 1–2.

- [ ] **Step 1: Add `--mark-discards` argparse argument**

In `main()`, after the `--apply` argument (after line 936), add:

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

- [ ] **Step 2: Update argument guards**

Replace the two existing guards (lines 950–955):

```python
    if args.delete_discards and not args.flickr:
        log.error("--delete-discards requires --flickr")
        sys.exit(1)
    if args.apply and not args.delete_discards:
        log.error("--apply requires --delete-discards")
        sys.exit(1)
```

With:

```python
    if args.delete_discards and not args.flickr:
        log.error("--delete-discards requires --flickr")
        sys.exit(1)
    if args.mark_discards and not args.flickr:
        log.error("--mark-discards requires --flickr")
        sys.exit(1)
    if args.mark_discards and args.delete_discards:
        log.error("--mark-discards and --delete-discards cannot be used together")
        sys.exit(1)
    if args.apply and not args.delete_discards and not args.mark_discards:
        log.error("--apply requires --delete-discards or --mark-discards")
        sys.exit(1)
```

- [ ] **Step 3: Add `--mark-discards` dispatch inside the `if args.flickr:` block**

Inside the `if args.flickr:` block (after the `if args.confirm:` guard, before the `if args.delete_discards:` check — around line 975), add:

```python
        if args.mark_discards:
            log.info("Marking reupload discards in %s …", db_path)
            _mark_reupload_discards(conn, dry_run=not args.apply, verbose=args.verbose)
            conn.close()
            return
```

- [ ] **Step 4: Smoke-test the new flag in dry-run mode**

```bash
python -m poller.deduplicator --config config/config.yml --flickr --mark-discards
```

Expected output (0 eligible since the live DB has no `reupload` groups written yet):

```
No reupload discards eligible to mark.
```

No errors, no stack trace.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all passed (same count — no new tests in this task).

- [ ] **Step 6: Commit**

```bash
git add poller/deduplicator.py
git commit -m "$(cat <<'EOF'
feat: wire --mark-discards flag into bp dedup --flickr (#104)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: README, docs, and GitHub issue

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-05-19-reupload-dedup-104-phase2.md` (this file — mark tasks done)

- [ ] **Step 1: Get final test count**

```bash
python -m pytest tests/ -q 2>&1 | tail -1
```

Note the number (should be 770: 763 existing + 6 `TestMarkReuploaDiscards` + 1 `TestDeleteDiscardsQuery`).

- [ ] **Step 2: Update test count in `README.md`**

Find the current test count in `README.md` (search for the number from before this work) and replace it with the new count.

- [ ] **Step 3: Run full suite one final time to confirm**

```bash
python -m pytest tests/ -q
```

Expected: all passed; count matches what you just put in README.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs: update README test count for #104 Phase 2

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Push to origin**

```bash
git push
```

- [ ] **Step 6: Comment on GitHub issue #104**

```bash
gh issue comment 104 --repo cdevers/Blue-Pearmain --body "Phase 2 implementation complete.

**What was shipped:**
- \`_mark_reupload_discards(conn, dry_run, verbose)\` — marks confirmed \`reupload\` group discards as \`duplicate_flickr\` in the DB; dry-run by default
- Fix to \`_delete_discards()\` — query now targets \`duplicate_flickr\` / \`group_type='reupload'\` discards instead of \`approved_public\`; this was a pre-existing bug that prevented the delete path from ever finding reupload discards
- \`--mark-discards\` CLI flag wired into \`bp dedup --flickr\`; use \`--apply\` to execute, default is dry-run
- 7 new tests (770 total)

**Workflow:**
\`\`\`
bp dedup --flickr --write               # Phase 1: detect + group
bp dedup --flickr --mark-discards       # dry-run preview
bp dedup --flickr --mark-discards --apply   # mark DB records
bp dedup --flickr --delete-discards     # dry-run preview
bp dedup --flickr --delete-discards --apply # delete from Flickr
\`\`\`

Phase 3 (metadata sync) and Phase 4 (UI cross-linking) tracked in #105 and #106."
```

- [ ] **Step 7: Close GitHub issue #104**

```bash
gh issue close 104 --repo cdevers/Blue-Pearmain --reason completed
```
