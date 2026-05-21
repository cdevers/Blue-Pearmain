# `bp reconcile --explain` — Current→Desired→Reason Output — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--explain` flag to `bp reconcile` that shows, for each photo with pending drift, the last-known Flickr state, the desired state, and the reason for the discrepancy — all from DB cache only, with no live Flickr API calls.

**Architecture:** A new `poller/explain.py` module contains pure functions that build per-photo explanation dicts from DB row data. `reconcile.py` gains an `--explain` mode that queries pushed photos, calls these functions, and prints the formatted output. `bp` exposes `--explain` on the `reconcile` subparser. `--explain` implies read-only (no Flickr API calls, no writes).

**Tech Stack:** Python stdlib only. Reads `photos`, `metadata_proposals` tables. No new DB tables or migrations.

---

## File map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `poller/explain.py` | Pure explain functions (tag discrepancy, perm discrepancy, text formatting) |
| Create | `tests/test_explain.py` | Unit tests for all explain functions |
| Modify | `poller/reconcile.py` | Add `--explain` flag and explain mode in `main()` |
| Modify | `bp` | Add `--explain` to `reconcile` subparser and `cmd_reconcile` |
| Modify | `README.md` | Document `--explain`; update test count |

---

### Task 1 — Tag and permission explain functions

**Files:**
- Create: `poller/explain.py`
- Create: `tests/test_explain.py`

These are pure functions: they take a photo row dict (from the DB) and return an explanation dict or `None` if there is nothing to explain.

- [ ] **Step 1.1 — Write the failing tests**

Create `tests/test_explain.py`:

```python
"""
tests/test_explain.py — unit tests for poller.explain

Run from repo root:
    python -m pytest tests/test_explain.py -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from poller.explain import explain_photo_tags, explain_photo_perms


def _row(**kw) -> dict:
    """Return a minimal photo row with required fields, with optional overrides."""
    base: dict = {
        "id": 42,
        "flickr_id": "99900001",
        "flickr_title": "Test Photo",
        "flickr_tags": '["beach", "family"]',
        "photos_tags": '["beach", "family"]',
        "pushed_tags": '["beach", "family"]',
        "privacy_state": "approved_public",
        "review_decision": "make_public",
        "reviewed_at": "2025-03-14T14:22:01",
        "perms_pushed_flickr": 1,
        "tags_pushed_flickr": 1,
    }
    base.update(kw)
    return base


class TestExplainPhotoTags(unittest.TestCase):

    def test_returns_none_when_tags_match(self):
        # flickr_tags == photos_tags — no drift to explain
        result = explain_photo_tags(_row())
        self.assertIsNone(result)

    def test_returns_dict_when_photos_has_extra_tag(self):
        # Photos has scanned-film; Flickr does not
        result = explain_photo_tags(_row(
            flickr_tags='["beach"]',
            photos_tags='["beach", "scanned-film"]',
        ))
        self.assertIsNotNone(result)

    def test_explains_tags_in_photos_not_on_flickr(self):
        result = explain_photo_tags(_row(
            flickr_tags='["beach"]',
            photos_tags='["beach", "scanned-film"]',
        ))
        self.assertIn("last_known_flickr", result)
        self.assertIn("desired", result)
        self.assertIn("reason", result)
        self.assertIn("scanned-film", result["reason"])

    def test_returns_none_when_flickr_tags_is_null_and_photos_tags_empty(self):
        result = explain_photo_tags(_row(flickr_tags=None, photos_tags=None, pushed_tags=None))
        self.assertIsNone(result)

    def test_reports_pushed_tags_that_disappeared_from_flickr(self):
        # We pushed "archive" but it is no longer in Flickr cache
        result = explain_photo_tags(_row(
            flickr_tags='["beach"]',
            photos_tags='["beach"]',
            pushed_tags='["beach", "archive"]',
        ))
        self.assertIsNotNone(result)
        self.assertIn("archive", result["reason"])

    def test_last_known_flickr_is_sorted_list(self):
        result = explain_photo_tags(_row(
            flickr_tags='["family", "beach"]',
            photos_tags='["beach", "family", "scanned-film"]',
        ))
        self.assertEqual(result["last_known_flickr"], ["beach", "family"])

    def test_desired_is_sorted_list(self):
        result = explain_photo_tags(_row(
            flickr_tags='["beach"]',
            photos_tags='["scanned-film", "beach"]',
        ))
        self.assertEqual(result["desired"], ["beach", "scanned-film"])


class TestExplainPhotoPerms(unittest.TestCase):

    def test_returns_none_when_perms_pushed_and_state_unchanged(self):
        # Pushed approved_public, push confirmed
        result = explain_photo_perms(_row(
            privacy_state="approved_public",
            perms_pushed_flickr=1,
            review_decision="make_public",
        ))
        self.assertIsNone(result)

    def test_returns_dict_when_perms_not_yet_pushed(self):
        result = explain_photo_perms(_row(
            privacy_state="approved_public",
            perms_pushed_flickr=0,
            review_decision="make_public",
        ))
        self.assertIsNotNone(result)

    def test_explains_unpushed_perms(self):
        result = explain_photo_perms(_row(
            privacy_state="approved_public",
            perms_pushed_flickr=0,
            review_decision="make_public",
        ))
        self.assertIn("desired", result)
        self.assertIn("reason", result)
        self.assertIn("not yet pushed", result["reason"])

    def test_returns_none_when_no_review_decision(self):
        # No decision yet — nothing to explain for perms
        result = explain_photo_perms(_row(
            privacy_state="needs_review",
            perms_pushed_flickr=0,
            review_decision=None,
        ))
        self.assertIsNone(result)

    def test_friends_only_state_labelled_correctly(self):
        result = explain_photo_perms(_row(
            privacy_state="approved_friends",
            perms_pushed_flickr=0,
            review_decision="make_friends",
        ))
        self.assertIn("friends-only", result["desired"])
```

