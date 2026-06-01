"""
db.py — database access layer for flickr-curator

Wraps SQLite via the standard library. All methods return plain dicts
or lists of dicts rather than row objects, to keep things portable and
easy to serialise.

Usage:
    from db import Database
    db = Database("/mnt/nas/flickr-curator/curator.db")
    db.upsert_photo({...})
"""

import json
import math
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _json_loads_safe(value: str | None) -> list:
    """Return parsed JSON list, or empty list on None/error."""
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _canonical_tag_list(tags: Iterable[object]) -> list[str]:
    """Return the canonical stored form for a tag collection.

    Output invariants: every element is a non-blank string; no duplicates;
    sorted lexicographically; whitespace-stripped. Non-strings are dropped
    (not coerced — None does not become "none", 1 does not become "1").

    Ownership boundary: storage shape ONLY. This function does not perform
    semantic normalization (lowercase, remap, blocklist) — that is the
    caller's contract (analyzer.tagger.propose_tags). db.py never imports
    analyzer. This is the single authoritative definition shared by
    _decode_proposed_tags (reader) and apply_legacy_metadata (writer).
    """
    seen: set[str] = set()
    result: list[str] = []
    for item in tags:
        if isinstance(item, str):
            s = item.strip()
            if s and s not in seen:
                seen.add(s)
                result.append(s)
    return sorted(result)


