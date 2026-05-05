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
import html
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
        {"".join(c for c in unicodedata.normalize("NFC", t.strip().casefold()) if c.isalnum())
         for t in tags if t.strip()}
    )
    return hashlib.sha256(" ".join(normed).encode()).hexdigest()


def _compute_text_hash(value: str) -> str:
    return hashlib.sha256(html.unescape((value or "").strip()).encode()).hexdigest()


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
                  p.flickr_tags_hash, p.photos_tags_hash,
                  p.flickr_title, p.photos_title,
                  p.flickr_description, p.photos_description
           FROM metadata_proposals mp
           JOIN photos p ON p.id = mp.photo_id
           WHERE mp.id = ?""",
        (proposal_id,),
    ).fetchone()

    if not row:
        return {"ok": False, "reason": "proposal not found"}
    if row["status"] != "pending":
        return {"ok": False, "reason": f"proposal already '{row['status']}'"}

    # Apply-time staleness checks
    field = row["field"]
    if field == "tags":
        src_hash = row["flickr_tags_hash"] if row["source"] == "flickr" else row["photos_tags_hash"]
        tgt_hash = row["photos_tags_hash"] if row["target"] == "photos" else row["flickr_tags_hash"]
    else:
        flickr_text = row[f"flickr_{field}"] or ""
        photos_text = row[f"photos_{field}"] or ""
        src_hash = _compute_text_hash(flickr_text if row["source"] == "flickr" else photos_text)
        tgt_hash = _compute_text_hash(photos_text if row["target"] == "photos" else flickr_text)

    if src_hash != row["source_hash_at_creation"]:
        _supersede(db, proposal_id)
        return {"ok": False, "reason": "source_changed"}
    if tgt_hash != row["target_hash_at_creation"]:
        _supersede(db, proposal_id)
        return {"ok": False, "reason": "target_changed"}

    if field == "tags":
        new_tags = json.loads(row["proposed_value"]) if row["proposed_value"] else []
        if row["target"] == "photos":
            result = _apply_to_photos(db, row, new_tags, library_path)
            if not result["ok"] and result.get("stale_uuid"):
                _handle_stale_uuid(db, row["id"], row["photo_id"])
                return {"ok": False, "reason": "stale_uuid"}
            return result
        if row["target"] == "flickr":
            if flickr_client is None:
                return {"ok": False, "reason": "no flickr_client provided"}
            return _apply_to_flickr(db, row, new_tags, flickr_client)
    else:
        new_value = row["proposed_value"] or ""
        if row["target"] == "photos":
            result = _apply_text_to_photos(db, row, new_value)
            if not result["ok"] and result.get("stale_uuid"):
                _handle_stale_uuid(db, row["id"], row["photo_id"])
                return {"ok": False, "reason": "stale_uuid"}
            return result
        if row["target"] == "flickr":
            if flickr_client is None:
                return {"ok": False, "reason": "no flickr_client provided"}
            return _apply_text_to_flickr(db, row, new_value, flickr_client)
    return {"ok": False, "reason": f"unknown target '{row['target']}'"}


def apply_batch(
    db: "Database",
    library_path: str,
    flickr_client: "FlickrClient | None" = None,
    conflict_types: list[str] | None = None,
    limit: int = 500,
) -> dict:
    """Apply pending proposals in batch. Returns totals dict.
    Pass limit=0 for no cap.
    """
    if conflict_types is None:
        conflict_types = ["non_conflict"]

    placeholders = ",".join("?" * len(conflict_types))
    if limit and limit > 0:
        rows = db.conn.execute(
            f"""SELECT id FROM metadata_proposals
                WHERE status = 'pending' AND conflict_type IN ({placeholders})
                ORDER BY id LIMIT ?""",
            conflict_types + [limit],
        ).fetchall()
    else:
        rows = db.conn.execute(
            f"""SELECT id FROM metadata_proposals
                WHERE status = 'pending' AND conflict_type IN ({placeholders})
                ORDER BY id""",
            conflict_types,
        ).fetchall()

    totals: dict = {"applied": 0, "failed": 0, "superseded": 0, "errors": []}
    for r in rows:
        try:
            result = apply_proposal(db, r["id"], library_path, flickr_client)
        except Exception as exc:
            log.exception("apply_batch: proposal %s raised unexpected exception", r["id"])
            totals["failed"] += 1
            totals["errors"].append({"proposal_id": r["id"], "reason": str(exc)})
            continue
        if result["ok"]:
            totals["applied"] += 1
        elif result.get("reason") in ("source_changed", "target_changed"):
            totals["superseded"] += 1
        else:
            reason = result.get("reason", "unknown")
            totals["failed"] += 1
            if reason == "stale_uuid":
                log.info("apply_batch: proposal %s permanently failed (stale UUID)", r["id"])
            else:
                totals["errors"].append({"proposal_id": r["id"], "reason": reason})
                log.warning("apply_batch: proposal %s failed: %s", r["id"], reason)
    return totals


def apply_collision_reverse(
    db: "Database",
    proposal_id: int,
    flickr_client: "FlickrClient | None" = None,
) -> dict:
    """
    Apply the current Photos value to Flickr for a collision proposal.

    Called when the user clicks "Use Photos" on the displayed flickr→photos
    collision proposal. Unlike apply_proposal on the sibling, this function
    reads the Photos value directly from the photos row so it works even when
    the photos→flickr sibling has been superseded by a sync run.

    On success: marks the displayed proposal rejected and the sibling (any
    status) applied.
    """
    row = db.conn.execute(
        """SELECT mp.id, mp.photo_id, mp.field, mp.conflict_type, mp.status,
                  mp.source, mp.target,
                  p.flickr_id,
                  p.flickr_tags, p.photos_tags,
                  p.flickr_title, p.photos_title,
                  p.flickr_description, p.photos_description
           FROM metadata_proposals mp
           JOIN photos p ON p.id = mp.photo_id
           WHERE mp.id = ?""",
        (proposal_id,),
    ).fetchone()

    if not row:
        return {"ok": False, "reason": "proposal not found"}
    if row["status"] != "pending":
        return {"ok": False, "reason": f"proposal already '{row['status']}'"}
    if row["conflict_type"] != "collision":
        return {"ok": False, "reason": "approve-reverse only valid for collision proposals"}

    flickr_id = row["flickr_id"]
    if not flickr_id:
        return {"ok": False, "reason": "photo has no flickr_id"}
    if flickr_client is None:
        return {"ok": False, "reason": "no flickr_client provided"}

    field    = row["field"]
    photo_id = row["photo_id"]

    if field == "tags":
        photos_tags = json.loads(row["photos_tags"]) if row["photos_tags"] else []
        result = _write_tags_to_flickr(db, photo_id, flickr_id, photos_tags, flickr_client)
        if not result["ok"]:
            return result
    else:
        photos_value        = (row[f"photos_{field}"] or "").strip()
        current_flickr_title = row["flickr_title"] or ""
        current_flickr_desc  = row["flickr_description"] or ""
        try:
            if field == "title":
                flickr_client.set_meta(flickr_id, title=photos_value, description=current_flickr_desc)
            else:
                flickr_client.set_meta(flickr_id, title=current_flickr_title, description=photos_value)
        except Exception as e:
            return {"ok": False, "reason": f"Flickr API error: {e}"}
        now = _now_iso()
        db.conn.execute(
            f"UPDATE photos SET flickr_{field}=?, meta_synced_flickr_at=?, updated_at=? WHERE id=?",
            (photos_value, now, now, photo_id),
        )

    # Reject the displayed proposal; mark the sibling (any status) as applied
    db.conn.execute(
        "UPDATE metadata_proposals SET status='rejected', resolved_at=?, resolution_note=? WHERE id=?",
        (_now_iso(), "collision reverse: Photos value written to Flickr", proposal_id),
    )
    sibling = db.conn.execute(
        """SELECT id FROM metadata_proposals
           WHERE photo_id=? AND field=? AND source='photos' AND conflict_type='collision'
           ORDER BY created_at DESC LIMIT 1""",
        (photo_id, field),
    ).fetchone()
    if sibling:
        _mark_applied(db, sibling["id"])

    db.conn.commit()
    log.info("collision-reverse proposal %s → Flickr  photo_id=%s  field=%s",
             proposal_id, photo_id, field)
    return {"ok": True}


def apply_manual_merge(
    db: "Database",
    proposal_id: int,
    custom_tags: list[str],
    library_path: str,
    flickr_client: "FlickrClient | None" = None,
) -> dict:
    """
    Apply a user-constructed tag set to both Photos and Flickr simultaneously.
    Only valid for pending tag collision proposals.  The caller is responsible
    for resolving the sibling collision proposal afterward.
    """
    row = db.conn.execute(
        """SELECT mp.id, mp.photo_id, mp.field, mp.conflict_type, mp.status,
                  mp.source, mp.target,
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
    if row["field"] != "tags":
        return {"ok": False, "reason": "apply-manual only valid for tag proposals"}
    if row["conflict_type"] != "collision":
        return {"ok": False, "reason": "apply-manual only valid for collision proposals"}
    if row["status"] != "pending":
        return {"ok": False, "reason": f"proposal already '{row['status']}'"}

    # Apply-time staleness checks (same contract as apply_proposal)
    src_hash = row["flickr_tags_hash"] if row["source"] == "flickr" else row["photos_tags_hash"]
    tgt_hash = row["photos_tags_hash"] if row["target"] == "photos" else row["flickr_tags_hash"]
    if src_hash != row["source_hash_at_creation"]:
        _supersede(db, proposal_id)
        return {"ok": False, "reason": "source_changed"}
    if tgt_hash != row["target_hash_at_creation"]:
        _supersede(db, proposal_id)
        return {"ok": False, "reason": "target_changed"}

    errors = []

    if row["uuid"]:
        r = _write_tags_to_photos(db, row["photo_id"], row["uuid"], custom_tags, library_path)
        if not r["ok"]:
            if r.get("stale_uuid"):
                _handle_stale_uuid(db, proposal_id, row["photo_id"])
                return {"ok": False, "reason": "stale_uuid"}
            errors.append(f"Photos: {r['reason']}")

    if row["flickr_id"]:
        if flickr_client is None:
            errors.append("Flickr: no flickr_client provided")
        else:
            r = _write_tags_to_flickr(
                db, row["photo_id"], row["flickr_id"], custom_tags, flickr_client
            )
            if not r["ok"]:
                errors.append(f"Flickr: {r['reason']}")

    if errors:
        return {"ok": False, "reason": "; ".join(errors)}

    _mark_applied(db, proposal_id)
    db.conn.commit()
    log.info("manual merge proposal %s → both  photo_id=%s  tags=%d",
             proposal_id, row["photo_id"], len(custom_tags))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_tags_to_photos(
    db: "Database", photo_id: int, uuid: str, new_tags: list[str], library_path: str
) -> dict:
    """Write tags to Photos.app and update the DB cache. Does not touch proposal state."""
    if not _photos_is_running():
        return {"ok": False, "reason": "Photos.app is not running"}
    try:
        import photoscript
    except ImportError:
        return {"ok": False, "reason": "photoscript not installed"}
    try:
        photo = photoscript.Photo(uuid)
    except Exception as e:
        if "invalid photo id" in str(e).lower():
            return {"ok": False, "reason": "stale_uuid", "stale_uuid": True}
        return {"ok": False, "reason": f"photo not found in Photos: {e}"}
    try:
        photo.keywords = new_tags
    except Exception as e:
        return {"ok": False, "reason": f"write failed: {e}"}
    try:
        written = list(photo.keywords or [])
    except Exception:
        written = new_tags
    now = _now_iso()
    db.conn.execute(
        "UPDATE photos SET photos_tags=?, photos_tags_hash=?, meta_synced_photos_at=?, updated_at=? WHERE id=?",
        (json.dumps(written), _compute_hash(written), now, now, photo_id),
    )
    return {"ok": True, "written": written}