- [ ] **Step 1.2 — Run to confirm failure**

```bash
python -m pytest tests/test_explain.py::TestExplainPhotoTags tests/test_explain.py::TestExplainPhotoPerms -v
```

Expected: `ImportError: No module named 'poller.explain'`

- [ ] **Step 1.3 — Create `poller/explain.py` with explain functions**

```python
"""
poller/explain.py — DB-only explain logic for bp reconcile --explain

All functions are pure: they take dicts (from DB rows or queries) and
return explanation dicts or formatted strings. No Flickr API calls.
No side effects.
"""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_loads_safe(value: str | None) -> list:
    """Return parsed JSON list, or [] on None/error."""
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


_STATE_LABEL: dict[str, str] = {
    "approved_public":        "public",
    "approved_friends":       "friends-only",
    "approved_family":        "family-only",
    "approved_friends_family": "friends & family",
    "keep_private":           "private",
    "auto_private":           "private (auto)",
}


# ---------------------------------------------------------------------------
# Per-field explain functions
# ---------------------------------------------------------------------------


def explain_photo_tags(row: dict) -> dict | None:
    """
    Return a tag explanation dict, or None if there is nothing to explain.

    Keys:
        last_known_flickr — sorted list of tags in the DB Flickr cache
        desired           — sorted list of tags from Apple Photos
        reason            — human-readable explanation of the discrepancy
    """
    flickr_tags = set(t.lower().strip() for t in _json_loads_safe(row.get("flickr_tags")) if t.strip())
    photos_tags = set(t.lower().strip() for t in _json_loads_safe(row.get("photos_tags")) if t.strip())
    pushed_tags = set(t.lower().strip() for t in _json_loads_safe(row.get("pushed_tags")) if t.strip())

    if not flickr_tags and not photos_tags and not pushed_tags:
        return None

    # Tags in Photos but not yet on Flickr
    to_push = photos_tags - flickr_tags
    # Tags we pushed that are no longer in the Flickr cache
    disappeared = pushed_tags - flickr_tags

    if not to_push and not disappeared:
        return None

    reasons: list[str] = []
    if to_push:
        tag_list = ", ".join(sorted(to_push))
        reasons.append(f"in Photos but not on Flickr (not yet pushed): {tag_list}")
    if disappeared:
        tag_list = ", ".join(sorted(disappeared))
        reasons.append(f"previously pushed but missing from Flickr cache: {tag_list}")

    return {
        "last_known_flickr": sorted(flickr_tags),
        "desired": sorted(photos_tags),
        "reason": "; ".join(reasons),
    }


def explain_photo_perms(row: dict) -> dict | None:
    """
    Return a permission explanation dict, or None if there is nothing to explain.

    Keys:
        desired — human-readable desired permission label
        reason  — explanation of why the push has not happened
    """
    review_decision = row.get("review_decision")
    if not review_decision:
        return None  # No decision yet — nothing to explain

    privacy_state = row.get("privacy_state", "")
    perms_pushed = bool(row.get("perms_pushed_flickr"))

    if perms_pushed:
        return None  # Push confirmed — no unpushed drift to explain

    desired = _STATE_LABEL.get(privacy_state, privacy_state)
    reviewed_at = row.get("reviewed_at") or "unknown date"

    return {
        "desired": desired,
        "reason": (
            f"review decision ({review_decision}, {reviewed_at}) "
            "not yet pushed to Flickr"
        ),
    }
```

