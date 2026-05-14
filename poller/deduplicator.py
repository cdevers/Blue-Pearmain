"""
deduplicator.py — find and classify duplicate photos in the Blue Pearmain DB.

Duplicate types detected:

  snapbridge   Same filename + timestamp, different fingerprints, one added within
               SNAPBRIDGE_WINDOW_SECS of date_taken (the low-res phone preview),
               the other added much later (the full-res card import). Keeper is
               always the higher-resolution copy (larger width × height). Falls
               back to later date_added_photos if dimensions are unavailable.

  device_upload  Same filename + timestamp, same or unknown fingerprint, upload
               timestamps to Flickr separated by more than DEVICE_GAP_MINUTES.
               Typical cause: same file auto-uploaded from both iPhone and iPad.
               Keeper is the earlier Flickr upload.

  uncertain    Same filename + timestamp but doesn't clearly fit either pattern.
               Flagged for human review rather than auto-resolved.

Run modes:
  --dry-run    Print findings without writing to the DB (default)
  --write      Populate duplicate_groups table and set duplicate_group_id /
               duplicate_role on photos rows
  --confirm    After --write, actually delete discard photos from Flickr
               (requires explicit flag to prevent accidents)

Usage:
    python poller/deduplicator.py --config config/config.yml --dry-run
    python poller/deduplicator.py --config config/config.yml --write
    python poller/deduplicator.py --config config/config.yml --write --confirm
"""

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

log = logging.getLogger(__name__)

# Upload timestamps this far apart suggest dual-device upload
DEVICE_GAP_MINUTES = 5

# Uncertain groups whose max/min pixel-count ratio exceeds this are auto-dismissed
# as not_duplicate (clearly different images — crops, firmware quirks, edits).
# True duplicates always have identical dimensions (ratio = 1.0).
NOT_DUPLICATE_PIXEL_RATIO = 1.1

# Re-upload detection: Flickr IDs this far apart indicate separate upload sessions
CROSS_SESSION_THRESHOLD = 100_000

# Re-upload detection: orphan must exceed linked pixel count by this ratio to displace it as keeper
REUPLOAD_KEEPER_PIXEL_RATIO = 1.5


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PhotoRow:
    id: int
    flickr_id: str | None
    uuid: str | None
    original_filename: str
    date_taken: str
    date_added_photos: str | None
    date_uploaded_flickr: str | None
    fingerprint: str | None
    width: int | None
    height: int | None
    privacy_state: str
    duplicate_group_id: int | None

    @property
    def pixels(self) -> int | None:
        if self.width and self.height:
            return self.width * self.height
        return None

    @property
    def date_taken_dt(self) -> datetime | None:
        return _parse_dt(self.date_taken)

    @property
    def date_added_dt(self) -> datetime | None:
        return _parse_dt(self.date_added_photos) if self.date_added_photos else None

    @property
    def date_uploaded_dt(self) -> datetime | None:
        return _parse_dt(self.date_uploaded_flickr) if self.date_uploaded_flickr else None

    @property
    def seconds_to_add(self) -> float | None:
        """Seconds between date_taken and date_added_photos (Snapbridge signal)."""
        t = self.date_taken_dt
        a = self.date_added_dt
        if t and a:
            return abs((a - t).total_seconds())
        return None


@dataclass
class DuplicateGroup:
    match_key: str               # "filename|date_taken"
    group_type: str              # snapbridge | device_upload | uncertain
    photos: list[PhotoRow]
    keeper: PhotoRow | None = None
    discards: list[PhotoRow] = field(default_factory=list)
    review: list[PhotoRow] = field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    # Normalise the space-separated variant from Flickr ("2024-09-28 14:12:43")
    s = s.strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _normalise_to_utc_second(s: str) -> str | None:
    """Parse date_taken, convert to UTC, truncate to whole second.

    Returns 'YYYY-MM-DD HH:MM:SS' in UTC, or None on parse failure.
    Uses truncation (not rounding) to match normalise_dt() in scanner.py.
    Both sides of the reupload join must use identical normalisation.

    Note: _parse_dt() already attaches tzinfo=UTC for naive datetimes, so
    .astimezone(timezone.utc) below is a no-op in that case.  The explicit
    guard is kept here so this function is correct even if refactored to
    not go through _parse_dt().
    """
    dt = _parse_dt(s)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
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