def _write_tags_to_flickr(
    db: "Database", photo_id: int, flickr_id: str, new_tags: list[str],
    flickr_client: "FlickrClient",
) -> dict:
    """Write tags to Flickr and update the DB cache. Does not touch proposal state."""
    truncated = len(new_tags) > MAX_FLICKR_TAGS
    tags_to_write = new_tags[:MAX_FLICKR_TAGS] if truncated else new_tags
    try:
        flickr_client.set_tags(flickr_id, tags_to_write)
    except Exception as e:
        return {"ok": False, "reason": f"Flickr API error: {e}"}
    now = _now_iso()
    db.conn.execute(
        """UPDATE photos
           SET flickr_tags=?, flickr_tags_hash=?, meta_synced_flickr_at=?,
               tags_truncated_for_flickr=?, updated_at=?
           WHERE id=?""",
        (json.dumps(tags_to_write), _compute_hash(tags_to_write), now,
         1 if truncated else 0, now, photo_id),
    )
    return {"ok": True, "truncated": truncated}


def _apply_to_photos(db: "Database", row, new_tags: list[str], library_path: str) -> dict:
    uuid = row["uuid"]
    if not uuid:
        return {"ok": False, "reason": "photo has no uuid"}
    result = _write_tags_to_photos(db, row["photo_id"], uuid, new_tags, library_path)
    if not result["ok"]:
        return result
    written = result.get("written", new_tags)
    _mark_applied(db, row["id"])
    db.conn.commit()
    log.info("applied proposal %s → Photos  photo_id=%s  tags=%d",
             row["id"], row["photo_id"], len(written))
    return {"ok": True}