- [ ] **Step 1.4 — Run to confirm all pass**

```bash
python -m pytest tests/test_explain.py::TestExplainPhotoTags tests/test_explain.py::TestExplainPhotoPerms -v
```

Expected: `12 passed`

- [ ] **Step 1.5 — Commit**

```bash
git add poller/explain.py tests/test_explain.py
git commit -m "feat: add explain_photo_tags and explain_photo_perms to poller/explain.py (GH #117)"
```

---

### Task 2 — `format_explain_text` and `run_explain`

**Files:**
- Modify: `poller/explain.py`
- Modify: `tests/test_explain.py`

`format_explain_text` renders a list of per-photo explanation dicts as a human-readable string. `run_explain` queries the DB for photos with pending drift and builds the explanation list.

- [ ] **Step 2.1 — Write the failing tests**

Append to `tests/test_explain.py`:

```python
import os
import tempfile

from db.db import Database
from poller.explain import format_explain_text, run_explain


def _make_db() -> Database:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Database(Path(f.name))


class TestFormatExplainText(unittest.TestCase):

    def _sample_explanation(self) -> dict:
        return {
            "photo_id": 42,
            "flickr_id": "99900001",
            "title": "Beach trip 2019",
            "perms": None,
            "tags": {
                "last_known_flickr": ["beach"],
                "desired": ["beach", "scanned-film"],
                "reason": "in Photos but not on Flickr (not yet pushed): scanned-film",
            },
        }

    def test_output_contains_photo_title(self):
        out = format_explain_text([self._sample_explanation()], flickr_username="testuser")
        self.assertIn("Beach trip 2019", out)

    def test_output_contains_flickr_url(self):
        out = format_explain_text([self._sample_explanation()], flickr_username="testuser")
        self.assertIn("99900001", out)
        self.assertIn("testuser", out)

    def test_output_contains_tags_section(self):
        out = format_explain_text([self._sample_explanation()], flickr_username="testuser")
        self.assertIn("tags", out)
        self.assertIn("scanned-film", out)

    def test_empty_list_returns_no_drift_message(self):
        out = format_explain_text([], flickr_username="testuser")
        self.assertIn("No drift", out)

    def test_perm_section_shown_when_present(self):
        exp = self._sample_explanation()
        exp["perms"] = {"desired": "public", "reason": "not yet pushed to Flickr"}
        out = format_explain_text([exp], flickr_username="testuser")
        self.assertIn("permissions", out)
        self.assertIn("public", out)


class TestRunExplain(unittest.TestCase):

    def test_returns_empty_list_for_empty_db(self):
        db = _make_db()
        result = run_explain(db, limit=100, flickr_username="testuser")
        db.close()
        self.assertEqual(result, [])
```