def _pixels_ratio(photos: list[PhotoRow]) -> float | None:
    """Return max/min pixel count ratio across photos, or None if any lack dimensions."""
    pixel_counts = [p.pixels for p in photos]
    if any(px is None for px in pixel_counts):
        return None
    counts = [px for px in pixel_counts if px is not None]
    return max(counts) / min(counts)


def _upload_gap_minutes(photos: list[PhotoRow]) -> float | None:
    """Max gap in minutes between Flickr upload timestamps in a group."""
    times = [p.date_uploaded_dt for p in photos if p.date_uploaded_dt]
    if len(times) < 2:
        return None
    return (max(times) - min(times)).total_seconds() / 60


def _is_snapbridge_pair(photos: list[PhotoRow]) -> bool:
    """
    True if exactly two photos match the Snapbridge low-res/high-res pattern:
    same filename + timestamp (guaranteed by the caller), different fingerprints
    (different file content), and — when available — different pixel dimensions.

    Timing (date_added_photos) is intentionally NOT used here. Snapbridge
    previews sometimes arrive days or weeks after capture, and full-res card
    imports can be delayed by months. The reliable signals are fingerprint
    divergence (proves different files) and resolution difference (proves
    one is the low-res preview). If dimensions are not yet populated, we
    return False and let the group stay 'uncertain' until the scanner
    backfill provides them.
    """
    if len(photos) != 2:
        return False
    a, b = photos
    if not a.fingerprint or not b.fingerprint:
        return False
    if a.fingerprint == b.fingerprint:
        return False
    # Dimensions available: must differ to confirm low-res/high-res split
    if a.pixels is not None and b.pixels is not None:
        return a.pixels != b.pixels
    # Dimensions not yet populated — stay uncertain until scanner backfill runs
    return False