def _decode_proposed_tags(raw: str | None) -> tuple[list[str], bool]:
    """Decode a stored proposed_tags JSON string to (tags, was_malformed).

    Canonical stored form is defined by _canonical_tag_list (sorted,
    de-duplicated, stripped, non-blank strings). Returns that canonical list
    plus was_malformed=True iff the raw decoded value was not already
    canonical, so any non-canonical row is rewritten ("repaired") on the
    next write:
      - NULL/blank     -> ([], False)
      - decode error   -> ([], True)
      - non-list JSON  -> ([], True)   (bare string, dict, number)
      - list with junk -> canonicalize via _canonical_tag_list; flag True if
                          anything was dropped/trimmed/de-duped/re-ordered.
    Non-string, whitespace, blank, duplicate, AND order deviations all count
    as repair, so tags_repaired is one honest signal: "stored value did not
    match the canonical sorted form".
    """
    if not raw:
        return [], False
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [], True
    if not isinstance(val, list):
        return [], True
    cleaned = _canonical_tag_list(val)
    return cleaned, cleaned != val


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in metres between two lat/lon points."""
    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()  # per-thread connection storage
        self._ensure_schema()

    # -----------------------------------------------------------------------
    # Connection management
    # -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA wal_autocheckpoint = 500")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Return a per-thread SQLite connection, opening one if needed."""
        if not getattr(self._local, "conn", None):
            self._local.conn = self._connect()
        return self._local.conn

    def close(self):
        """Close the calling thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None

    def checkpoint(self, mode: str = "TRUNCATE") -> dict:
        """
        Run a WAL checkpoint and return {busy, log, checkpointed}.
        TRUNCATE shrinks the WAL file to zero but requires no active readers.
        PASSIVE (the default SQLite behaviour) is safe with concurrent readers
        but leaves the WAL file in place.
        If busy > 0, some WAL frames couldn't be moved (readers still active).
        """
        if mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
            raise ValueError(f"invalid checkpoint mode: {mode!r}")
        row = self.conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        if row:
            return {"busy": row[0], "log": row[1], "checkpointed": row[2]}
        return {"busy": 0, "log": 0, "checkpointed": 0}

    # -----------------------------------------------------------------------
    # Late-linking: merge a Flickr-only record into a Photos-only record
    # -----------------------------------------------------------------------

    def merge_flickr_into_photos(self, flickr_rec_id: int, photos_rec_id: int) -> bool:
        """
        Late-link a Flickr-only record into an Apple-Photos-only record.

        Copies Flickr identity fields (flickr_id, flickr_secret, etc.) from
        the Flickr record into the Photos record, migrates album memberships,
        tag_events, and metadata_conflicts, copies any review decision if the
        Photos record has none, then deletes the Flickr record.

        Returns True if the merge completed, False if either record is missing
        or the preconditions aren't met (Photos record already has a flickr_id,
        or Flickr record already has a uuid).
        """
        flickr_row = self.conn.execute(
            "SELECT * FROM photos WHERE id = ?", (flickr_rec_id,)
        ).fetchone()
        photos_row = self.conn.execute(
            "SELECT * FROM photos WHERE id = ?", (photos_rec_id,)
        ).fetchone()

        if not flickr_row or not photos_row:
            return False

        flickr_row = dict(flickr_row)
        photos_row = dict(photos_row)

        # Sanity checks
        if photos_row.get("flickr_id"):
            return False  # Photos record already linked
        if flickr_row.get("uuid"):
            return False  # Flickr record already linked

        # 1. Migrate album memberships (INSERT OR IGNORE handles the case where
        #    both records somehow ended up in the same album already)
        albums = self.conn.execute(
            "SELECT album_id, flickr_pushed, pushed_at FROM photo_albums WHERE photo_id = ?",
            (flickr_rec_id,),
        ).fetchall()
        for a in albums:
            self.conn.execute(
                """INSERT OR IGNORE INTO photo_albums (photo_id, album_id, flickr_pushed, pushed_at)
                   VALUES (?, ?, ?, ?)""",
                (photos_rec_id, a["album_id"], a["flickr_pushed"], a["pushed_at"]),
            )

        # 2. Migrate tag_events — DELETE + re-INSERT rather than UPDATE to work
        #    around a SQLite 3.51.0 bug: UPDATE on a FK column raises
        #    "no such table: main.photos_old" when the parent table has columns
        #    added via ALTER TABLE with CHECK constraints.
        tag_rows = self.conn.execute(
            "SELECT event_at, destination, tags_before, tags_after, success, error "
            "FROM tag_events WHERE photo_id = ?",
            (flickr_rec_id,),
        ).fetchall()
        if tag_rows:
            self.conn.execute("DELETE FROM tag_events WHERE photo_id = ?", (flickr_rec_id,))
            for t in tag_rows:
                self.conn.execute(
                    """INSERT INTO tag_events
                       (photo_id, event_at, destination, tags_before, tags_after, success, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        photos_rec_id,
                        t["event_at"],
                        t["destination"],
                        t["tags_before"],
                        t["tags_after"],
                        t["success"],
                        t["error"],
                    ),
                )

        # 3. Migrate metadata_conflicts (INSERT OR IGNORE to skip field conflicts
        #    that already exist on the Photos record)
        conflicts = self.conn.execute(
            "SELECT * FROM metadata_conflicts WHERE photo_id = ?",
            (flickr_rec_id,),
        ).fetchall()
        for c in conflicts:
            self.conn.execute(
                """INSERT OR IGNORE INTO metadata_conflicts
                   (photo_id, field, flickr_value, photos_value,
                    resolved, resolution, resolved_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    photos_rec_id,
                    c["field"],
                    c["flickr_value"],
                    c["photos_value"],
                    c["resolved"],
                    c["resolution"],
                    c["resolved_at"],
                    c["created_at"],
                ),
            )

        # 4. Build the set of fields to copy into the Photos record
        update: dict[str, Any] = {}

        # Flickr identity fields — always copy from the Flickr record
        for field in (
            "flickr_id",
            "flickr_secret",
            "flickr_server",
            "flickr_farm",
            "date_uploaded_flickr",
            "perms_pushed_flickr",
            "tags_pushed_flickr",
        ):
            if flickr_row.get(field) is not None:
                update[field] = flickr_row[field]

        # Thumbnail: use Flickr thumbnail only if Photos record has none
        if not photos_row.get("thumbnail_path") and flickr_row.get("thumbnail_path"):
            update["thumbnail_path"] = flickr_row["thumbnail_path"]

        # Review decision: copy from Flickr record only if Photos record has none
        if not photos_row.get("review_decision") and flickr_row.get("review_decision"):
            for field in ("review_decision", "reviewed_at", "review_notes", "privacy_state"):
                if flickr_row.get(field) is not None:
                    update[field] = flickr_row[field]

        update["updated_at"] = _now_iso()

        # 5. Release the UNIQUE constraint on flickr_id before copying it.
        #    SQLite enforces UNIQUE per-row immediately, so we must clear the
        #    value on the source record before setting it on the target record.
        if "flickr_id" in update:
            self.conn.execute("UPDATE photos SET flickr_id = NULL WHERE id = ?", (flickr_rec_id,))

        if update:
            placeholders = ", ".join(f"{k} = ?" for k in update)
            self.conn.execute(
                f"UPDATE photos SET {placeholders} WHERE id = ?",
                list(update.values()) + [photos_rec_id],
            )

        # 6. Delete the Flickr-only record (ON DELETE CASCADE handles photo_albums)
        self.conn.execute("DELETE FROM photos WHERE id = ?", (flickr_rec_id,))
        self.conn.commit()
        return True

    _FLICKR_COPY_FIELDS: list[str] = [
        "flickr_id",
        "flickr_secret",
        "flickr_server",
        "flickr_farm",
        "date_uploaded_flickr",
        "tags_pushed_flickr",
        "perms_pushed_flickr",
        "flickr_deleted",
        "flickr_title",
        "flickr_description",
        "flickr_tags",
        "flickr_tags_hash",
        "flickr_last_updated",
        "meta_synced_flickr_at",
        "tags_truncated_for_flickr",
        "display_rotation",
    ]

    def merge_flickr_donor_in_group(self, donor_id: int, target_id: int, group_id: int) -> None:
        """
        Soft-merge a Flickr-only donor record into a Photos-linked target record.

        Copies all Flickr identity fields from donor → target, migrates
        photo_albums/tag_events/metadata_conflicts, then soft-deletes the donor
        (sets merged_into_id, privacy_state='duplicate_flickr', duplicate_role='discard')
        and resolves the duplicate group.

        Raises ValueError if preconditions are not met.
        """
        donor = self.conn.execute("SELECT * FROM photos WHERE id = ?", (donor_id,)).fetchone()
        target = self.conn.execute("SELECT * FROM photos WHERE id = ?", (target_id,)).fetchone()

        if not donor:
            raise ValueError(f"donor {donor_id} not found")
        if not donor["flickr_id"]:
            raise ValueError(f"donor {donor_id} has no flickr_id")
        if donor["uuid"] is not None:
            raise ValueError(
                f"donor {donor_id} has a uuid — only Flickr-only records can be donors"
            )
        if not target:
            raise ValueError(f"target {target_id} not found")
        if target["uuid"] is None:
            raise ValueError(
                f"target {target_id} has no uuid — only Photos-linked records can be targets"
            )
        if target["flickr_id"] is not None:
            raise ValueError(
                f"target {target_id} already has flickr_id '{target['flickr_id']}' — merge would overwrite it"
            )

        donor = dict(donor)

        # 1. Migrate album memberships
        albums = self.conn.execute(
            "SELECT album_id, flickr_pushed, pushed_at FROM photo_albums WHERE photo_id = ?",
            (donor_id,),
        ).fetchall()
        for a in albums:
            self.conn.execute(
                """INSERT OR IGNORE INTO photo_albums (photo_id, album_id, flickr_pushed, pushed_at)
                   VALUES (?, ?, ?, ?)""",
                (target_id, a["album_id"], a["flickr_pushed"], a["pushed_at"]),
            )

        # 2. Migrate tag_events — DELETE + re-INSERT to work around SQLite FK/ALTER TABLE bug
        tag_rows = self.conn.execute(
            "SELECT event_at, destination, tags_before, tags_after, success, error "
            "FROM tag_events WHERE photo_id = ?",
            (donor_id,),
        ).fetchall()
        if tag_rows:
            self.conn.execute("DELETE FROM tag_events WHERE photo_id = ?", (donor_id,))
            for t in tag_rows:
                self.conn.execute(
                    """INSERT INTO tag_events
                       (photo_id, event_at, destination, tags_before, tags_after, success, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        target_id,
                        t["event_at"],
                        t["destination"],
                        t["tags_before"],
                        t["tags_after"],
                        t["success"],
                        t["error"],
                    ),
                )

        # 3. Migrate metadata_conflicts
        conflicts = self.conn.execute(
            "SELECT * FROM metadata_conflicts WHERE photo_id = ?",
            (donor_id,),
        ).fetchall()
        for c in conflicts:
            self.conn.execute(
                """INSERT OR IGNORE INTO metadata_conflicts
                   (photo_id, field, flickr_value, photos_value,
                    resolved, resolution, resolved_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    target_id,
                    c["field"],
                    c["flickr_value"],
                    c["photos_value"],
                    c["resolved"],
                    c["resolution"],
                    c["resolved_at"],
                    c["created_at"],
                ),
            )

        # 4. Build set of Flickr fields to copy to target (skip nulls)
        update = {f: donor[f] for f in self._FLICKR_COPY_FIELDS if donor.get(f) is not None}
        update["updated_at"] = _now_iso()

        # 5. Clear flickr_id on donor BEFORE writing it to target (UNIQUE constraint)
        self.conn.execute("UPDATE photos SET flickr_id = NULL WHERE id = ?", (donor_id,))

        # 6. Copy Flickr fields to target
        if update:
            placeholders = ", ".join(f"{k} = ?" for k in update)
            self.conn.execute(
                f"UPDATE photos SET {placeholders} WHERE id = ?",
                list(update.values()) + [target_id],
            )

        # 7. Soft-delete donor
        self.conn.execute(
            """UPDATE photos
               SET merged_into_id = ?, privacy_state = 'duplicate_flickr', duplicate_role = 'discard'
               WHERE id = ?""",
            (target_id, donor_id),
        )

        # 8. Promote target role
        self.conn.execute("UPDATE photos SET duplicate_role = 'keeper' WHERE id = ?", (target_id,))

        # 9. Resolve the duplicate group
        self.conn.execute(
            """UPDATE duplicate_groups
               SET resolved = 1, keeper_id = ?, resolved_at = datetime('now')
               WHERE id = ?""",
            (target_id, group_id),
        )

        self.conn.commit()

    def _ensure_schema(self):
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            sql = schema_path.read_text()
            self.conn.executescript(sql)
            self.conn.commit()
        # Additive columns for existing DBs (schema.sql handles fresh installs)
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(photos)").fetchall()}
        if "display_rotation" not in existing:
            self.conn.execute(
                "ALTER TABLE photos ADD COLUMN display_rotation INTEGER NOT NULL DEFAULT 0"
            )
            self.conn.commit()
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(photos)").fetchall()}
        if "is_video" not in existing:
            self.conn.execute("ALTER TABLE photos ADD COLUMN is_video INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(photos)").fetchall()}
        if "bp_rating" not in existing:
            self.conn.execute("ALTER TABLE photos ADD COLUMN bp_rating INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(photos)").fetchall()}
        if "proposed_title" not in existing:
            self.conn.execute("ALTER TABLE photos ADD COLUMN proposed_title TEXT")
            self.conn.commit()
        pa_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(photo_albums)").fetchall()}
        if "removed_at" not in pa_cols:
            self.conn.execute("ALTER TABLE photo_albums ADD COLUMN removed_at TEXT")
            self.conn.commit()
        al_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(albums)").fetchall()}
        if "deleted_at" not in al_cols:
            self.conn.execute("ALTER TABLE albums ADD COLUMN deleted_at TEXT")
            self.conn.commit()
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "operation_log" not in tables:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS operation_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    photo_id    INTEGER REFERENCES photos(id),
                    operation   TEXT NOT NULL,
                    target      TEXT,
                    old_value   TEXT,
                    new_value   TEXT,
                    trigger     TEXT,
                    actor       TEXT NOT NULL DEFAULT 'bp'
                );
                CREATE INDEX IF NOT EXISTS idx_operation_log_photo
                    ON operation_log(photo_id);
                CREATE INDEX IF NOT EXISTS idx_operation_log_operation
                    ON operation_log(operation);
                CREATE INDEX IF NOT EXISTS idx_operation_log_occurred
                    ON operation_log(occurred_at);
            """)
            self.conn.commit()
        if "bulk_batches" not in tables:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS bulk_batches (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation   TEXT NOT NULL,
                    field       TEXT,
                    value       TEXT,
                    tags        TEXT,
                    filter      TEXT,  -- audit metadata only, not executable replay state
                    photo_count INTEGER NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)
            self.conn.commit()
        prop_cols = {
            r[1] for r in self.conn.execute("PRAGMA table_info(metadata_proposals)").fetchall()
        }
        if "batch_id" not in prop_cols:
            self.conn.execute(
                "ALTER TABLE metadata_proposals ADD COLUMN batch_id INTEGER REFERENCES bulk_batches(id)"
            )
            self.conn.commit()

    # -----------------------------------------------------------------------
    # Photo upsert — the main ingestion path
    # -----------------------------------------------------------------------

    def upsert_photo(self, data: dict[str, Any]) -> int:
        """
        Insert or update a photo record. Keyed on uuid (if present) or
        flickr_id (if present). Returns the row id.

        Caller is responsible for passing a dict with field names matching
        the schema. JSON fields (apple_labels, apple_persons, proposed_tags)
        should be passed as Python lists; this method serialises them.
        """
        # Serialise list fields
        for field in (
            "apple_labels",
            "apple_persons",
            "proposed_tags",
            "flickr_tags",
            "photos_tags",
        ):
            if isinstance(data.get(field), list):
                data[field] = json.dumps(data[field])

        data.setdefault("date_synced", _now_iso())
        data["updated_at"] = _now_iso()  # always stamp on every write

        # Determine lookup key — try uuid first, then flickr_id.
        # When both are present (scanner enriching a Flickr record), we must
        # check both to avoid a spurious INSERT hitting the unique constraint.
        existing = None
        lookup_field = None
        for field in ("uuid", "flickr_id"):
            value = data.get(field)
            if value:
                row = self.conn.execute(
                    f"SELECT id FROM photos WHERE {field} = ?", (value,)
                ).fetchone()
                if row:
                    existing = row
                    lookup_field = field
                    break

        if existing is None and not data.get("uuid") and not data.get("flickr_id"):
            raise ValueError("upsert_photo requires at least one of uuid or flickr_id")

        if lookup_field is None:
            lookup_field = "uuid" if data.get("uuid") else "flickr_id"

        if existing:
            row_id = existing["id"]

            # Check whether a human has ever made a review decision on this photo.
            # If so, never overwrite the privacy fields from a background sync pass.
            existing_full = self.conn.execute(
                "SELECT review_decision FROM photos WHERE id = ?", (row_id,)
            ).fetchone()
            already_reviewed = bool(existing_full and existing_full["review_decision"])

            update_data = {k: v for k, v in data.items() if k != lookup_field}
            if already_reviewed:
                for p in (
                    "privacy_state",
                    "privacy_reason",
                    "review_decision",
                    "reviewed_at",
                    "review_notes",
                ):
                    update_data.pop(p, None)

            placeholders = ", ".join(f"{k} = ?" for k in update_data)
            values = list(update_data.values()) + [row_id]
            self.conn.execute(f"UPDATE photos SET {placeholders} WHERE id = ?", values)
        else:
            columns = ", ".join(data.keys())
            placeholders = ", ".join("?" * len(data))
            values = list(data.values())
            cursor = self.conn.execute(
                f"INSERT INTO photos ({columns}) VALUES ({placeholders})", values
            )
            row_id = cursor.lastrowid

        self.conn.commit()
        return row_id

    # -----------------------------------------------------------------------
    # Privacy state transitions
    # -----------------------------------------------------------------------

    def set_privacy_state(self, photo_id: int, state: str, reason: str = ""):
        self.conn.execute(
            "UPDATE photos SET privacy_state = ?, privacy_reason = ?, date_synced = ?, updated_at = ? WHERE id = ?",
            (state, reason, _now_iso(), _now_iso(), photo_id),
        )
        self.conn.commit()

    def reclassify_legacy_match(
        self,
        photo_id: int,
        new_state: str,
        reason: str,
        *,
        trigger: str,
    ) -> None:
        """Atomically set privacy_state and append the audit row (one txn).

        `reason` and `trigger` are pre-formatted by the caller via
        legacy_match.format_legacy_reason / format_legacy_trigger — this method
        carries no format literal, so the two provenance strings stay in lockstep
        at their single source. Unlike log_operation (fire-and-forget), an
        audit-write failure here rolls the whole reclassification back — never a
        state change without its audit trail.
        """
        now = _now_iso()
        with self.conn:
            self.conn.execute(
                "UPDATE photos SET privacy_state = ?, privacy_reason = ?, "
                "date_synced = ?, updated_at = ? WHERE id = ?",
                (new_state, reason, now, now, photo_id),
            )
            self.conn.execute(
                "INSERT INTO operation_log "
                "(occurred_at, photo_id, operation, target, old_value, "
                " new_value, trigger, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now,
                    photo_id,
                    "match_legacy_apply",
                    "privacy_state",
                    "candidate_public",
                    new_state,
                    trigger,
                    "bp",
                ),
            )

    def apply_legacy_metadata(
        self,
        photo_id: int,
        *,
        add_tags: list[str],
        title: str | None = None,
        description: str | None = None,
        trigger: str,
    ) -> bool:
        """Stage propagated legacy metadata for one photo (one txn).

        Two-level normalization boundary:
          Semantic (caller's contract): add_tags must already be lowercased,
            remapped, and blocklist-filtered via analyzer.tagger.propose_tags.
            db.py does NOT import analyzer — that boundary is frozen.
          Storage shape (db's job): _canonical_tag_list handles trim/sort/
            dedupe/drop-invalid. Both the reader (_decode_proposed_tags) and
            this writer use it, so they always agree on canonical form.

        proposed_tags: set-union of add_tags into the photo's existing tags
        (decoded via _decode_proposed_tags; non-list/malformed values are
        repaired in place). proposed_title / proposed_description: filled only
        when currently empty (NULL or whitespace-only) and the incoming value is
        non-empty after stripping — never clobbers a human draft, never stages a
        whitespace-only value. Stored stripped. Writes ONE aggregate
        operation_log row iff something changed; new_value.fields is in schema
        order, tags_added is the delta, tags_repaired flags a repaired malformed
        row. Returns True iff anything changed.

        Concurrency: this does a read -> compute -> write that is NOT isolated
        against another writer mutating the same row between the SELECT and the
        UPDATE. That is safe under BP's single-writer model — `match-legacy
        --apply` is a one-shot CLI command, not run concurrently with another
        writer to `photos`. The write itself (UPDATE + operation_log INSERT) is
        atomic via the single `with self.conn:` transaction.
        """
        row = self.conn.execute(
            "SELECT proposed_tags, proposed_title, proposed_description FROM photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
        current, malformed = _decode_proposed_tags(row["proposed_tags"])
        merged = _canonical_tag_list(list(current) + list(add_tags))
        tags_changed = merged != current or malformed

        # Scalar hygiene at the persistence boundary: treat whitespace-only
        # incoming title/description as empty, and store the stripped form.
        # (stdlib only — no analyzer coupling.)
        title_in = (title or "").strip()
        desc_in = (description or "").strip()

        sets: list[str] = []
        params: list = []
        changed_fields: list[str] = []
        if tags_changed:
            changed_fields.append("proposed_tags")
            sets.append("proposed_tags = ?")
            params.append(json.dumps(merged))
        if title_in and not (row["proposed_title"] or "").strip():
            changed_fields.append("proposed_title")
            sets.append("proposed_title = ?")
            params.append(title_in)
        if desc_in and not (row["proposed_description"] or "").strip():
            changed_fields.append("proposed_description")
            sets.append("proposed_description = ?")
            params.append(desc_in)

        if not changed_fields:
            return False

        new_value: dict = {
            "fields": [
                f
                for f in ("proposed_tags", "proposed_title", "proposed_description")
                if f in changed_fields
            ]
        }
        if tags_changed:
            # "new members introduced", not a length delta: current may contain
            # duplicates (dirty historical rows), so len(merged)-len(current) can
            # be wrong or negative. Count tags in merged that weren't in current.
            added = len(set(merged) - set(current))
            if added > 0:
                new_value["tags_added"] = added
            if malformed:
                new_value["tags_repaired"] = True

        now = _now_iso()
        with self.conn:
            self.conn.execute(
                f"UPDATE photos SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                (*params, now, photo_id),
            )
            self.conn.execute(
                "INSERT INTO operation_log "
                "(occurred_at, photo_id, operation, target, old_value, "
                " new_value, trigger, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now,
                    photo_id,
                    "match_legacy_metadata",
                    "legacy_metadata",
                    None,
                    json.dumps(new_value),
                    trigger,
                    "bp",
                ),
            )
        return True

    # -----------------------------------------------------------------------
    # Star ratings
    # -----------------------------------------------------------------------

    def set_bp_rating(self, photo_id: int, rating: int) -> None:
        """Set bp_rating directly (from reviewer UI). Logs to operation_log."""
        row = self.conn.execute("SELECT bp_rating FROM photos WHERE id = ?", (photo_id,)).fetchone()
        old_rating = row["bp_rating"] if row else 0
        self.conn.execute("UPDATE photos SET bp_rating = ? WHERE id = ?", (rating, photo_id))
        self.conn.commit()
        self.log_operation(
            photo_id, "set_rating", "bp_rating", str(old_rating), str(rating), "reviewer_ui"
        )

    def get_photo_uuid(self, photo_id: int) -> str | None:
        """Return the Apple Photos UUID for the given DB row, or None."""
        row = self.conn.execute("SELECT uuid FROM photos WHERE id = ?", (photo_id,)).fetchone()
        return row["uuid"] if row else None

    def apply_scanner_rating(self, photo_id: int, apple_favorite: int) -> None:
        """Apply scanner sync policy for bp_rating. Logs changes to operation_log.

        Sync table (runs on every poll):
          favorite=True  + bp_rating=0   → set bp_rating=1   (seed from heart)
          favorite=True  + bp_rating>0   → no change          (already rated)
          favorite=False + bp_rating=0   → no change          (nothing to clear)
          favorite=False + bp_rating>0   → set bp_rating=0   (user un-hearted)
        """
        row = self.conn.execute("SELECT bp_rating FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if row is None:
            return
        old_rating = row["bp_rating"]

        if apple_favorite == 1 and old_rating == 0:
            new_rating = 1
        elif apple_favorite == 0 and old_rating > 0:
            new_rating = 0
        else:
            return  # no change

        self.conn.execute("UPDATE photos SET bp_rating = ? WHERE id = ?", (new_rating, photo_id))
        self.conn.commit()

        if new_rating == 1:
            self.log_operation(
                photo_id,
                "seed_rating_from_photos",
                "bp_rating",
                str(old_rating),
                str(new_rating),
                "scanner",
            )
        else:
            self.log_operation(
                photo_id,
                "clear_rating_from_photos",
                "bp_rating",
                str(old_rating),
                str(new_rating),
                "scanner",
            )

    def seed_flickr_rating(self, photo_id: int, flickr_rating: int) -> None:
        """Seed bp_rating from Flickr machine tag, only if currently unrated.

        BP is authoritative once a rating is set — Flickr tags are seed-only.
        Never overwrites an existing non-zero bp_rating.
        """
        if flickr_rating <= 0:
            return
        row = self.conn.execute("SELECT bp_rating FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if row is None or row["bp_rating"] != 0:
            return
        self.conn.execute("UPDATE photos SET bp_rating = ? WHERE id = ?", (flickr_rating, photo_id))
        self.conn.commit()
        self.log_operation(
            photo_id, "seed_rating_from_flickr", "bp_rating", "0", str(flickr_rating), "poller"
        )

    def record_review(self, photo_id: int, decision: str, notes: str = ""):
        """Record a human review decision and update privacy state accordingly."""
        state_map = {
            "make_public": "approved_public",
            "confirm_public": "already_public",
            "keep_private": "keep_private",
            "skip": "skipped",
            "make_friends": "approved_friends",
            "make_family": "approved_family",
            "make_friends_family": "approved_friends_family",
        }
        new_state = state_map.get(decision, "skipped")
        self.conn.execute(
            """UPDATE photos
               SET review_decision = ?, review_notes = ?, reviewed_at = ?,
                   privacy_state = ?, date_synced = ?, updated_at = ?
               WHERE id = ?""",
            (decision, notes, _now_iso(), new_state, _now_iso(), _now_iso(), photo_id),
        )
        self.conn.commit()

    # -----------------------------------------------------------------------
    # Geofence matching
    # -----------------------------------------------------------------------

    def active_zones(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM geofence_zones WHERE active = 1").fetchall()
        return [_row_to_dict(r) for r in rows]

    def match_geofence(self, lat: float | None, lon: float | None) -> dict | None:
        """
        Return the first active zone that contains (lat, lon), or None.
        Zones are checked in id order; add priority column later if needed.
        """
        if lat is None or lon is None:
            return None
        for zone in self.active_zones():
            dist = haversine_m(lat, lon, zone["latitude"], zone["longitude"])
            if dist <= zone["radius_m"]:
                return zone
        return None

    def upsert_zone(self, data: dict) -> int:
        data.setdefault("created_at", _now_iso())
        existing = self.conn.execute(
            "SELECT id FROM geofence_zones WHERE name = ?", (data["name"],)
        ).fetchone()
        if existing:
            row_id = existing["id"]
            update_data = {k: v for k, v in data.items() if k != "name"}
            placeholders = ", ".join(f"{k} = ?" for k in update_data)
            self.conn.execute(
                f"UPDATE geofence_zones SET {placeholders} WHERE id = ?",
                list(update_data.values()) + [row_id],
            )
        else:
            columns = ", ".join(data.keys())
            placeholders = ", ".join("?" * len(data))
            cursor = self.conn.execute(
                f"INSERT INTO geofence_zones ({columns}) VALUES ({placeholders})",
                list(data.values()),
            )
            row_id = cursor.lastrowid
        self.conn.commit()
        return row_id

    # -----------------------------------------------------------------------
    # Person policies
    # -----------------------------------------------------------------------

    def get_person_policies(self) -> dict[str, str]:
        """Return {person_name: policy} for all rows in person_policies."""
        try:
            rows = self.conn.execute("SELECT person_name, policy FROM person_policies").fetchall()
            return {r["person_name"]: r["policy"] for r in rows}
        except Exception:
            # Table absent (migration not yet applied): return empty dict
            return {}

    def set_person_policy(self, person_name: str, policy: str) -> None:
        """Insert or replace a policy for person_name."""
        now = _now_iso()
        self.conn.execute(
            """INSERT INTO person_policies (person_name, policy, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(person_name) DO UPDATE SET policy=excluded.policy""",
            (person_name, policy, now),
        )
        self.conn.commit()

    def delete_person_policy(self, person_name: str) -> None:
        """Remove any policy for person_name. No-op if none exists."""
        self.conn.execute("DELETE FROM person_policies WHERE person_name = ?", (person_name,))
        self.conn.commit()

    # -----------------------------------------------------------------------
    # Person birthdays
    # -----------------------------------------------------------------------

    def get_person_birthdays(self) -> dict[str, str]:
        """Return {person_name: birthday} for all rows in person_birthdays.

        birthday is stored as 'MM-DD' (recurring annual) or 'YYYY-MM-DD' (full known date).
        Returns an empty dict if the table does not yet exist.
        """
        try:
            rows = self.conn.execute(
                "SELECT person_name, birthday FROM person_birthdays"
            ).fetchall()
            return {r["person_name"]: r["birthday"] for r in rows}
        except Exception:
            return {}

    def set_person_birthday(self, person_name: str, birthday: str) -> None:
        """Upsert a birthday for person_name.

        birthday must be 'MM-DD' or 'YYYY-MM-DD'. No format validation here;
        callers are responsible for validation.
        """
        now = _now_iso()
        self.conn.execute(
            """INSERT INTO person_birthdays (person_name, birthday, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(person_name) DO UPDATE SET birthday=excluded.birthday, updated_at=excluded.updated_at""",
            (person_name, birthday, now, now),
        )
        self.conn.commit()

    def delete_person_birthday(self, person_name: str) -> None:
        """Remove the birthday for person_name. No-op if absent."""
        self.conn.execute("DELETE FROM person_birthdays WHERE person_name = ?", (person_name,))
        self.conn.commit()

    # -----------------------------------------------------------------------
    # Review queue queries
    # -----------------------------------------------------------------------

    def review_queue(
        self,
        states: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        exclude_screenshots: bool = False,
    ) -> list[dict]:
        """Return photos awaiting review, ordered newest-first.

        Fetches only the columns the review grid needs — avoids pulling large
        JSON metadata columns (flickr_tags, photos_tags, etc.) for every row.
        The idx_photos_review_queue index covers (privacy_state, date_taken DESC,
        id DESC), so SQLite can serve this query without a temp B-tree sort.
        """
        if states is None:
            states = ["needs_review", "candidate_public"]
        placeholders = ",".join("?" * len(states))
        screenshot_filter = " AND is_screenshot = 0" if exclude_screenshots else ""
        rows = self.conn.execute(
            f"""SELECT id, uuid, flickr_id, original_filename,
                       apple_unknown_faces, apple_named_faces, proposed_tags,
                       display_rotation, is_screenshot, updated_at,
                       geofence_zone, apple_persons, privacy_reason,
                       width, height, is_video, bp_rating
                FROM photos
                WHERE privacy_state IN ({placeholders}){screenshot_filter}
                ORDER BY date_taken DESC, id DESC
                LIMIT ? OFFSET ?""",
            states + [limit, offset],
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["proposed_tags"] = _json_loads_safe(d.get("proposed_tags"))
            d["apple_persons"] = _json_loads_safe(d.get("apple_persons"))
            result.append(d)
        return result

    def get_photo_nav(
        self,
        photo_id: int,
        state: str,
        date_taken: str | None,
        person_filter: str | None = None,
    ) -> tuple[int | None, int | None]:
        """Return (prev_id, next_id) for the per-photo detail view.

        Uses two single-row indexed lookups instead of a full window-function
        scan over the entire queue.  'prev' is the photo that appears earlier
        in the list (newer date_taken); 'next' appears later (older date_taken).
        """
        if not date_taken:
            return None, None

        if person_filter:
            prev_row = self.conn.execute(
                """SELECT DISTINCT photos.id FROM photos, json_each(photos.apple_persons) p
                   WHERE p.value = ? AND photos.privacy_state = ?
                     AND (photos.date_taken > ? OR (photos.date_taken = ? AND photos.id > ?))
                   ORDER BY photos.date_taken ASC, photos.id ASC LIMIT 1""",
                (person_filter, state, date_taken, date_taken, photo_id),
            ).fetchone()
            next_row = self.conn.execute(
                """SELECT DISTINCT photos.id FROM photos, json_each(photos.apple_persons) p
                   WHERE p.value = ? AND photos.privacy_state = ?
                     AND (photos.date_taken < ? OR (photos.date_taken = ? AND photos.id < ?))
                   ORDER BY photos.date_taken DESC, photos.id DESC LIMIT 1""",
                (person_filter, state, date_taken, date_taken, photo_id),
            ).fetchone()
        else:
            prev_row = self.conn.execute(
                """SELECT id FROM photos
                   WHERE privacy_state = ?
                     AND (date_taken > ? OR (date_taken = ? AND id > ?))
                   ORDER BY date_taken ASC, id ASC LIMIT 1""",
                (state, date_taken, date_taken, photo_id),
            ).fetchone()
            next_row = self.conn.execute(
                """SELECT id FROM photos
                   WHERE privacy_state = ?
                     AND (date_taken < ? OR (date_taken = ? AND id < ?))
                   ORDER BY date_taken DESC, id DESC LIMIT 1""",
                (state, date_taken, date_taken, photo_id),
            ).fetchone()

        return (
            prev_row["id"] if prev_row else None,
            next_row["id"] if next_row else None,
        )

    def review_queue_count(
        self, states: list[str] | None = None, exclude_screenshots: bool = False
    ) -> int:
        if states is None:
            states = ["needs_review", "candidate_public"]
        placeholders = ",".join("?" * len(states))
        screenshot_filter = " AND is_screenshot = 0" if exclude_screenshots else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM photos WHERE privacy_state IN ({placeholders}){screenshot_filter}",
            states,
        ).fetchone()
        return row["n"] if row else 0

    # -----------------------------------------------------------------------
    # Library view queries (bulk operations)
    # -----------------------------------------------------------------------

    _STATUS_STATES: dict[str, tuple[str, ...]] = {
        "public": ("already_public", "approved_public"),
        "friends": ("approved_friends",),
        "family": ("approved_family",),
        "friends_family": ("approved_friends_family",),
        "private": ("auto_private", "keep_private"),
        "pending": ("needs_review", "candidate_public"),
    }

    def _library_where(
        self,
        date_from: str | None,
        date_to: str | None,
        album_id: int | None,
        tag: str | None,
        status: str | None,
        untitled_only: bool,
        time_pattern: str | None = None,  # added by #142
        time_expand: int = 0,  # added by #142
        q: str | None = None,  # #141 text search
        country: str | None = None,  # #141 location cascade
        state: str | None = None,  # #141
        city: str | None = None,  # #141
        neighborhood: str | None = None,  # #141
        person: str | None = None,  # #141 person filter
        lat_min: float | None = None,  # #144 bbox
        lat_max: float | None = None,  # #144
        lon_min: float | None = None,  # #144
        lon_max: float | None = None,  # #144
        no_location: bool = False,  # #145 no_location filter
        confirmed_none: bool = False,  # #148 confirmed-none filter
    ) -> tuple[str, list]:
        """Return (WHERE clause fragment, params list) for library queries."""
        clauses: list[str] = ["p.flickr_deleted = 0"]
        params: list = []

        if date_from:
            clauses.append("p.date_taken >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("p.date_taken <= ?")
            params.append(date_to)
        if status and status in self._STATUS_STATES:
            states = self._STATUS_STATES[status]
            placeholders = ",".join("?" * len(states))
            clauses.append(f"p.privacy_state IN ({placeholders})")
            params.extend(states)
        if untitled_only:
            clauses.append(
                "(p.flickr_title IS NULL OR p.flickr_title = '') "
                "AND (p.photos_title IS NULL OR p.photos_title = '')"
            )
        if tag:
            clauses.append(
                "(EXISTS (SELECT 1 FROM json_each(p.flickr_tags) WHERE value = ?) "
                "OR EXISTS (SELECT 1 FROM json_each(p.photos_tags) WHERE value = ?))"
            )
            params.extend([tag, tag])
        if time_pattern:
            from db.time_patterns import parse_pattern, birthday_clause

            if time_pattern.startswith("birthday:"):
                person_name = time_pattern[9:]
                bday_rows = self.get_person_birthdays()
                bday = bday_rows.get(person_name)
                if bday:
                    all_years = self._distinct_years()
                    month, day = (int(x) for x in bday[-5:].split("-"))
                    frag, frag_params = birthday_clause(month, day, time_expand, all_years)
                    if frag != "1=1":
                        clauses.append(frag)
                        params.extend(frag_params)
            else:
                years = self._distinct_years() if time_pattern.startswith("holiday:") else []
                frag, frag_params = parse_pattern(time_pattern, time_expand, years)
                if frag != "1=1":
                    clauses.append(frag)
                    params.extend(frag_params)

        # #141 — text, location, person
        if q or country or state or city or neighborhood or person:
            from db.photo_filters import (
                build_text_clause,
                build_location_clause,
                build_person_clause,
            )

            if q:
                frag, frag_params = build_text_clause(q)
                clauses.append(frag)
                params.extend(frag_params)
            loc_sql, loc_params = build_location_clause(country, state, city, neighborhood)
            if loc_sql != "1=1":
                clauses.append(loc_sql)
                params.extend(loc_params)
            if person:
                frag, frag_params = build_person_clause(person)
                clauses.append(frag)
                params.extend(frag_params)

        # #145/#148 — no_location and confirmed_none are complementary but mutually exclusive
        if no_location and confirmed_none:
            raise ValueError("confirmed_none and no_location are mutually exclusive")

        # #145 — "No location" filter: untagged + not confirmed-none
        if no_location:
            clauses.append(
                "p.latitude IS NULL AND p.longitude IS NULL AND p.geo_confirmed_none = 0"
            )
            # Mutually exclusive with bbox — suppress it
            lat_min = lat_max = lon_min = lon_max = None

        # #148 — "Reviewed: no location" filter: confirmed-none photos
        if confirmed_none:
            clauses.append("p.geo_confirmed_none = 1")

        # #144 — spatial bounding box
        if (
            lat_min is not None
            and lat_max is not None
            and lon_min is not None
            and lon_max is not None
        ):
            from db.photo_filters import build_bbox_clause

            frag, frag_params = build_bbox_clause(lat_min, lat_max, lon_min, lon_max)
            clauses.append(frag)
            params.extend(frag_params)

        where = "WHERE " + " AND ".join(clauses)

        if album_id is not None:
            return where + " AND pa.album_id = ? AND pa.removed_at IS NULL", params + [album_id]

        return where, params

    def _distinct_years(self) -> list[int]:
        """Return all distinct calendar years present in photos.date_taken, sorted ascending."""
        rows = self.conn.execute(
            "SELECT DISTINCT CAST(strftime('%Y', date_taken) AS INTEGER) AS y "
            "FROM photos WHERE date_taken IS NOT NULL ORDER BY y"
        ).fetchall()
        return [r["y"] for r in rows if r["y"] is not None]

    def library_photos(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        album_id: int | None = None,
        tag: str | None = None,
        status: str | None = None,
        untitled_only: bool = False,
        time_pattern: str | None = None,
        time_expand: int = 0,
        q: str | None = None,
        country: str | None = None,
        state: str | None = None,
        city: str | None = None,
        neighborhood: str | None = None,
        person: str | None = None,
        lat_min: float | None = None,
        lat_max: float | None = None,
        lon_min: float | None = None,
        lon_max: float | None = None,
        no_location: bool = False,
        confirmed_none: bool = False,
        limit: int = 120,
        offset: int = 0,
    ) -> list[dict]:
        """Return photos for the library grid, newest first, with filters applied."""
        where, params = self._library_where(
            date_from=date_from,
            date_to=date_to,
            album_id=album_id,
            tag=tag,
            status=status,
            untitled_only=untitled_only,
            time_pattern=time_pattern,
            time_expand=time_expand,
            q=q,
            country=country,
            state=state,
            city=city,
            neighborhood=neighborhood,
            person=person,
            lat_min=lat_min,
            lat_max=lat_max,
            lon_min=lon_min,
            lon_max=lon_max,
            no_location=no_location,
            confirmed_none=confirmed_none,
        )
        join = "JOIN photo_albums pa ON pa.photo_id = p.id" if album_id is not None else ""
        rows = self.conn.execute(
            f"""SELECT p.id, p.flickr_id, p.uuid, p.original_filename,
                       p.thumbnail_path, p.date_taken, p.privacy_state,
                       p.flickr_title, p.photos_title,
                       p.flickr_tags, p.photos_tags,
                       p.is_video, p.width, p.height, p.bp_rating,
                       p.display_rotation, p.latitude, p.longitude, p.geo_confirmed_none
                FROM photos p {join}
                {where}
                ORDER BY p.date_taken DESC, p.id DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["flickr_tags"] = _json_loads_safe(d.get("flickr_tags"))
            d["photos_tags"] = _json_loads_safe(d.get("photos_tags"))
            result.append(d)
        return result

    def library_photo_count(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        album_id: int | None = None,
        tag: str | None = None,
        status: str | None = None,
        untitled_only: bool = False,
        time_pattern: str | None = None,
        time_expand: int = 0,
        q: str | None = None,
        country: str | None = None,
        state: str | None = None,
        city: str | None = None,
        neighborhood: str | None = None,
        person: str | None = None,
        lat_min: float | None = None,
        lat_max: float | None = None,
        lon_min: float | None = None,
        lon_max: float | None = None,
        no_location: bool = False,
        confirmed_none: bool = False,
    ) -> int:
        """Return total photo count for the given library filters."""
        where, params = self._library_where(
            date_from=date_from,
            date_to=date_to,
            album_id=album_id,
            tag=tag,
            status=status,
            untitled_only=untitled_only,
            time_pattern=time_pattern,
            time_expand=time_expand,
            q=q,
            country=country,
            state=state,
            city=city,
            neighborhood=neighborhood,
            person=person,
            lat_min=lat_min,
            lat_max=lat_max,
            lon_min=lon_min,
            lon_max=lon_max,
            no_location=no_location,
            confirmed_none=confirmed_none,
        )
        join = "JOIN photo_albums pa ON pa.photo_id = p.id" if album_id is not None else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM photos p {join} {where}", params
        ).fetchone()
        return row["n"] if row else 0

    def library_photo_ids(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        album_id: int | None = None,
        tag: str | None = None,
        status: str | None = None,
        untitled_only: bool = False,
        time_pattern: str | None = None,
        time_expand: int = 0,
        q: str | None = None,
        country: str | None = None,
        state: str | None = None,
        city: str | None = None,
        neighborhood: str | None = None,
        person: str | None = None,
        lat_min: float | None = None,
        lat_max: float | None = None,
        lon_min: float | None = None,
        lon_max: float | None = None,
        no_location: bool = False,
        confirmed_none: bool = False,
    ) -> list[int]:
        """Return all photo IDs matching the filters (no limit — used by bulk-edit)."""
        where, params = self._library_where(
            date_from=date_from,
            date_to=date_to,
            album_id=album_id,
            tag=tag,
            status=status,
            untitled_only=untitled_only,
            time_pattern=time_pattern,
            time_expand=time_expand,
            q=q,
            country=country,
            state=state,
            city=city,
            neighborhood=neighborhood,
            person=person,
            lat_min=lat_min,
            lat_max=lat_max,
            lon_min=lon_min,
            lon_max=lon_max,
            no_location=no_location,
            confirmed_none=confirmed_none,
        )
        join = "JOIN photo_albums pa ON pa.photo_id = p.id" if album_id is not None else ""
        rows = self.conn.execute(
            f"SELECT p.id FROM photos p {join} {where} ORDER BY p.id",
            params,
        ).fetchall()
        return [r["id"] for r in rows]

    def no_location_count(self) -> int:
        """Count photos with no geotag that have not been confirmed as intentionally-none."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM photos"
            " WHERE latitude IS NULL AND longitude IS NULL"
            "   AND geo_confirmed_none = 0"
            "   AND (flickr_deleted IS NULL OR flickr_deleted = 0)"
        ).fetchone()
        return row["n"] if row else 0

    def confirmed_none_count(self) -> int:
        """Count photos marked as intentionally having no location (geo_confirmed_none=1)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM photos"
            " WHERE geo_confirmed_none = 1"
            "   AND (flickr_deleted IS NULL OR flickr_deleted = 0)"
        ).fetchone()
        return row["n"] if row else 0

    def location_data(self) -> dict:
        """Return nested dict {country: {state: {city: [neighborhoods]}}} for non-deleted photos.
        Photos where place_country is NULL or empty are excluded.
        Empty-string neighborhoods are excluded from neighborhood lists.
        All levels sorted alphabetically."""
        rows = self.conn.execute(
            "SELECT place_country, place_state, place_city, place_neighborhood "
            "FROM photos "
            "WHERE flickr_deleted = 0 "
            "  AND place_country IS NOT NULL AND place_country != ''"
        ).fetchall()

        tree: dict = {}
        for r in rows:
            country = (r["place_country"] or "").strip()
            state = (r["place_state"] or "").strip()
            city = (r["place_city"] or "").strip()
            nbhd = (r["place_neighborhood"] or "").strip()
            if not country:
                continue
            tree.setdefault(country, {})
            tree[country].setdefault(state, {})
            tree[country][state].setdefault(city, set())
            if nbhd:
                tree[country][state][city].add(nbhd)

        return {
            c: {
                s: {ci: sorted(nbhds) for ci, nbhds in sorted(cities.items())}
                for s, cities in sorted(states.items())
            }
            for c, states in sorted(tree.items())
        }

    def person_names(self) -> list[str]:
        """Return distinct person names from apple_persons JSON arrays,
        excluding '_UNKNOWN_', sorted alphabetically."""
        rows = self.conn.execute(
            "SELECT DISTINCT j.value "
            "FROM photos p, json_each(p.apple_persons) j "
            "WHERE p.apple_persons IS NOT NULL "
            "  AND p.apple_persons NOT IN ('null', '[]', '') "
            "  AND j.value != '_UNKNOWN_' AND p.flickr_deleted = 0 "
            "ORDER BY j.value"
        ).fetchall()
        return [r["value"] for r in rows]

    def tag_names(self) -> list[str]:
        """Return distinct tag values from photos_tags JSON arrays, sorted alphabetically.

        Excludes blank/whitespace-only values that can appear when Apple Photos
        exports empty keyword entries. Source is photos_tags only (human-readable
        Apple Photos keywords); flickr_tags includes machine tags and is not used
        for the datalist.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT j.value "
            "FROM photos p, json_each(p.photos_tags) j "
            "WHERE p.photos_tags IS NOT NULL "
            "  AND p.photos_tags NOT IN ('null', '[]', '') "
            "  AND p.flickr_deleted = 0 "
            "  AND trim(j.value) != '' "
            "ORDER BY j.value"
        ).fetchall()
        return [r["value"] for r in rows]

    def get_all_albums(self) -> list[dict]:
        """Return all non-deleted albums ordered by name."""
        rows = self.conn.execute(
            """SELECT id, name, flickr_set_id
               FROM albums
               WHERE deleted_at IS NULL
               ORDER BY name""",
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_albums_with_counts(self) -> list[dict]:
        """Return all non-deleted albums with active photo membership counts, ordered by name."""
        rows = self.conn.execute(
            """SELECT a.id, a.name, a.flickr_set_id,
                      COUNT(pa.photo_id) AS photo_count
               FROM albums a
               LEFT JOIN photo_albums pa ON pa.album_id = a.id
                                         AND pa.removed_at IS NULL
               WHERE a.deleted_at IS NULL
               GROUP BY a.id
               ORDER BY a.name""",
        ).fetchall()
        return [dict(r) for r in rows]

    def rename_album(self, album_id: int, name: str) -> None:
        """Update the album's display name and timestamp.

        flickr_name is intentionally NOT updated here — it holds the last
        name successfully pushed to Flickr. After this call, name != flickr_name
        signals a pending rename to any tooling that inspects both columns.
        sync_album_titles() (called by bp sync-albums) pushes albums.name to
        the Flickr photoset title and then calls set_album_flickr_name() to
        bring flickr_name back in sync.

        updated_at is always written, even when the name is unchanged.
        sync_album_titles() (in bp sync-albums) pushes all album names
        unconditionally on each run, so a same-name rename causes no
        extra Flickr API churn beyond the normal sync invocation.

        Note: does not check deleted_at. The caller is responsible for
        confirming the album is not soft-deleted before calling this method.
        """
        self.conn.execute(
            "UPDATE albums SET name = ?, updated_at = ? WHERE id = ?",
            (name, _now_iso(), album_id),
        )
        self.conn.commit()

    def get_album_membership_for_photos(self, photo_ids: list[int]) -> dict[int, set[int]]:
        """
        Return {album_id: {photo_id, ...}} for all active memberships among the given photo_ids.
        Albums with no active membership among photo_ids are absent from the result.
        Used to show current membership state in the Add-to-album panel.
        Empty list input returns empty dict.
        """
        if not photo_ids:
            return {}
        placeholders = ",".join("?" * len(photo_ids))
        rows = self.conn.execute(
            f"""SELECT album_id, photo_id
                FROM photo_albums
                WHERE photo_id IN ({placeholders})
                  AND removed_at IS NULL""",
            photo_ids,
        ).fetchall()
        result: dict[int, set[int]] = {}
        for row in rows:
            result.setdefault(row["album_id"], set()).add(row["photo_id"])
        return result

    def bulk_upsert_photo_albums(self, photo_ids: list[int], album_id: int) -> int:
        """
        Add photo_ids to album_id without committing — caller must commit.
        Idempotent: already-active rows are no-ops (not counted).
        Tombstoned rows have removed_at cleared and are counted as re-activated.
        Returns count of newly inserted or re-activated rows.
        """
        if not photo_ids:
            return 0
        added = 0
        for photo_id in photo_ids:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO photo_albums (photo_id, album_id) VALUES (?, ?)",
                (photo_id, album_id),
            )
            if cur.rowcount > 0:
                added += 1
            else:
                cur2 = self.conn.execute(
                    "UPDATE photo_albums SET removed_at = NULL "
                    "WHERE photo_id = ? AND album_id = ? AND removed_at IS NOT NULL",
                    (photo_id, album_id),
                )
                added += cur2.rowcount
        return added

    def bulk_remove_photo_albums(self, photo_ids: list[int], album_id: int) -> int:
        """
        Tombstone photo_ids in album_id without committing — caller must commit.
        Only tombstones active (non-tombstoned) rows; already-tombstoned rows are no-ops.
        Returns count of newly tombstoned rows.
        """
        if not photo_ids:
            return 0
        removed = 0
        _now = _now_iso()
        for photo_id in photo_ids:
            cur = self.conn.execute(
                "UPDATE photo_albums SET removed_at = ? "
                "WHERE photo_id = ? AND album_id = ? AND removed_at IS NULL",
                (_now, photo_id, album_id),
            )
            removed += cur.rowcount
        return removed

    # -----------------------------------------------------------------------
    # Bulk operations
    # -----------------------------------------------------------------------

    def create_bulk_batch(
        self,
        operation: str,
        field: str | None,
        value: str | None,
        tags: list[str] | None,
        filter_json: str | None,
        photo_count: int,
    ) -> int:
        """Create a bulk_batches record and return the new batch_id."""
        cur = self.conn.execute(
            """INSERT INTO bulk_batches (operation, field, value, tags, filter, photo_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                operation,
                field,
                value,
                json.dumps(tags) if tags is not None else None,
                filter_json,
                photo_count,
                _now_iso(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def insert_bulk_proposals(
        self,
        batch_id: int,
        photo_ids: list[int],
        field: str,
        value: str | None = None,
        tags: list[str] | None = None,
        skip_existing: bool = False,
    ) -> int:
        """
        Insert metadata_proposals for the given photos.

        field must be one of: 'title', 'description', 'tags_add', 'tags_remove'.

        For 'tags_add' / 'tags_remove', proposed_value is the full new tag list
        (sorted JSON array), not the delta. Photos without a flickr_id are skipped.

        Returns count of proposals actually inserted.
        """
        if not photo_ids:
            return 0

        placeholders = ",".join("?" * len(photo_ids))
        rows = self.conn.execute(
            f"""SELECT id, flickr_id,
                       flickr_title, flickr_description,
                       flickr_tags, flickr_tags_hash,
                       photos_title
                FROM photos
                WHERE id IN ({placeholders}) AND flickr_id IS NOT NULL AND flickr_deleted = 0""",
            photo_ids,
        ).fetchall()

        created = 0
        now = _now_iso()

        for row in rows:
            photo_id = row["id"]
            db_field: str
            proposed_value: str

            if field == "title":
                db_field = "title"
                existing = (row["flickr_title"] or "").strip()
                if skip_existing and existing:
                    continue
                proposed_value = value or ""

            elif field == "description":
                db_field = "description"
                existing = (row["flickr_description"] or "").strip()
                if skip_existing and existing:
                    continue
                proposed_value = value or ""

            elif field == "tags_add":
                db_field = "tags"
                assert tags is not None
                current = _json_loads_safe(row["flickr_tags"])
                current_set = set(current)
                new_set = current_set | set(tags)
                if new_set == current_set:
                    continue  # all tags already present
                proposed_value = json.dumps(sorted(new_set))

            elif field == "tags_remove":
                db_field = "tags"
                assert tags is not None
                current = _json_loads_safe(row["flickr_tags"])
                remove_set = set(tags)
                new_list = sorted(t for t in current if t not in remove_set)
                if len(new_list) == len(current):
                    continue  # none of the tags were present
                proposed_value = json.dumps(new_list)

            else:
                raise ValueError(f"Unknown bulk field: {field!r}")

            # One INSERT per photo — O(N) and correct for BP's scale.
            # INSERT OR IGNORE respects the unique pending index:
            # (photo_id, field, proposed_value, target, source) WHERE status='pending'
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO metadata_proposals
                   (photo_id, field, proposed_value, source, target, conflict_type,
                    source_hash_at_creation, target_hash_at_creation,
                    status, created_at, batch_id)
                   VALUES (?, ?, ?, 'manual', 'flickr', 'non_conflict',
                           NULL, ?, 'pending', ?, ?)""",
                (
                    photo_id,
                    db_field,
                    proposed_value,
                    row["flickr_tags_hash"] if db_field == "tags" else None,
                    now,
                    batch_id,
                ),
            )
            if cur.rowcount:
                created += 1

        self.conn.commit()
        return created

    def get_pending_bulk_batches(self) -> list[dict]:
        """Return batches that have at least one pending proposal, newest first."""
        rows = self.conn.execute(
            """SELECT bb.id, bb.operation, bb.field, bb.value, bb.tags,
                      bb.photo_count, bb.created_at,
                      COUNT(mp.id) AS pending_count
               FROM bulk_batches bb
               JOIN metadata_proposals mp ON mp.batch_id = bb.id AND mp.status = 'pending'
               GROUP BY bb.id
               ORDER BY bb.id DESC"""
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("tags"):
                d["tags"] = _json_loads_safe(d["tags"])
            result.append(d)
        return result

    def reject_bulk_batch(self, batch_id: int) -> int:
        """Reject all pending proposals in a batch. Returns count rejected."""
        now = _now_iso()
        cur = self.conn.execute(
            """UPDATE metadata_proposals
               SET status='rejected', resolved_at=?, resolution_note='bulk batch rejected'
               WHERE batch_id=? AND status='pending'""",
            (now, batch_id),
        )
        self.conn.commit()
        return cur.rowcount

    def get_photo(self, photo_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        d["apple_labels"] = _json_loads_safe(d.get("apple_labels"))
        d["apple_persons"] = _json_loads_safe(d.get("apple_persons"))
        d["proposed_tags"] = _json_loads_safe(d.get("proposed_tags"))
        return d

    def get_photo_by_uuid(self, uuid: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM photos WHERE uuid = ?", (uuid,)).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        d["apple_labels"] = _json_loads_safe(d.get("apple_labels"))
        d["apple_persons"] = _json_loads_safe(d.get("apple_persons"))
        d["proposed_tags"] = _json_loads_safe(d.get("proposed_tags"))
        return d

    def get_photo_by_flickr_id(self, flickr_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM photos WHERE flickr_id = ?", (flickr_id,)).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        d["apple_labels"] = _json_loads_safe(d.get("apple_labels"))
        d["apple_persons"] = _json_loads_safe(d.get("apple_persons"))
        d["proposed_tags"] = _json_loads_safe(d.get("proposed_tags"))
        return d

    # -----------------------------------------------------------------------
    # Sync run tracking
    # -----------------------------------------------------------------------

    def start_sync_run(self, source: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO sync_runs (started_at, source, status) VALUES (?, ?, 'running')",
            (_now_iso(), source),
        )
        self.conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def finish_sync_run(
        self,
        run_id: int,
        status: str = "complete",
        photos_seen: int = 0,
        photos_new: int = 0,
        photos_updated: int = 0,
        error_message: str | None = None,
    ):
        self.conn.execute(
            """UPDATE sync_runs
               SET finished_at = ?, status = ?, photos_seen = ?,
                   photos_new = ?, photos_updated = ?, error_message = ?
               WHERE id = ?""",
            (_now_iso(), status, photos_seen, photos_new, photos_updated, error_message, run_id),
        )
        self.conn.commit()

    # -----------------------------------------------------------------------
    # Tag event logging
    # -----------------------------------------------------------------------

    def log_tag_event(
        self,
        photo_id: int,
        destination: str,
        tags_before: list,
        tags_after: list,
        success: bool = True,
        error: str | None = None,
    ):
        self.conn.execute(
            """INSERT INTO tag_events
               (photo_id, event_at, destination, tags_before, tags_after, success, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                photo_id,
                _now_iso(),
                destination,
                json.dumps(tags_before),
                json.dumps(tags_after),
                1 if success else 0,
                error,
            ),
        )
        self.conn.commit()

    def undo_decision(self, photo_id: int) -> bool:
        """
        Revert a review decision, returning the photo to the appropriate
        pre-review state (needs_review if it has people signals, otherwise
        candidate_public). Clears review_decision, reviewed_at, and resets
        perms_pushed_flickr. Returns True if the photo was found and updated.
        """
        row = self.conn.execute(
            """SELECT privacy_state, apple_persons, apple_unknown_faces, apple_named_faces
               FROM photos WHERE id = ?""",
            (photo_id,),
        ).fetchone()
        if not row:
            return False
        persons = _json_loads_safe(row["apple_persons"])
        has_faces = bool(persons) or (row["apple_unknown_faces"] or 0) > 0
        new_state = "needs_review" if has_faces else "candidate_public"
        self.conn.execute(
            """UPDATE photos
               SET privacy_state       = ?,
                   review_decision     = NULL,
                   reviewed_at         = NULL,
                   perms_pushed_flickr = 0,
                   updated_at          = ?
               WHERE id = ?""",
            (new_state, _now_iso(), photo_id),
        )
        self.conn.commit()
        return True

    # -----------------------------------------------------------------------
    # Album sync
    # -----------------------------------------------------------------------

    def upsert_album(self, apple_uuid: str, name: str, folder_id: int | None = None) -> int:
        """Insert or update an album record. Returns the album row id."""
        self.conn.execute(
            "INSERT OR IGNORE INTO albums (apple_uuid, name, folder_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (apple_uuid, name, folder_id, _now_iso(), _now_iso()),
        )
        self.conn.execute(
            "UPDATE albums SET name = ?, folder_id = ?, updated_at = ? WHERE apple_uuid = ?",
            (name, folder_id, _now_iso(), apple_uuid),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM albums WHERE apple_uuid = ?", (apple_uuid,)
        ).fetchone()
        return row["id"]

    def upsert_photo_album(self, photo_id: int, album_id: int) -> None:
        """Record that a photo belongs to an album.
        If the row already exists with a removed_at tombstone (photo was removed
        then re-added before sync ran), clears the tombstone — no Flickr removal needed.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO photo_albums (photo_id, album_id) VALUES (?, ?)",
            (photo_id, album_id),
        )
        # Clear any tombstone — photo is back in the album
        self.conn.execute(
            "UPDATE photo_albums SET removed_at = NULL WHERE photo_id = ? AND album_id = ? AND removed_at IS NOT NULL",
            (photo_id, album_id),
        )
        self.conn.commit()

    def get_pending_album_pushes(self, limit: int = 500) -> list[dict]:
        """Return photo+album pairs ready to push: flickr_id present, review decision made, not yet synced.

        Includes both make_public photos (perms_pushed_flickr=1) and keep_private photos
        (review_decision='keep_private'), since private photos should still appear in photosets
        for personal archive organisation.
        """
        rows = self.conn.execute(
            """SELECT pa.photo_id, pa.album_id,
                      p.flickr_id,
                      a.name AS album_name, a.flickr_set_id
               FROM photo_albums pa
               JOIN photos p ON p.id = pa.photo_id
               JOIN albums  a ON a.id = pa.album_id
               WHERE pa.flickr_pushed = 0
                 AND p.flickr_id IS NOT NULL
                 AND (p.perms_pushed_flickr = 1 OR p.review_decision = 'keep_private')
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_album_pushed(self, photo_id: int, album_id: int) -> None:
        """Mark a photo→album pair as successfully pushed to Flickr."""
        self.conn.execute(
            "UPDATE photo_albums SET flickr_pushed = 1, pushed_at = ? WHERE photo_id = ? AND album_id = ?",
            (_now_iso(), photo_id, album_id),
        )
        self.conn.commit()

    def mark_photo_album_removed(self, photo_id: int, album_id: int) -> None:
        """Tombstone a photo→album row: scanner detected the photo is no longer in this album."""
        self.conn.execute(
            "UPDATE photo_albums SET removed_at = ? WHERE photo_id = ? AND album_id = ?",
            (_now_iso(), photo_id, album_id),
        )
        self.conn.commit()

    def clear_photo_album_removed(self, photo_id: int, album_id: int) -> None:
        """Clear a removal tombstone when a photo is re-observed in an album."""
        self.conn.execute(
            "UPDATE photo_albums SET removed_at = NULL WHERE photo_id = ? AND album_id = ?",
            (photo_id, album_id),
        )
        self.conn.commit()

    def get_pending_album_removals(self, limit: int = 500) -> list[dict]:
        """Return photo→album pairs tombstoned and confirmed pushed, ready for Flickr removePhoto."""
        rows = self.conn.execute(
            """SELECT pa.photo_id, pa.album_id,
                      p.flickr_id,
                      a.name AS album_name, a.flickr_set_id
               FROM photo_albums pa
               JOIN photos p ON p.id = pa.photo_id
               JOIN albums  a ON a.id = pa.album_id
               WHERE pa.removed_at IS NOT NULL
                 AND pa.flickr_pushed = 1
                 AND a.flickr_set_id IS NOT NULL
                 AND p.flickr_id IS NOT NULL
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_deleted_albums(self) -> list[dict]:
        """Return albums marked deleted that have a Flickr photoset to clean up."""
        rows = self.conn.execute(
            """SELECT id, name, flickr_set_id
               FROM albums
               WHERE deleted_at IS NOT NULL
                 AND flickr_set_id IS NOT NULL"""
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_album_deleted(self, album_id: int) -> None:
        """Mark an album as deleted in Apple Photos (pending Flickr photoset deletion)."""
        self.conn.execute(
            "UPDATE albums SET deleted_at = ? WHERE id = ?",
            (_now_iso(), album_id),
        )
        self.conn.commit()

    def delete_photo_album_row(self, photo_id: int, album_id: int) -> None:
        """Hard-delete one photo→album membership row after Flickr removal is confirmed."""
        self.conn.execute(
            "DELETE FROM photo_albums WHERE photo_id = ? AND album_id = ?",
            (photo_id, album_id),
        )
        self.conn.commit()

    def delete_album(self, album_id: int) -> None:
        """Hard-delete an album row. ON DELETE CASCADE removes its photo_albums rows."""
        self.conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        self.conn.commit()

    def set_album_flickr_set_id(
        self, album_id: int, flickr_set_id: str, flickr_set_url: str = ""
    ) -> None:
        """Store the Flickr photoset ID after creating a new photoset."""
        self.conn.execute(
            "UPDATE albums SET flickr_set_id = ?, flickr_set_url = ?, updated_at = ? WHERE id = ?",
            (flickr_set_id, flickr_set_url, _now_iso(), album_id),
        )
        self.conn.commit()

    def set_album_flickr_name(self, album_id: int, name: str) -> None:
        """Record the name most recently pushed to the Flickr photoset title."""
        self.conn.execute(
            "UPDATE albums SET flickr_name = ?, updated_at = ? WHERE id = ?",
            (name, _now_iso(), album_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Folder methods
    # ------------------------------------------------------------------

    def upsert_folder(self, apple_uuid: str, name: str, parent_id: int | None = None) -> int:
        """Insert or update a folder record. Returns the folder row id."""
        self.conn.execute(
            "INSERT OR IGNORE INTO folders (apple_uuid, name, parent_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (apple_uuid, name, parent_id, _now_iso(), _now_iso()),
        )
        self.conn.execute(
            "UPDATE folders SET name = ?, parent_id = ?, updated_at = ? WHERE apple_uuid = ?",
            (name, parent_id, _now_iso(), apple_uuid),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM folders WHERE apple_uuid = ?", (apple_uuid,)
        ).fetchone()
        return row["id"]

    def get_all_folders(self) -> list[dict]:
        """Return all folder rows as dicts."""
        rows = self.conn.execute(
            "SELECT id, apple_uuid, name, parent_id, flickr_collection_id FROM folders"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_folder_flickr_collection_id(self, folder_id: int, collection_id: str) -> None:
        """Store the Flickr Collection ID after creating a collection."""
        self.conn.execute(
            "UPDATE folders SET flickr_collection_id = ?, updated_at = ? WHERE id = ?",
            (collection_id, _now_iso(), folder_id),
        )
        self.conn.commit()

    def set_folder_flickr_name(self, folder_id: int, name: str) -> None:
        """Record the name most recently pushed to the Flickr Collection title."""
        self.conn.execute(
            "UPDATE folders SET flickr_name = ?, updated_at = ? WHERE id = ?",
            (name, _now_iso(), folder_id),
        )
        self.conn.commit()

    def clear_folder_flickr_collection_id(self, folder_id: int) -> None:
        """Clear a stale Flickr Collection ID (e.g. collection deleted externally)."""
        self.conn.execute(
            "UPDATE folders SET flickr_collection_id = NULL, updated_at = ? WHERE id = ?",
            (_now_iso(), folder_id),
        )
        self.conn.commit()

    def get_photo_albums(self, photo_id: int) -> list[dict]:
        """Return album membership for one photo, with Flickr sync status."""
        rows = self.conn.execute(
            """SELECT a.id AS album_id, a.name, a.flickr_set_id, a.flickr_set_url,
                      pa.flickr_pushed, pa.pushed_at
               FROM photo_albums pa
               JOIN albums a ON a.id = pa.album_id
               WHERE pa.photo_id = ?
               ORDER BY a.name""",
            (photo_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_album_counts_for_photos(self, photo_ids: list[int]) -> dict[int, int]:
        """Return {photo_id: album_count} for a batch of photo IDs."""
        if not photo_ids:
            return {}
        placeholders = ",".join("?" * len(photo_ids))
        rows = self.conn.execute(
            f"SELECT photo_id, COUNT(*) AS cnt FROM photo_albums WHERE photo_id IN ({placeholders}) GROUP BY photo_id",
            photo_ids,
        ).fetchall()
        return {r["photo_id"]: r["cnt"] for r in rows}

    # -----------------------------------------------------------------------
    # Metadata conflicts (Flickr vs. Apple Photos)
    # -----------------------------------------------------------------------

    def mark_flickr_deleted(self, photo_id: int) -> None:
        """Record that a photo's Flickr copy no longer exists (API error 1)."""
        self.conn.execute(
            "UPDATE photos SET flickr_deleted = 1, updated_at = ? WHERE id = ?",
            (_now_iso(), photo_id),
        )
        self.conn.commit()

    def delete_photo(self, photo_id: int) -> None:
        """Hard-delete a Photos-only record. ON DELETE CASCADE handles photo_albums, metadata_proposals, metadata_conflicts, tag_events."""
        self.conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))

    def upsert_metadata_conflict(
        self,
        photo_id: int,
        field: str,
        flickr_value: str,
        photos_value: str,
    ) -> int:
        """
        Insert or replace a conflict record for (photo_id, field).
        Uses INSERT OR REPLACE so re-running the puller is idempotent.
        Returns the conflict row id.
        """
        cursor = self.conn.execute(
            """INSERT OR REPLACE INTO metadata_conflicts
               (photo_id, field, flickr_value, photos_value, resolved, resolution, resolved_at, created_at)
               VALUES (?, ?, ?, ?, 0, NULL, NULL, ?)""",
            (photo_id, field, flickr_value, photos_value, _now_iso()),
        )
        self.conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def resolve_metadata_conflict(self, conflict_id: int, resolution: str) -> None:
        """Mark a conflict as resolved with the given resolution strategy."""
        self.conn.execute(
            """UPDATE metadata_conflicts
               SET resolved = 1, resolution = ?, resolved_at = ?
               WHERE id = ?""",
            (resolution, _now_iso(), conflict_id),
        )
        self.conn.commit()

    def get_unresolved_conflicts(
        self,
        photo_id: int | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """
        Return unresolved metadata conflicts, optionally filtered to one photo.
        Each row includes photo fields needed for the UI (filename, thumbnail, flickr_id).
        """
        rows = self.conn.execute(
            """SELECT mc.id, mc.photo_id, mc.field,
                      mc.flickr_value, mc.photos_value, mc.created_at,
                      p.flickr_id, p.uuid, p.original_filename,
                      p.thumbnail_path, p.flickr_secret, p.flickr_server
               FROM metadata_conflicts mc
               JOIN photos p ON p.id = mc.photo_id
               WHERE mc.resolved = 0
                 AND (mc.photo_id = ? OR ? IS NULL)
               ORDER BY mc.created_at DESC
               LIMIT ?""",
            (photo_id, photo_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_conflict_counts(self) -> dict[str, int]:
        """Return per-field and total counts of unresolved conflicts."""
        row = self.conn.execute(
            """SELECT
                   COUNT(*)                                               AS total,
                   SUM(CASE WHEN field = 'title'       THEN 1 ELSE 0 END) AS title,
                   SUM(CASE WHEN field = 'description' THEN 1 ELSE 0 END) AS description,
                   SUM(CASE WHEN field = 'tags'        THEN 1 ELSE 0 END) AS tags
               FROM metadata_conflicts
               WHERE resolved = 0"""
        ).fetchone()
        if not row or row["total"] is None:
            return {"total": 0, "title": 0, "description": 0, "tags": 0}
        return {
            "total": row["total"] or 0,
            "title": row["title"] or 0,
            "description": row["description"] or 0,
            "tags": row["tags"] or 0,
        }

    # -----------------------------------------------------------------------
    # Metadata proposals (Phase 4 sync engine)
    # -----------------------------------------------------------------------

    def upsert_proposal(self, proposal: dict) -> None:
        """
        Insert a proposal into metadata_proposals following idempotency rules:
        - pending, same source hash  → skip (duplicate)
        - pending, changed hash      → supersede old, insert new
        - rejected, same source hash → skip (user previously rejected this state)
        - rejected, changed hash     → insert new
        - otherwise                  → insert new
        Does NOT commit; caller is responsible for committing.
        """
        photo_id = proposal["photo_id"]
        field = proposal["field"]
        source = proposal["source"]
        target = proposal["target"]
        new_hash = proposal["source_hash_at_creation"]

        existing = self.conn.execute(
            """SELECT id, status, source_hash_at_creation
               FROM metadata_proposals
               WHERE photo_id = ? AND field = ? AND source = ? AND target = ?
                 AND status IN ('pending', 'rejected')
               ORDER BY created_at DESC LIMIT 1""",
            (photo_id, field, source, target),
        ).fetchone()

        if existing:
            if existing["status"] == "pending":
                if existing["source_hash_at_creation"] == new_hash:
                    return  # exact duplicate, nothing changed
                # Source state changed → supersede
                self.conn.execute(
                    "UPDATE metadata_proposals SET status='superseded', resolved_at=? WHERE id=?",
                    (_now_iso(), existing["id"]),
                )
            elif existing["status"] == "rejected":
                if existing["source_hash_at_creation"] == new_hash:
                    return  # user rejected this state; don't re-generate

        self.conn.execute(
            """INSERT INTO metadata_proposals
               (photo_id, field, proposed_value, source, target, conflict_type,
                source_hash_at_creation, target_hash_at_creation, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                photo_id,
                field,
                proposal.get("proposed_value"),
                source,
                target,
                proposal["conflict_type"],
                new_hash,
                proposal.get("target_hash_at_creation"),
                proposal["created_at"],
            ),
        )

    def get_pending_proposals(
        self,
        limit: int = 100,
        offset: int = 0,
        conflict_type: str | None = None,
    ) -> list[dict]:
        """Return pending proposals joined with photo metadata, collisions first.

        For collision proposals, only the flickr→photos direction is returned
        (source='flickr'). The sibling photos→flickr proposal exists in the DB
        and is resolved automatically when the displayed proposal is actioned.
        """
        type_filter = "AND mp.conflict_type = ?" if conflict_type else ""
        # Suppress the photos→flickr half of each collision/divergence pair from display
        collision_filter = (
            "AND NOT (mp.conflict_type IN ('collision', 'divergence') AND mp.source = 'photos')"
        )
        params: list = ([conflict_type] if conflict_type else []) + [limit, offset]
        rows = self.conn.execute(
            f"""SELECT mp.id, mp.photo_id, mp.field, mp.proposed_value,
                       mp.source, mp.target, mp.conflict_type, mp.created_at,
                       mp.source_hash_at_creation, mp.target_hash_at_creation,
                       p.flickr_id, p.uuid, p.original_filename, p.thumbnail_path,
                       p.flickr_tags, p.photos_tags,
                       p.flickr_title, p.photos_title,
                       p.flickr_description, p.photos_description,
                       p.latitude, p.longitude
                FROM metadata_proposals mp
                JOIN photos p ON p.id = mp.photo_id
                WHERE mp.status = 'pending' {type_filter} {collision_filter}
                ORDER BY
                  CASE mp.conflict_type
                    WHEN 'collision'  THEN 1
                    WHEN 'divergence' THEN 2
                    ELSE                   3
                  END,
                  CASE mp.field
                    WHEN 'tags'        THEN 1
                    WHEN 'title'       THEN 2
                    WHEN 'description' THEN 3
                    ELSE                    4
                  END,
                  mp.id
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["flickr_tags"] = _json_loads_safe(d.get("flickr_tags"))
            d["photos_tags"] = _json_loads_safe(d.get("photos_tags"))
            # proposed_value is JSON for tags/geo_location, plain text for title/description
            if d.get("field") == "tags":
                d["proposed_value"] = _json_loads_safe(d.get("proposed_value"))
            elif d.get("field") == "geo_location":
                raw = d.get("proposed_value")
                try:
                    d["proposed_value"] = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, TypeError):
                    d["proposed_value"] = {}
            else:
                d["proposed_value"] = d.get("proposed_value") or ""
            result.append(d)
        return result

    def resolve_proposal(self, proposal_id: int, status: str, note: str | None = None) -> None:
        assert status in ("rejected", "applied", "superseded", "failed")
        self.conn.execute(
            """UPDATE metadata_proposals
               SET status=?, resolved_at=?, resolution_note=? WHERE id=?""",
            (status, _now_iso(), note, proposal_id),
        )
        self.conn.commit()

    def find_collision_sibling(self, proposal_id: int) -> int | None:
        """Return the id of the opposite-direction collision proposal, if pending."""
        p = self.conn.execute(
            "SELECT photo_id, field, source FROM metadata_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not p:
            return None
        sibling = self.conn.execute(
            """SELECT id FROM metadata_proposals
               WHERE photo_id=? AND field=? AND source!=? AND status='pending'
                 AND conflict_type='collision'""",
            (p["photo_id"], p["field"], p["source"]),
        ).fetchone()
        return sibling["id"] if sibling else None

    def prune_proposals(self, older_than_days: int, dry_run: bool = False) -> int:
        """Delete resolved proposals older than N days. Returns count affected."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        if dry_run:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM metadata_proposals WHERE status != 'pending' AND resolved_at < ?",
                (cutoff,),
            ).fetchone()
            return row["n"] if row else 0
        cur = self.conn.execute(
            "DELETE FROM metadata_proposals WHERE status != 'pending' AND resolved_at < ?",
            (cutoff,),
        )
        self.conn.commit()
        return cur.rowcount

    def supersede_managed_tag_proposals(self, dry_run: bool = False) -> int:
        """Supersede pending flickr→photos tag proposals that only differ by BP-managed tags.

        BP pushes machine-generated tags (location, labels) to Flickr via proposed_tags.
        When those tags appear on Flickr but not in Photos, the metadata sync would
        generate spurious flickr→photos divergence proposals. This method closes them.
        """
        import json
        import unicodedata

        def norm(tag: str) -> str:
            return "".join(
                c for c in unicodedata.normalize("NFC", tag.strip().casefold()) if c.isalnum()
            )

        now = datetime.now(timezone.utc).isoformat()
        rows = self.conn.execute(
            """SELECT mp.id, mp.proposed_value, p.proposed_tags, p.photos_tags
               FROM metadata_proposals mp
               JOIN photos p ON p.id = mp.photo_id
               WHERE mp.status = 'pending'
                 AND mp.field = 'tags'
                 AND mp.source = 'flickr'
                 AND mp.target = 'photos'"""
        ).fetchall()

        to_supersede = []
        for row in rows:
            ftags = json.loads(row["proposed_value"]) if row["proposed_value"] else []
            ptags = json.loads(row["photos_tags"]) if row["photos_tags"] else []
            managed = json.loads(row["proposed_tags"]) if row["proposed_tags"] else []
            ftags_norm = {norm(t) for t in ftags if t.strip()}
            ptags_norm = {norm(t) for t in ptags if t.strip()}
            managed_norm = {norm(t) for t in managed if t.strip()}
            ftags_effective = ftags_norm - managed_norm
            if not (ftags_effective > ptags_norm):
                to_supersede.append(row["id"])

        if not dry_run and to_supersede:
            chunk_size = 900
            for i in range(0, len(to_supersede), chunk_size):
                chunk = to_supersede[i : i + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                self.conn.execute(
                    f"UPDATE metadata_proposals SET status='superseded', resolved_at=?"
                    f" WHERE id IN ({placeholders})",
                    [now] + chunk,
                )
            self.conn.commit()

        return len(to_supersede)

    def get_proposal_counts(self) -> dict:
        """Return pending proposal counts by conflict_type (display-facing).

        Collision proposals exist in pairs (flickr→photos and photos→flickr).
        Only the flickr→photos direction is shown in the UI, so collisions are
        counted once per photo rather than twice.
        """
        rows = self.conn.execute(
            """SELECT conflict_type, COUNT(*) AS n
               FROM metadata_proposals
               WHERE status = 'pending'
                 AND NOT (conflict_type = 'collision' AND source = 'photos')
               GROUP BY conflict_type"""
        ).fetchall()
        counts = {r["conflict_type"]: r["n"] for r in rows}
        return {
            "total": sum(counts.values()),
            "non_conflict": counts.get("non_conflict", 0),
            "divergence": counts.get("divergence", 0),
            "collision": counts.get("collision", 0),
        }

    # -----------------------------------------------------------------------
    # Operation log
    # -----------------------------------------------------------------------

    def log_operation(
        self,
        photo_id: int | None,
        operation: str,
        target: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
        trigger: str | None = None,
        actor: str = "bp",
    ) -> None:
        """
        Append one entry to the operation_log table.

        Fire-and-forget: swallows all errors so journaling never interrupts
        the main operation. Safe to call even before migration 020 is applied.
        """
        try:
            self.conn.execute(
                """INSERT INTO operation_log
                   (occurred_at, photo_id, operation, target,
                    old_value, new_value, trigger, actor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (_now_iso(), photo_id, operation, target, old_value, new_value, trigger, actor),
            )
            self.conn.commit()
        except Exception:
            pass

    def get_operation_log(
        self,
        photo_id: int | None = None,
        operation: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Return operation log entries, newest first.

        Optionally filter by photo_id, operation type, or both.
        Returns [] if the table doesn't exist (pre-migration) or on error.
        """
        try:
            conditions: list[str] = []
            params: list[Any] = []
            if photo_id is not None:
                conditions.append("photo_id = ?")
                params.append(photo_id)
            if operation is not None:
                conditions.append("operation = ?")
                params.append(operation)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            params.append(limit)
            rows = self.conn.execute(
                f"""SELECT id, occurred_at, photo_id, operation, target,
                           old_value, new_value, trigger, actor
                    FROM operation_log
                    {where}
                    ORDER BY occurred_at DESC
                    LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []

    # -----------------------------------------------------------------------
    # Stats (for dashboard)
    # -----------------------------------------------------------------------

    def stats(self) -> dict:
        rows = self.conn.execute(
            """SELECT privacy_state, COUNT(*) AS n
               FROM photos GROUP BY privacy_state"""
        ).fetchall()
        counts = {r["privacy_state"]: r["n"] for r in rows}
        total = self.conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()["n"]
        result = {"total": total, "by_state": counts}
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM duplicate_groups WHERE resolved = 0"
            ).fetchone()
            result["unresolved_duplicates"] = row["n"] if row else 0
        except Exception:
            result["unresolved_duplicates"] = 0
        try:
            result["metadata_conflicts"] = self.get_conflict_counts()
        except Exception:
            result["metadata_conflicts"] = {"total": 0, "title": 0, "description": 0, "tags": 0}
        try:
            result["proposals"] = self.get_proposal_counts()
        except Exception:
            result["proposals"] = {"total": 0, "non_conflict": 0, "divergence": 0, "collision": 0}
        try:
            screenshot_counts: dict[str, int] = {}
            for label, condition in [
                ("screenshot_unreviewed", "is_screenshot = 1 AND privacy_state = 'auto_private'"),
                ("screenshot_public", "is_screenshot = 1 AND privacy_state = 'approved_public'"),
                ("screenshot_private", "is_screenshot = 1 AND privacy_state = 'keep_private'"),
            ]:
                row = self.conn.execute(
                    f"SELECT COUNT(*) AS n FROM photos WHERE {condition}"
                ).fetchone()
                screenshot_counts[label] = row["n"] if row else 0
            result["screenshot_counts"] = screenshot_counts
        except Exception:
            result["screenshot_counts"] = {
                "screenshot_unreviewed": 0,
                "screenshot_public": 0,
                "screenshot_private": 0,
            }
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM photos WHERE flickr_id IS NOT NULL AND uuid IS NULL"
            ).fetchone()
            result["flickr_only"] = row["n"] if row else 0
        except Exception:
            result["flickr_only"] = 0
        try:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM photos
                   WHERE privacy_state = 'approved_public'
                     AND flickr_id IS NOT NULL
                     AND perms_pushed_flickr = 0"""
            ).fetchone()
            result["pushable_approved"] = row["n"] if row else 0
        except Exception:
            result["pushable_approved"] = 0
        try:
            row = self.conn.execute(
                """SELECT MAX(meta_synced_flickr_at) AS flickr_ts,
                          MAX(meta_synced_photos_at) AS photos_ts
                   FROM photos"""
            ).fetchone()
            now = datetime.now(timezone.utc)

            def _age_hours(ts: str | None) -> float | None:
                if not ts:
                    return None
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return (now - dt).total_seconds() / 3600
                except Exception:
                    return None

            result["flickr_cache_age_hours"] = _age_hours(row["flickr_ts"]) if row else None
            result["photos_cache_age_hours"] = _age_hours(row["photos_ts"]) if row else None
        except Exception:
            result["flickr_cache_age_hours"] = None
            result["photos_cache_age_hours"] = None
        return result

    # -----------------------------------------------------------------------
    # Legacy library index (#162)
    # -----------------------------------------------------------------------

    _LEGACY_LIBRARY_COLS = (
        "library_uuid",
        "display_name",
        "source_path_last_seen",
        "schema_version",
        "db_mtime",
        "db_size",
        "db_head_hash",
        "asset_count",
        "indexed_at",
    )

    _LEGACY_ASSET_COLS = (
        "library_uuid",
        "asset_uuid",
        "original_filename",
        "fingerprint",
        "date_taken",
        "width",
        "height",
        "latitude",
        "longitude",
        "title",
        "description",
        "keywords",
        "labels",
        "persons",
        "named_face_count",
        "unknown_face_count",
        "master_rel_path",
        "thumbnail_cache_key",
        "thumbnail_status",
        "indexed_at",
    )

    def set_legacy_library(self, rec: dict) -> None:
        """Upsert a legacy_libraries row by library_uuid. Missing keys default
        to NULL; indexed_at defaults to now if absent."""
        rec = dict(rec)
        rec.setdefault("indexed_at", _now_iso())
        cols = [c for c in self._LEGACY_LIBRARY_COLS if c in rec]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "library_uuid")
        self.conn.execute(
            f"INSERT INTO legacy_libraries ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(library_uuid) DO UPDATE SET {updates}",
            [rec[c] for c in cols],
        )
        self.conn.commit()

    def get_legacy_library(self, library_uuid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM legacy_libraries WHERE library_uuid = ?", (library_uuid,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def upsert_legacy_asset(self, rec: dict) -> None:
        """Upsert one legacy_assets row, idempotent on (library_uuid, asset_uuid)."""
        rec = dict(rec)
        rec.setdefault("indexed_at", _now_iso())
        cols = list(self._LEGACY_ASSET_COLS)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(
            f"{c}=excluded.{c}" for c in cols if c not in ("library_uuid", "asset_uuid")
        )
        self.conn.execute(
            f"INSERT INTO legacy_assets ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(library_uuid, asset_uuid) DO UPDATE SET {updates}",
            [rec.get(c) for c in cols],
        )
        self.conn.commit()

    def legacy_asset_count(self, library_uuid: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM legacy_assets WHERE library_uuid = ?", (library_uuid,)
        ).fetchone()
        return int(row[0])

    def iter_legacy_assets(self, library_uuid: str) -> Iterator[dict]:
        """Yield legacy_assets rows for a library as dicts, ordered by asset_uuid."""
        for row in self.conn.execute(
            "SELECT * FROM legacy_assets WHERE library_uuid = ? ORDER BY asset_uuid",
            (library_uuid,),
        ):
            yield _row_to_dict(row)

    def delete_legacy_assets_not_in(
        self, library_uuid: str, seen_asset_uuids: set[str]
    ) -> list[str]:
        """Hard-delete rows for this library whose asset_uuid was NOT seen this run.
        Returns the thumbnail_cache_keys of deleted rows (for thumbnail GC).
        Authoritative reconciliation — callers must only invoke after a FULL run
        completes successfully (never for --limit / interrupted runs)."""
        rows = self.conn.execute(
            "SELECT asset_uuid, thumbnail_cache_key FROM legacy_assets WHERE library_uuid = ?",
            (library_uuid,),
        ).fetchall()
        to_delete = [r for r in rows if r["asset_uuid"] not in seen_asset_uuids]
        removed_keys = [r["thumbnail_cache_key"] for r in to_delete if r["thumbnail_cache_key"]]
        for r in to_delete:
            self.conn.execute(
                "DELETE FROM legacy_assets WHERE library_uuid = ? AND asset_uuid = ?",
                (library_uuid, r["asset_uuid"]),
            )
        self.conn.commit()
        return removed_keys