- [ ] **Step 2.2 — Run to confirm failure**

```bash
python -m pytest tests/test_explain.py::TestFormatExplainText tests/test_explain.py::TestRunExplain -v
```

Expected: `cannot import name 'format_explain_text'`

- [ ] **Step 2.3 — Implement `format_explain_text` and `run_explain` in `poller/explain.py`**

Append to `poller/explain.py`:

```python
# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_explain_text(explanations: list[dict], flickr_username: str) -> str:
    """
    Render a list of per-photo explanation dicts as a human-readable string.

    Each dict must have keys: photo_id, flickr_id, title, perms, tags.
    perms and tags are the dicts returned by explain_photo_perms/tags, or None.
    """
    if not explanations:
        return "\nNo drift found in DB cache — everything looks consistent.\n"

    lines: list[str] = [""]
    for exp in explanations:
        title = exp.get("title") or f"Photo {exp['photo_id']}"
        fid = exp.get("flickr_id") or ""
        url = f"https://www.flickr.com/photos/{flickr_username}/{fid}" if fid else "(no Flickr ID)"
        lines.append(f'Photo {exp["photo_id"]} — "{title}"  [{url}]')
        lines.append("")

        if exp.get("perms"):
            p = exp["perms"]
            lines.append("  permissions")
            lines.append(f"    desired:       {p['desired']}")
            lines.append(f"    reason:        {p['reason']}")
            lines.append("")

        if exp.get("tags"):
            t = exp["tags"]
            flickr_str = ", ".join(t["last_known_flickr"]) or "(none)"
            desired_str = ", ".join(t["desired"]) or "(none)"
            lines.append("  tags")
            lines.append(f"    last-known Flickr:  {flickr_str}")
            lines.append(f"    desired (Photos):   {desired_str}")
            lines.append(f"    reason:             {t['reason']}")
            lines.append("")

        lines.append("─" * 60)
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------


def run_explain(db: "Database", limit: int, flickr_username: str) -> list[dict]:
    """
    Query photos with pending drift (from DB cache) and return explanation dicts.

    Only reads from DB — no Flickr API calls.
    Returns a list of explanation dicts, one per photo with something to explain.
    """
    from typing import TYPE_CHECKING

    rows = db.conn.execute(
        """SELECT id, flickr_id, flickr_title,
                  flickr_tags, photos_tags, pushed_tags,
                  privacy_state, review_decision, reviewed_at,
                  perms_pushed_flickr, tags_pushed_flickr
           FROM photos
           WHERE flickr_id IS NOT NULL
             AND (perms_pushed_flickr = 1 OR tags_pushed_flickr = 1)
             AND (flickr_deleted IS NULL OR flickr_deleted = 0)
           ORDER BY reviewed_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    results = []
    for row in rows:
        r = dict(row)
        perms_exp = explain_photo_perms(r)
        tags_exp = explain_photo_tags(r)

        if perms_exp or tags_exp:
            results.append(
                {
                    "photo_id": r["id"],
                    "flickr_id": r.get("flickr_id"),
                    "title": r.get("flickr_title") or "",
                    "perms": perms_exp,
                    "tags": tags_exp,
                }
            )

    return results
```

We also need to add the `TYPE_CHECKING` import at the top of the file. Add this after `import json`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db.db import Database
```

- [ ] **Step 2.4 — Run to confirm all pass**

```bash
python -m pytest tests/test_explain.py -v
```

Expected: all pass (18+ tests)

- [ ] **Step 2.5 — Commit**

```bash
git add poller/explain.py tests/test_explain.py
git commit -m "feat: add format_explain_text and run_explain to poller/explain.py (GH #117)"
```

---

### Task 3 — Wire `--explain` into `reconcile.py` and `bp`

**Files:**
- Modify: `poller/reconcile.py`
- Modify: `bp`

- [ ] **Step 3.1 — Write the failing test**

Add to `tests/test_explain.py`:

```python
import subprocess as proc


