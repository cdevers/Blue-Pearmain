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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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

        flickr_row  = dict(flickr_row)
        photos_row  = dict(photos_row)

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
                    (photos_rec_id, t["event_at"], t["destination"],
                     t["tags_before"], t["tags_after"], t["success"], t["error"]),
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
                (photos_rec_id, c["field"], c["flickr_value"], c["photos_value"],
                 c["resolved"], c["resolution"], c["resolved_at"], c["created_at"]),
            )

        # 4. Build the set of fields to copy into the Photos record
        update: dict[str, Any] = {}

        # Flickr identity fields — always copy from the Flickr record
        for field in (
            "flickr_id", "flickr_secret", "flickr_server", "flickr_farm",
            "date_uploaded_flickr", "perms_pushed_flickr", "tags_pushed_flickr",
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
            self.conn.execute(
                "UPDATE photos SET flickr_id = NULL WHERE id = ?", (flickr_rec_id,)
            )

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
        for field in ("apple_labels", "apple_persons", "proposed_tags",
                      "flickr_tags", "photos_tags"):
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
                for p in ("privacy_state", "privacy_reason",
                          "review_decision", "reviewed_at", "review_notes"):
                    update_data.pop(p, None)

            placeholders = ", ".join(f"{k} = ?" for k in update_data)
            values = list(update_data.values()) + [row_id]
            self.conn.execute(
                f"UPDATE photos SET {placeholders} WHERE id = ?", values
            )
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

    def record_review(self, photo_id: int, decision: str, notes: str = ""):
        """Record a human review decision and update privacy state accordingly."""
        state_map = {
            "make_public":  "approved_public",
            "keep_private": "keep_private",
            "skip":         "skipped",
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
        rows = self.conn.execute(
            "SELECT * FROM geofence_zones WHERE active = 1"
        ).fetchall()
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
    # Review queue queries
    # -----------------------------------------------------------------------

    def review_queue(
        self,
        states: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
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
        rows = self.conn.execute(
            f"""SELECT id, flickr_id, original_filename,
                       apple_unknown_faces, apple_named_faces, proposed_tags,
                       display_rotation
                FROM photos
                WHERE privacy_state IN ({placeholders})
                ORDER BY date_taken DESC, id DESC
                LIMIT ? OFFSET ?""",
            states + [limit, offset],
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["proposed_tags"] = _json_loads_safe(d.get("proposed_tags"))
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

    def review_queue_count(self, states: list[str] | None = None) -> int:
        if states is None:
            states = ["needs_review", "candidate_public"]
        placeholders = ",".join("?" * len(states))
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM photos WHERE privacy_state IN ({placeholders})",
            states,
        ).fetchone()
        return row["n"] if row else 0

    def get_photo(self, photo_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        d["apple_labels"] = _json_loads_safe(d.get("apple_labels"))
        d["apple_persons"] = _json_loads_safe(d.get("apple_persons"))
        d["proposed_tags"] = _json_loads_safe(d.get("proposed_tags"))
        return d

    def get_photo_by_uuid(self, uuid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM photos WHERE uuid = ?", (uuid,)
        ).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        d["apple_labels"] = _json_loads_safe(d.get("apple_labels"))
        d["apple_persons"] = _json_loads_safe(d.get("apple_persons"))
        d["proposed_tags"] = _json_loads_safe(d.get("proposed_tags"))
        return d

    def get_photo_by_flickr_id(self, flickr_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM photos WHERE flickr_id = ?", (flickr_id,)
        ).fetchone()
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
            (_now_iso(), status, photos_seen, photos_new, photos_updated,
             error_message, run_id),
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
            (photo_id,)
        ).fetchone()
        if not row:
            return False
        persons   = _json_loads_safe(row["apple_persons"])
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
        """Record that a photo belongs to an album. No-op if already exists."""
        self.conn.execute(
            "INSERT OR IGNORE INTO photo_albums (photo_id, album_id) VALUES (?, ?)",
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

    def set_album_flickr_set_id(self, album_id: int, flickr_set_id: str, flickr_set_url: str = "") -> None:
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
            "total":       row["total"]       or 0,
            "title":       row["title"]       or 0,
            "description": row["description"] or 0,
            "tags":        row["tags"]        or 0,
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
        photo_id  = proposal["photo_id"]
        field     = proposal["field"]
        source    = proposal["source"]
        target    = proposal["target"]
        new_hash  = proposal["source_hash_at_creation"]

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
                photo_id, field, proposal.get("proposed_value"),
                source, target, proposal["conflict_type"],
                new_hash, proposal.get("target_hash_at_creation"),
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
        # Suppress the photos→flickr half of each collision pair from display
        collision_filter = "AND NOT (mp.conflict_type = 'collision' AND mp.source = 'photos')"
        params: list = ([conflict_type] if conflict_type else []) + [limit, offset]
        rows = self.conn.execute(
            f"""SELECT mp.id, mp.photo_id, mp.field, mp.proposed_value,
                       mp.source, mp.target, mp.conflict_type, mp.created_at,
                       mp.source_hash_at_creation, mp.target_hash_at_creation,
                       p.flickr_id, p.uuid, p.original_filename, p.thumbnail_path,
                       p.flickr_tags, p.photos_tags,
                       p.flickr_title, p.photos_title,
                       p.flickr_description, p.photos_description
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
            d["flickr_tags"]  = _json_loads_safe(d.get("flickr_tags"))
            d["photos_tags"]  = _json_loads_safe(d.get("photos_tags"))
            # proposed_value is JSON for tags, plain text for title/description
            if d.get("field") == "tags":
                d["proposed_value"] = _json_loads_safe(d.get("proposed_value"))
            else:
                d["proposed_value"] = d.get("proposed_value") or ""
            result.append(d)
        return result

    def resolve_proposal(
        self, proposal_id: int, status: str, note: str | None = None
    ) -> None:
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
            "total":        sum(counts.values()),
            "non_conflict": counts.get("non_conflict", 0),
            "divergence":   counts.get("divergence",   0),
            "collision":    counts.get("collision",    0),
        }

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
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM photos WHERE flickr_id IS NOT NULL AND uuid IS NULL"
            ).fetchone()
            result["flickr_only"] = row["n"] if row else 0
        except Exception:
            result["flickr_only"] = 0
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
            result["flickr_cache_age_hours"]  = _age_hours(row["flickr_ts"])  if row else None
            result["photos_cache_age_hours"]  = _age_hours(row["photos_ts"])  if row else None
        except Exception:
            result["flickr_cache_age_hours"] = None
            result["photos_cache_age_hours"] = None
        return result