def _apply_to_flickr(
    db: "Database", row, new_tags: list[str], flickr_client: "FlickrClient"
) -> dict:
    flickr_id = row["flickr_id"]
    if not flickr_id:
        return {"ok": False, "reason": "photo has no flickr_id"}
    result = _write_tags_to_flickr(db, row["photo_id"], flickr_id, new_tags, flickr_client)
    if not result["ok"]:
        return result
    _mark_applied(db, row["id"])
    db.conn.commit()
    log.info("applied proposal %s → Flickr  photo_id=%s  tags=%d%s",
             row["id"], row["photo_id"],
             min(len(new_tags), MAX_FLICKR_TAGS),
             " (truncated)" if result.get("truncated") else "")
    return {"ok": True}


def _apply_text_to_photos(db: "Database", row, new_value: str) -> dict:
    field = row["field"]  # "title" or "description"
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
        if "invalid photo id" in str(e).lower():
            return {"ok": False, "reason": "stale_uuid", "stale_uuid": True}
        return {"ok": False, "reason": f"photo not found in Photos: {e}"}

    try:
        if field == "title":
            photo.title = new_value
        else:
            photo.description = new_value
    except Exception as e:
        return {"ok": False, "reason": f"write failed: {e}"}

    try:
        written = photo.title if field == "title" else photo.description
        written = (written or "").strip()
    except Exception:
        written = new_value

    now = _now_iso()
    col = f"photos_{field}"
    db.conn.execute(
        f"UPDATE photos SET {col}=?, meta_synced_photos_at=?, updated_at=? WHERE id=?",
        (written, now, now, row["photo_id"]),
    )
    _mark_applied(db, row["id"])
    db.conn.commit()
    log.info(
        "applied proposal %s → Photos  photo_id=%s  field=%s",
        row["id"], row["photo_id"], field,
    )
    return {"ok": True}