def _classify_group(photos: list[PhotoRow]) -> DuplicateGroup:
    filename = photos[0].original_filename
    date_taken = photos[0].date_taken
    match_key = f"{filename}|{date_taken}"

    if _is_snapbridge_pair(photos):
        # Keeper = higher resolution (larger pixel count). _is_snapbridge_pair only
        # returns True when both photos have dimensions, so pixels will not be None.
        ranked = sorted(photos, key=lambda p: p.pixels or 0, reverse=True)
        keeper = ranked[0]
        discards = ranked[1:]
        notes = (
            f"Snapbridge pair: keeper is {keeper.width}×{keeper.height}px "
            f"({keeper.uuid or keeper.flickr_id}), "
            f"discard is {discards[0].width}×{discards[0].height}px "
            f"({discards[0].uuid or discards[0].flickr_id})"
        )
        return DuplicateGroup(match_key, "snapbridge", photos, keeper, discards, [], notes)

    # Check for device_upload pattern: same/unknown fingerprint, staggered Flickr uploads
    gap = _upload_gap_minutes(photos)
    fingerprints = {p.fingerprint for p in photos if p.fingerprint}
    all_on_flickr = all(p.flickr_id for p in photos)

    if all_on_flickr and gap is not None and gap > DEVICE_GAP_MINUTES:
        # Keeper = earliest Flickr upload (it got there first, may have more views eventually)
        ranked = sorted(
            photos,
            key=lambda p: p.date_uploaded_dt.timestamp() if p.date_uploaded_dt else float("inf")
        )
        keeper = ranked[0]
        discards = ranked[1:]
        notes = (
            f"Device upload duplicate: {len(photos)} copies, "
            f"upload gap {gap:.0f} min, "
            f"keeper uploaded {keeper.date_uploaded_flickr}"
        )
        return DuplicateGroup(match_key, "device_upload", photos, keeper, discards, [], notes)

    # Dimension-divergence check: if all photos have dimensions and they differ
    # beyond the threshold, these are clearly different images (crop, firmware quirk).
    ratio = _pixels_ratio(photos)
    if ratio is not None and ratio > NOT_DUPLICATE_PIXEL_RATIO:
        notes = (
            f"Auto-dismissed: pixel ratio {ratio:.2f} exceeds threshold "
            f"{NOT_DUPLICATE_PIXEL_RATIO} — likely distinct images with coincident filename/timestamp"
        )
        return DuplicateGroup(match_key, "not_duplicate", photos, None, [], [], notes)

    # Uncertain — flag for human review
    notes = (
        f"Uncertain: {len(photos)} photos, "
        f"fingerprints={'same' if len(fingerprints) == 1 else f'{len(fingerprints)} unique'}, "
        f"upload gap={gap:.0f}min" if gap else f"upload gap=unknown"
    )
    return DuplicateGroup(match_key, "uncertain", photos, None, [], photos, notes)


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
        # Only linked has valid dimensions — linked wins tentatively, but can't confirm
        keeper = linked
        force_uncertain = True
        dimension_ratio = None
    elif orphan_px:
        # Only orphan has dims — never auto-promote orphan from its own unilateral data
        keeper = linked
        force_uncertain = True
        dimension_ratio = None
    else:
        # Neither has dimensions — linked wins conservatively; keeper choice is assumed
        keeper = linked
        keeper_assumed = True
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


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def _fetch_duplicate_candidates(conn: sqlite3.Connection) -> list[DuplicateGroup]:
    """Return all groups of photos sharing original_filename + date_taken."""
    rows = conn.execute("""
        SELECT
            p.id, p.flickr_id, p.uuid, p.original_filename, p.date_taken,
            p.date_added_photos, p.date_uploaded_flickr, p.fingerprint,
            p.width, p.height, p.privacy_state, p.duplicate_group_id
        FROM photos p
        JOIN (
            SELECT original_filename, date_taken
            FROM photos
            WHERE original_filename IS NOT NULL
              AND date_taken IS NOT NULL
            GROUP BY original_filename, date_taken
            HAVING COUNT(*) > 1
        ) dup USING (original_filename, date_taken)
        ORDER BY p.original_filename, p.date_taken, p.date_added_photos
    """).fetchall()

    # Group by (filename, date_taken)
    groups: dict[str, list[PhotoRow]] = {}
    for r in rows:
        key = f"{r['original_filename']}|{r['date_taken']}"
        photo = PhotoRow(
            id=r["id"],
            flickr_id=r["flickr_id"],
            uuid=r["uuid"],
            original_filename=r["original_filename"],
            date_taken=r["date_taken"],
            date_added_photos=r["date_added_photos"],
            date_uploaded_flickr=r["date_uploaded_flickr"],
            fingerprint=r["fingerprint"],
            width=r["width"],
            height=r["height"],
            privacy_state=r["privacy_state"],
            duplicate_group_id=r["duplicate_group_id"],
        )
        groups.setdefault(key, []).append(photo)

    return [_classify_group(photos) for photos in groups.values()]


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


def _write_groups(conn: sqlite3.Connection, groups: list[DuplicateGroup]) -> dict[str, int]:
    """Write duplicate_groups rows and update photos. Returns type counts."""
    counts: dict[str, int] = {"snapbridge": 0, "device_upload": 0, "uncertain": 0, "not_duplicate": 0}

    for group in groups:
        is_not_duplicate = group.group_type == "not_duplicate"

        # Upsert into duplicate_groups; not_duplicate groups are immediately resolved
        conn.execute("""
            INSERT INTO duplicate_groups (match_key, group_type, photo_count, notes, resolved)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(match_key) DO UPDATE SET
                group_type  = excluded.group_type,
                photo_count = excluded.photo_count,
                notes       = excluded.notes,
                resolved    = excluded.resolved,
                updated_at  = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """, (group.match_key, group.group_type, len(group.photos), group.notes,
              1 if is_not_duplicate else 0))

        group_id = conn.execute(
            "SELECT id FROM duplicate_groups WHERE match_key = ?", (group.match_key,)
        ).fetchone()["id"]

        # Set keeper
        if group.keeper:
            conn.execute(
                "UPDATE duplicate_groups SET keeper_id = ? WHERE id = ?",
                (group.keeper.id, group_id)
            )
            conn.execute(
                "UPDATE photos SET duplicate_group_id = ?, duplicate_role = ? WHERE id = ?",
                (group_id, "keeper", group.keeper.id)
            )

        # Set discards
        for p in group.discards:
            conn.execute(
                "UPDATE photos SET duplicate_group_id = ?, duplicate_role = ? WHERE id = ?",
                (group_id, "discard", p.id)
            )

        # Set review
        for p in group.review:
            conn.execute(
                "UPDATE photos SET duplicate_group_id = ?, duplicate_role = ? WHERE id = ?",
                (group_id, "review", p.id)
            )

        counts[group.group_type] = counts.get(group.group_type, 0) + 1

    return counts


