"""
flickr/proposal_applier.py — apply metadata proposals to Photos or Flickr

Used by the reviewer UI (/api/proposals/<id>/approve) and
CLI (bp reconcile --apply-proposals).

Apply-time staleness checks are performed per the architecture contract:
if source or target state has changed since the proposal was created, the
proposal is superseded rather than applied.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import unicodedata
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db.db import Database
    from flickr.flickr_client import FlickrClient

log = logging.getLogger("blue-pearmain.proposal_applier")

MAX_FLICKR_TAGS = 75


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_hash(tags: list[str]) -> str:
    normed = sorted(
        {unicodedata.normalize("NFC", t.strip().casefold()).replace(" ", "")
         for t in tags if t.strip()}
    )
    return hashlib.sha256(" ".join(normed).encode()).hexdigest()


def apply_proposal(
    db: "Database",
    proposal_id: int,
    library_path: str,
    flickr_client: "FlickrClient | None" = None,
) -> dict:
    """
    Apply a pending proposal. Returns {"ok": bool, "reason": str | None}.
    Marks the proposal 'applied' on success; leaves it 'pending' on failure
    so the next run can retry. Supersedes it if stale.
    """
    row = db.conn.execute(
        """SELECT mp.id, mp.photo_id, mp.field, mp.proposed_value,
                  mp.source, mp.target, mp.conflict_type, mp.status,
                  mp.source_hash_at_creation, mp.target_hash_at_creation,
                  p.flickr_id, p.uuid,
                  p.flickr_tags_hash, p.photos_tags_hash
           FROM metadata_proposals mp
           JOIN photos p ON p.id = mp.photo_id
           WHERE mp.id = ?""",
        (proposal_id,),
    ).fetchone()

    if not row:
        return {"ok": False, "reason": "proposal not found"}
    if row["status"] != "pending":
        return {"ok": False, "reason": f"proposal already '{row['status']}'"}

    # Apply-time staleness checks (architecture doc §Proposals table rules 4 & 5)
    src_hash  = row["flickr_tags_hash"] if row["source"] == "flickr" else row["photos_tags_hash"]
    tgt_hash  = row["photos_tags_hash"] if row["target"] == "photos" else row["flickr_tags_hash"]

    if src_hash != row["source_hash_at_creation"]:
        _supersede(db, proposal_id)
        return {"ok": False, "reason": "source_changed"}
    if tgt_hash != row["target_hash_at_creation"]:
        _supersede(db, proposal_id)
        return {"ok": False, "reason": "target_changed"}

    new_tags = json.loads(row["proposed_value"]) if row["proposed_value"] else []

    if row["target"] == "photos":
        return _apply_to_photos(db, row, new_tags, library_path)
    if row["target"] == "flickr":
        if flickr_client is None:
            return {"ok": False, "reason": "no flickr_client provided"}
        return _apply_to_flickr(db, row, new_tags, flickr_client)
    return {"ok": False, "reason": f"unknown target '{row['target']}'"}


def apply_batch(
    db: "Database",
    library_path: str,
    flickr_client: "FlickrClient | None" = None,
    conflict_types: list[str] | None = None,
    limit: int = 500,
) -> dict:
    """Apply pending proposals in batch. Returns totals dict."""
    if conflict_types is None:
        conflict_types = ["non_conflict"]

    placeholders = ",".join("?" * len(conflict_types))
    rows = db.conn.execute(
        f"""SELECT id FROM metadata_proposals
            WHERE status = 'pending' AND conflict_type IN ({placeholders})
            ORDER BY id LIMIT ?""",
        conflict_types + [limit],
    ).fetchall()

    totals: dict = {"applied": 0, "failed": 0, "superseded": 0}
    for r in rows:
        result = apply_proposal(db, r["id"], library_path, flickr_client)
        if result["ok"]:
            totals["applied"] += 1
        elif result.get("reason") in ("source_changed", "target_changed"):
            totals["superseded"] += 1
        else:
            totals["failed"] += 1
            log.warning("apply_batch: proposal %s failed: %s", r["id"], result.get("reason"))
    return totals


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _apply_to_photos(db: "Database", row, new_tags: list[str], library_path: str) -> dict:
    uuid = row["uuid"]
    if not uuid:
        return {"ok": False, "reason": "photo has no uuid"}
    if not _photos_is_running():
        return {"ok": False, "reason": "Photos.app is not running"}

    try:
        import photoscript
    except ImportError:
        return {"ok": False, "reason": "photoscript not installed"}

    try:
        photo = photoscript.Photo(uuid)
    except Exception as e:
        return {"ok": False, "reason": f"photo not found in Photos: {e}"}

    try:
        photo.keywords = new_tags
    except Exception as e:
        return {"ok": False, "reason": f"write failed: {e}"}

    try:
        written = list(photo.keywords or [])
    except Exception:
        written = new_tags  # can't re-read; assume success

    now = _now_iso()
    db.conn.execute(
        """UPDATE photos
           SET photos_tags=?, photos_tags_hash=?, meta_synced_photos_at=?, updated_at=?
           WHERE id=?""",
        (json.dumps(written), _compute_hash(written), now, now, row["photo_id"]),
    )
    _mark_applied(db, row["id"])
    db.conn.commit()
    log.info(
        "applied proposal %s → Photos  photo_id=%s  tags=%d",
        row["id"], row["photo_id"], len(written),
    )
    return {"ok": True}


def _apply_to_flickr(
    db: "Database", row, new_tags: list[str], flickr_client: "FlickrClient"
) -> dict:
    flickr_id = row["flickr_id"]
    if not flickr_id:
        return {"ok": False, "reason": "photo has no flickr_id"}

    truncated = len(new_tags) > MAX_FLICKR_TAGS
    if truncated:
        new_tags = new_tags[:MAX_FLICKR_TAGS]

    try:
        flickr_client.set_tags(flickr_id, new_tags)
    except Exception as e:
        return {"ok": False, "reason": f"Flickr API error: {e}"}

    now = _now_iso()
    db.conn.execute(
        """UPDATE photos
           SET flickr_tags=?, flickr_tags_hash=?, meta_synced_flickr_at=?,
               tags_truncated_for_flickr=?, updated_at=?
           WHERE id=?""",
        (json.dumps(new_tags), _compute_hash(new_tags), now,
         1 if truncated else 0, now, row["photo_id"]),
    )
    _mark_applied(db, row["id"])
    db.conn.commit()
    log.info(
        "applied proposal %s → Flickr  photo_id=%s  tags=%d%s",
        row["id"], row["photo_id"], len(new_tags),
        " (truncated)" if truncated else "",
    )
    return {"ok": True}


def _supersede(db: "Database", proposal_id: int) -> None:
    db.conn.execute(
        "UPDATE metadata_proposals SET status='superseded', resolved_at=? WHERE id=?",
        (_now_iso(), proposal_id),
    )
    db.conn.commit()


def _mark_applied(db: "Database", proposal_id: int) -> None:
    db.conn.execute(
        "UPDATE metadata_proposals SET status='applied', resolved_at=? WHERE id=?",
        (_now_iso(), proposal_id),
    )


def _photos_is_running() -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to (name of processes) contains "Photos"'],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip().lower() == "true"
    except Exception:
        return False