def _apply_text_to_flickr(
    db: "Database", row, new_value: str, flickr_client: "FlickrClient"
) -> dict:
    field = row["field"]
    flickr_id = row["flickr_id"]
    if not flickr_id:
        return {"ok": False, "reason": "photo has no flickr_id"}

    # set_meta requires both title and description; keep the current value for the unchanged field
    current_title = row["flickr_title"] or ""
    current_desc  = row["flickr_description"] or ""
    try:
        if field == "title":
            flickr_client.set_meta(flickr_id, title=new_value, description=current_desc)
        else:
            flickr_client.set_meta(flickr_id, title=current_title, description=new_value)
    except Exception as e:
        return {"ok": False, "reason": f"Flickr API error: {e}"}

    now = _now_iso()
    col = f"flickr_{field}"
    db.conn.execute(
        f"UPDATE photos SET {col}=?, meta_synced_flickr_at=?, updated_at=? WHERE id=?",
        (new_value, now, now, row["photo_id"]),
    )
    _mark_applied(db, row["id"])
    db.conn.commit()
    log.info(
        "applied proposal %s → Flickr  photo_id=%s  field=%s",
        row["id"], row["photo_id"], field,
    )
    return {"ok": True}


def _handle_stale_uuid(db: "Database", proposal_id: int, photo_id: int) -> None:
    """Mark a proposal failed and flag the photo row when Photos rejects the UUID."""
    now = _now_iso()
    db.conn.execute(
        "UPDATE photos SET uuid_stale=1, updated_at=? WHERE id=?",
        (now, photo_id),
    )
    db.conn.execute(
        """UPDATE metadata_proposals
           SET status='failed', resolved_at=?, resolution_note='stale_uuid'
           WHERE id=?""",
        (now, proposal_id),
    )
    db.conn.commit()
    log.warning("stale UUID: photo_id=%s proposal %s marked failed", photo_id, proposal_id)


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