def _delete_flickr_discards(
    conn: sqlite3.Connection, groups: list[DuplicateGroup], flickr_client: Any
) -> int:
    """Delete discard photos from Flickr. Returns count of deletions."""
    deleted = 0
    for group in groups:
        for photo in group.discards:
            if photo.flickr_id:
                try:
                    flickr_client.delete_photo(photo.flickr_id)
                    conn.execute(
                        "UPDATE photos SET privacy_state = 'duplicate_flickr' WHERE id = ?",
                        (photo.id,)
                    )
                    log.info("Deleted Flickr photo %s (%s)", photo.flickr_id, group.match_key)
                    deleted += 1
                except Exception as exc:
                    log.error(
                        "Failed to delete %s: %s", photo.flickr_id, exc
                    )
    return deleted


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_report(groups: list[DuplicateGroup]) -> None:
    by_type: dict[str, list[DuplicateGroup]] = {}
    for g in groups:
        by_type.setdefault(g.group_type, []).append(g)

    total = len(groups)
    print(f"\nDuplicate groups found: {total}")
    for gtype, glist in sorted(by_type.items()):
        print(f"  {gtype:<15} {len(glist):>5} groups")

    print()
    for gtype in ("snapbridge", "device_upload", "uncertain"):
        glist = by_type.get(gtype, [])
        if not glist:
            continue
        print(f"── {gtype.upper()} ({len(glist)} groups) " + "─" * 40)
        for g in glist[:10]:  # show up to 10 per type
            keeper_label = (
                f"keeper={g.keeper.uuid or g.keeper.flickr_id or g.keeper.id}"
                if g.keeper else "no keeper assigned"
            )
            print(f"  {g.match_key}")
            print(f"    {keeper_label}")
            print(f"    {g.notes}")
        if len(glist) > 10:
            print(f"  ... and {len(glist) - 10} more")
        print()


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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Print findings without writing (default)")
    parser.add_argument("--write", action="store_true",
                        help="Write duplicate groups to DB")
    parser.add_argument("--confirm", action="store_true",
                        help="Delete discard photos from Flickr (requires --write)")
    parser.add_argument("--flickr", action="store_true",
                        help="Detect Flickr re-upload duplicates (orphan paired with linked record)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum pairs to write (recommended for first live runs)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.write:
        args.dry_run = False

    config_path = Path(args.config)
    if not config_path.exists():
        log.error("Config not found: %s", config_path)
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    db_path = config.get("database", {}).get("path", "data/curator.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if args.flickr:
        if args.confirm:
            log.error("--confirm is not supported with --flickr (use --flickr --write to group only)")
            sys.exit(1)
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

    log.info("Scanning for duplicates in %s …", db_path)
    groups = _fetch_duplicate_candidates(conn)
    _print_report(groups)

    if args.dry_run:
        print("Dry run — no changes written. Use --write to persist.")
        conn.close()
        return

    log.info("Writing %d groups to DB …", len(groups))
    conn.execute("BEGIN")
    try:
        counts = _write_groups(conn, groups)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    print(f"Written: {counts}")

    if args.confirm:
        if not args.write:
            log.error("--confirm requires --write")
            sys.exit(1)
        log.info("Loading Flickr client for deletions …")
        sys.path.insert(0, ".")
        from flickr.flickr_client import FlickrClient
        flickr_client = FlickrClient(config)
        conn.execute("BEGIN")
        try:
            deleted = _delete_flickr_discards(conn, groups, flickr_client)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        print(f"Deleted {deleted} photos from Flickr.")

    conn.close()


if __name__ == "__main__":
    main()
