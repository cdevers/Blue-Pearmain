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
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    # -----------------------------------------------------------------------
    # Connection management
    # -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_schema(self):
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            sql = schema_path.read_text()
            self.conn.executescript(sql)
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
        for field in ("apple_labels", "apple_persons", "proposed_tags"):
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
            # Don't clobber review decisions that have already been made
            protected = {"review_decision", "reviewed_at", "review_notes",
                         "privacy_state", "privacy_reason"}
            # ... unless the caller explicitly passes them
            has_review = any(k in data for k in protected)

            update_data = {k: v for k, v in data.items() if k != lookup_field}
            if not has_review:
                for p in protected:
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
            "UPDATE photos SET privacy_state = ?, privacy_reason = ?, date_synced = ? WHERE id = ?",
            (state, reason, _now_iso(), photo_id),
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
                   privacy_state = ?, date_synced = ?
               WHERE id = ?""",
            (decision, notes, _now_iso(), new_state, _now_iso(), photo_id),
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
        """Return photos awaiting review, ordered oldest-first."""
        if states is None:
            states = ["needs_review", "candidate_public"]
        placeholders = ",".join("?" * len(states))
        rows = self.conn.execute(
            f"""SELECT * FROM photos
                WHERE privacy_state IN ({placeholders})
                ORDER BY date_taken ASC
                LIMIT ? OFFSET ?""",
            states + [limit, offset],
        ).fetchall()
        result = []
        for row in rows:
            d = _row_to_dict(row)
            d["apple_labels"] = _json_loads_safe(d.get("apple_labels"))
            d["apple_persons"] = _json_loads_safe(d.get("apple_persons"))
            d["proposed_tags"] = _json_loads_safe(d.get("proposed_tags"))
            result.append(d)
        return result

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
        return result