def set_photo_text(
    db: "Database",
    photo_id: int,
    title: str,
    description: str,
    library_path: str,
    flickr_client: "FlickrClient | None" = None,
) -> dict:
    """
    Write title and description to both Apple Photos and Flickr simultaneously.
    Updates the DB cache and supersedes any pending title/description proposals.
    Partial success (one side fails) returns ok=True with warnings.
    """
    row = db.conn.execute(
        "SELECT id, flickr_id, uuid FROM photos WHERE id = ?",
        (photo_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "reason": "photo not found"}

    flickr_id = row["flickr_id"]
    uuid = row["uuid"]
    warnings = []

    if uuid:
        r = _write_text_to_photos_both(db, photo_id, uuid, title, description)
        if not r["ok"]:
            if r.get("stale_uuid"):
                db.conn.execute(
                    "UPDATE photos SET uuid_stale=1, updated_at=? WHERE id=?",
                    (_now_iso(), photo_id),
                )
                warnings.append("Photos: stale UUID — photo no longer in library")
            else:
                warnings.append(f"Photos: {r['reason']}")
    elif not flickr_id:
        return {"ok": False, "reason": "photo has neither uuid nor flickr_id"}

    if flickr_id:
        if flickr_client is None:
            warnings.append("Flickr: no client available")
        else:
            r = _write_text_to_flickr_both(db, photo_id, flickr_id, title, description, flickr_client)
            if not r["ok"]:
                warnings.append(f"Flickr: {r['reason']}")

    now = _now_iso()
    db.conn.execute(
        """UPDATE metadata_proposals
           SET status='superseded', resolved_at=?
           WHERE photo_id=? AND field IN ('title','description') AND status='pending'""",
        (now, photo_id),
    )
    db.conn.commit()

    log.info("set_photo_text photo_id=%s title=%r desc_len=%d warnings=%s",
             photo_id, title[:40], len(description), warnings)
    if warnings:
        return {"ok": True, "warnings": warnings}
    return {"ok": True}


def _write_text_to_photos_both(
    db: "Database", photo_id: int, uuid: str, title: str, description: str
) -> dict:
    """Write both title and description to Photos.app and update the DB cache."""
    if not _photos_is_running():
        return {"ok": False, "reason": "Photos.app is not running"}
    try:
        import photoscript
    except ImportError:
        return {"ok": False, "reason": "photoscript not installed"}
    try:
        photo = photoscript.Photo(uuid)
    except Exception as e:
        if "invalid photo id" in str(e).lower():
            return {"ok": False, "reason": "stale_uuid", "stale_uuid": True}
        return {"ok": False, "reason": f"photo not found in Photos: {e}"}
    try:
        photo.title = title
        photo.description = description
    except Exception as e:
        return {"ok": False, "reason": f"write failed: {e}"}
    try:
        written_title = (photo.title or "").strip()
        written_desc  = (photo.description or "").strip()
    except Exception:
        written_title = title
        written_desc  = description
    now = _now_iso()
    db.conn.execute(
        """UPDATE photos
           SET photos_title=?, photos_description=?, meta_synced_photos_at=?, updated_at=?
           WHERE id=?""",
        (written_title, written_desc, now, now, photo_id),
    )
    return {"ok": True}


def _write_text_to_flickr_both(
    db: "Database", photo_id: int, flickr_id: str, title: str, description: str,
    flickr_client: "FlickrClient",
) -> dict:
    """Write both title and description to Flickr and update the DB cache."""
    try:
        flickr_client.set_meta(flickr_id, title=title, description=description)
    except Exception as e:
        return {"ok": False, "reason": f"Flickr API error: {e}"}
    now = _now_iso()
    db.conn.execute(
        """UPDATE photos
           SET flickr_title=?, flickr_description=?, meta_synced_flickr_at=?, updated_at=?
           WHERE id=?""",
        (title, description, now, now, photo_id),
    )
    return {"ok": True}


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