class TestBpReconcileExplain(unittest.TestCase):
    """Smoke-test bp reconcile --explain via the CLI entry point."""

    def test_bp_reconcile_explain_exits_without_crashing_when_no_config(self):
        result = proc.run(
            ["python", "bp", "reconcile", "--explain",
             "--config", "/nonexistent/config.yml"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotEqual(result.returncode, None)
```

- [ ] **Step 3.2 — Run to confirm failure**

```bash
python -m pytest tests/test_explain.py::TestBpReconcileExplain -v
```

Expected: fails because `--explain` is not a recognised argument for `reconcile`.

- [ ] **Step 3.3 — Add `--explain` mode to `poller/reconcile.py`**

In `poller/reconcile.py`, add `--explain` to the argument parser in `main()`:

```python
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Show current→desired→reason for each photo with pending drift (DB-only, no Flickr calls)",
    )
```

Add the explain mode branch at the start of `main()`, after loading config and DB but **before** the Flickr auth block:

```python
    # --explain: DB-only drift explanation, no Flickr API calls
    if args.explain:
        from poller.explain import format_explain_text, run_explain

        flickr_username = config.get("flickr", {}).get("username") or config.get(
            "flickr", {}).get("user_nsid", "unknown")
        limit = args.limit or 500
        explanations = run_explain(db, limit=limit, flickr_username=flickr_username)
        print(format_explain_text(explanations, flickr_username=flickr_username))
        db.close()
        return 0
```

This must be inserted **before** the `try: client = FlickrClient.from_config(config)` block so that `--explain` never reaches the auth code.

- [ ] **Step 3.4 — Add `--explain` to the `reconcile` subparser in `bp`**

In `bp`, in the `reconcile` subparser block, add:

```python
    p_rec.add_argument(
        "--explain",
        action="store_true",
        help="Show current→desired→reason for each photo with pending drift (DB-only)",
    )
```

Update `cmd_reconcile` to pass `--explain`:

```python
def cmd_reconcile(args):
    from poller.reconcile import main
    _run(main, args, [
        ("--config",          args.config),
        ("--fix",             args.fix),
        ("--apply-proposals", args.apply_proposals),
        ("--explain",         args.explain),
        ("--limit",           str(args.limit) if args.limit is not None else None),
        ("--verbose",         args.verbose),
    ])
```

Add to the attribute-guard block after `args = parser.parse_args()`:

```python
    if not hasattr(args, "explain"):       args.explain = False
```

- [ ] **Step 3.5 — Run the smoke test**

```bash
python -m pytest tests/test_explain.py::TestBpReconcileExplain -v
```

Expected: `1 passed`

- [ ] **Step 3.6 — Run the full test suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass

- [ ] **Step 3.7 — Run lint**

```bash
make lint
```

Expected: no errors. Fix any formatting issues with:
```bash
uv run --with ruff ruff format poller/explain.py poller/reconcile.py bp
```

- [ ] **Step 3.8 — Commit**

```bash
git add poller/explain.py poller/reconcile.py bp tests/test_explain.py
git commit -m "feat: add bp reconcile --explain — DB-only drift explainer (GH #117)"
```

---

### Task 4 — Update docs and README

**Files:**
- Modify: `README.md`
- Modify: `docs/future-directions.md`

- [ ] **Step 4.1 — Add `--explain` to the README**

In `README.md`, in the `## Running` section, update the `bp reconcile` entry to show the new flag:

```
bp reconcile --explain             # Show why each pushed photo has drift (DB-only, no Flickr calls)
```

Update the test count in `README.md`:
```bash
python -m pytest tests/ -q
```
Use the final number reported.

- [ ] **Step 4.2 — Mark #117 done in future-directions.md**

In `docs/future-directions.md`, update the reconcile--explain heading:

```markdown
### `bp reconcile --explain` ([#117](https://github.com/cdevers/Blue-Pearmain/issues/117)) `size:M` · ✓ done
```

- [ ] **Step 4.3 — Commit and push**

```bash
git add README.md docs/future-directions.md
git commit -m "docs: update README and roadmap for bp reconcile --explain (Closes #117)"
git push
```
