"""
app.py — Blue Pearmain review UI

A local Flask web app for working through the photo review queue.
Serves a grid of photos with keyboard shortcuts for fast triage.

Usage:
    python reviewer/app.py --config config/config.yml
    python reviewer/app.py --config config/config.yml --port 5173

Keyboard shortcuts (in grid):
    J / ↓       next photo
    K / ↑       previous photo
    P           make public (approve + push tags)
    X           keep private
    Space       skip (defer decision)
    T           edit tags
    Enter       open detail view
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import yaml
from flask import (
    Flask, Response, abort, jsonify, redirect,
    render_template, request, send_file, session, url_for,
)

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import Database
from flickr.flickr_client import FlickrClient, FlickrError, FLICKR_ERR_NOT_FOUND

log = logging.getLogger("blue-pearmain.reviewer")
app = Flask(__name__)
app.secret_key = os.urandom(24)

# Globals set at startup
_db: Database | None = None
_config: dict = {}
_client: FlickrClient | None = None


def db() -> Database:
    assert _db is not None
    return _db


def client() -> FlickrClient | None:
    return _client


@app.teardown_appcontext
def _close_db_connection(exc):
    """Close the per-thread SQLite connection at the end of every request."""
    if _db is not None:
        _db.close()


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

@app.template_filter("truncate_tags")
def truncate_tags(tags: list, n: int = 8) -> str:
    if not tags:
        return ""
    shown = tags[:n]
    rest = len(tags) - n
    s = ", ".join(shown)
    return f"{s} +{rest}" if rest > 0 else s


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    stats = db().stats()
    recent = db().conn.execute(
        """SELECT id, flickr_id, uuid, original_filename, thumbnail_path,
                  privacy_state, review_decision, reviewed_at
           FROM photos
           WHERE reviewed_at IS NOT NULL
           ORDER BY reviewed_at DESC LIMIT 12"""
    ).fetchall()
    return render_template(
        "dashboard.html",
        stats=stats,
        recent=[dict(r) for r in recent],
    )


@app.route("/review")
def review():
    state_filter = request.args.get("state", "candidate_public")
    person_filter = request.args.get("person", "").strip()
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 120))
    offset = (page - 1) * per_page

    valid_states = ["candidate_public", "needs_review", "auto_private",
                    "already_public", "approved_public", "keep_private", "skipped"]
    if state_filter not in valid_states:
        state_filter = "candidate_public"

    if person_filter:
        # Filter by person using json_each
        rows = db().conn.execute(
            """SELECT DISTINCT photos.*
               FROM photos, json_each(photos.apple_persons) AS p
               WHERE p.value = ?
                 AND photos.privacy_state = ?
               ORDER BY photos.date_taken ASC
               LIMIT ? OFFSET ?""",
            (person_filter, state_filter, per_page, offset)
        ).fetchall()
        photos = []
        for row in rows:
            d = dict(row)
            import json as _json
            for field in ("apple_labels", "apple_persons", "proposed_tags"):
                if isinstance(d.get(field), str):
                    try: d[field] = _json.loads(d[field])
                    except: d[field] = []
            photos.append(d)

        total_row = db().conn.execute(
            """SELECT COUNT(DISTINCT photos.id) AS n
               FROM photos, json_each(photos.apple_persons) AS p
               WHERE p.value = ? AND photos.privacy_state = ?""",
            (person_filter, state_filter)
        ).fetchone()
        total = total_row["n"] if total_row else 0
    else:
        photos = db().review_queue(
            states=[state_filter],
            limit=per_page,
            offset=offset,
        )
        total = db().review_queue_count(states=[state_filter])

    total_pages = max(1, (total + per_page - 1) // per_page)

    # Attach album count to each photo dict for the grid badge
    photo_ids = [p["id"] for p in photos]
    album_counts = db().get_album_counts_for_photos(photo_ids)
    for p in photos:
        p["album_count"] = album_counts.get(p["id"], 0)

    return render_template(
        "review.html",
        photos=photos,
        state_filter=state_filter,
        person_filter=person_filter,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        stats=db().stats(),
    )


@app.route("/photo/<int:photo_id>")
def photo_detail(photo_id: int):
    photo = db().get_photo(photo_id)
    if not photo:
        abort(404)

    state        = request.args.get("state", photo.get("privacy_state", "candidate_public"))
    person_filter = request.args.get("person", "").strip()

    prev_id, next_id = db().get_photo_nav(
        photo_id, state, photo.get("date_taken"), person_filter or None
    )

    flickr_url = None
    if photo.get("flickr_id"):
        flickr_username = _config.get("flickr", {}).get("username") or \
                          _config.get("flickr", {}).get("user_nsid", "")
        flickr_url = f"https://www.flickr.com/photos/{flickr_username}/{photo['flickr_id']}"

    albums = db().get_photo_albums(photo_id)

    return render_template(
        "photo.html",
        photo=photo,
        flickr_url=flickr_url,
        prev_id=prev_id,
        next_id=next_id,
        state=state,
        person_filter=person_filter,
        albums=albums,
    )


@app.route("/faces")
def faces():
    """People directory — aggregated from apple_persons across all photos."""
    # Aggregate named persons using SQLite's json_each
    rows = db().conn.execute(
        """SELECT p.value AS person,
                  COUNT(*) AS photo_count,
                  SUM(CASE WHEN privacy_state IN ('approved_public','already_public') THEN 1 ELSE 0 END) AS public_count,
                  SUM(CASE WHEN privacy_state = 'keep_private' THEN 1 ELSE 0 END) AS private_count,
                  SUM(CASE WHEN privacy_state IN ('needs_review','candidate_public') THEN 1 ELSE 0 END) AS pending_count
           FROM photos, json_each(photos.apple_persons) AS p
           WHERE photos.apple_persons IS NOT NULL
             AND photos.apple_persons NOT IN ('null', '[]', '')
             AND p.value != '_UNKNOWN_'
           GROUP BY p.value
           ORDER BY photo_count DESC"""
    ).fetchall()

    named = [dict(r) for r in rows]

    # Count unknown separately
    unknown_count = db().conn.execute(
        """SELECT COUNT(*) AS n
           FROM photos, json_each(photos.apple_persons) AS p
           WHERE p.value = '_UNKNOWN_'"""
    ).fetchone()["n"]

    unknown_photos = db().conn.execute(
        """SELECT COUNT(DISTINCT photos.id) AS n
           FROM photos, json_each(photos.apple_persons) AS p
           WHERE p.value = '_UNKNOWN_'"""
    ).fetchone()["n"]

    return render_template(
        "faces.html",
        named=named,
        unknown_count=unknown_count,
        unknown_photos=unknown_photos,
        stats=db().stats(),
    )


@app.route("/api/batch_person", methods=["POST"])
def api_batch_person():
    """
    Batch-set privacy decision for all photos containing a named person.
    decision: 'keep_private' | 'make_public'
    """
    data = request.get_json(force=True)
    person   = data.get("person", "").strip()
    decision = data.get("decision")

    if not person or decision not in ("keep_private", "make_public"):
        return jsonify({"ok": False, "error": "invalid params"}), 400

    new_state = "approved_public" if decision == "make_public" else "keep_private"

    # Find all photos containing this person that haven't been reviewed yet
    rows = db().conn.execute(
        """SELECT DISTINCT photos.id
           FROM photos, json_each(photos.apple_persons) AS p
           WHERE p.value = ?
             AND photos.privacy_state NOT IN ('already_public')""",
        (person,)
    ).fetchall()

    count = 0
    for row in rows:
        db().conn.execute(
            """UPDATE photos
               SET privacy_state = ?, privacy_reason = ?,
                   review_decision = ?, reviewed_at = datetime('now')
               WHERE id = ?""",
            (new_state, f"batch: {person}", decision, row["id"])
        )
        count += 1

    db().conn.commit()
    return jsonify({"ok": True, "updated": count, "person": person, "decision": decision})


@app.route("/duplicates")
def duplicates():
    try:
        rows = db().conn.execute("""
            SELECT
                dg.id          AS group_id,
                dg.match_key,
                dg.group_type,
                dg.photo_count,
                dg.keeper_id,
                dg.resolved,
                dg.notes,
                p.id           AS photo_id,
                p.flickr_id,
                p.uuid,
                p.original_filename,
                p.width,
                p.height,
                p.date_taken,
                p.duplicate_role,
                p.thumbnail_path,
                p.flickr_secret,
                p.flickr_server,
                p.privacy_state
            FROM duplicate_groups dg
            JOIN photos p ON p.duplicate_group_id = dg.id
            WHERE dg.resolved = 0
            ORDER BY
                CASE dg.group_type
                    WHEN 'snapbridge'    THEN 0
                    WHEN 'device_upload' THEN 1
                    ELSE 2
                END,
                dg.id,
                CASE p.duplicate_role
                    WHEN 'keeper'  THEN 0
                    WHEN 'discard' THEN 1
                    ELSE 2
                END,
                p.id
        """).fetchall()
    except Exception:
        rows = []

    # Aggregate rows into groups, preserving ORDER BY order
    groups: dict[int, dict] = {}
    for r in rows:
        gid = r["group_id"]
        if gid not in groups:
            key = r["match_key"] or ""
            filename, _, date_key = key.partition("|")
            groups[gid] = {
                "id":          gid,
                "match_key":   key,
                "group_type":  r["group_type"],
                "photo_count": r["photo_count"],
                "keeper_id":   r["keeper_id"],
                "resolved":    r["resolved"],
                "notes":       r["notes"],
                "filename":    filename,
                "date_key":    date_key,
                "photos":      [],
            }
        groups[gid]["photos"].append({
            "id":                r["photo_id"],
            "flickr_id":         r["flickr_id"],
            "uuid":              r["uuid"],
            "original_filename": r["original_filename"],
            "width":             r["width"],
            "height":            r["height"],
            "date_taken":        r["date_taken"],
            "duplicate_role":    r["duplicate_role"],
            "thumbnail_path":    r["thumbnail_path"],
            "flickr_secret":     r["flickr_secret"],
            "flickr_server":     r["flickr_server"],
            "privacy_state":     r["privacy_state"],
        })

    sections = []
    for gtype, label, description in (
        ("snapbridge",    "Snapbridge",    "Low-res phone preview vs. full-res card import — keeper is the higher-resolution copy"),
        ("device_upload", "Device upload", "Same file uploaded from multiple devices — keeper is the earlier Flickr upload"),
        ("uncertain",     "Uncertain",     "Same filename and timestamp but pattern unclear — needs manual review"),
    ):
        type_groups = [g for g in groups.values() if g["group_type"] == gtype]
        if type_groups:
            sections.append({
                "type":        gtype,
                "label":       label,
                "description": description,
                "groups":      type_groups,
            })

    total_unresolved = sum(len(s["groups"]) for s in sections)
    flickr_username = _config.get("flickr", {}).get("username") or \
                      _config.get("flickr", {}).get("user_nsid", "")
    return render_template(
        "duplicates.html",
        sections=sections,
        total_unresolved=total_unresolved,
        stats=db().stats(),
        flickr_username=flickr_username,
    )


@app.route("/api/duplicates/<int:group_id>/resolve", methods=["POST"])
def api_dup_resolve(group_id: int):
    row = db().conn.execute(
        "SELECT id FROM duplicate_groups WHERE id = ?", (group_id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    db().conn.execute(
        "UPDATE duplicate_groups SET resolved = 1, resolved_at = datetime('now') WHERE id = ?",
        (group_id,),
    )
    db().conn.commit()
    return jsonify({"ok": True})


@app.route("/api/duplicates/<int:group_id>/assign", methods=["POST"])
def api_dup_assign(group_id: int):
    data   = request.get_json(force=True)
    action = data.get("action")

    group = db().conn.execute(
        "SELECT id FROM duplicate_groups WHERE id = ?", (group_id,)
    ).fetchone()
    if not group:
        return jsonify({"ok": False, "error": "not found"}), 404

    if action == "set_keeper":
        photo_id = data.get("photo_id")
        if not photo_id:
            return jsonify({"ok": False, "error": "missing photo_id"}), 400
        member = db().conn.execute(
            "SELECT id FROM photos WHERE id = ? AND duplicate_group_id = ?",
            (photo_id, group_id),
        ).fetchone()
        if not member:
            return jsonify({"ok": False, "error": "photo not in group"}), 400
        db().conn.execute(
            "UPDATE photos SET duplicate_role = 'discard' WHERE duplicate_group_id = ?",
            (group_id,),
        )
        db().conn.execute(
            "UPDATE photos SET duplicate_role = 'keeper' WHERE id = ?",
            (photo_id,),
        )
        db().conn.execute(
            """UPDATE duplicate_groups
               SET keeper_id = ?, resolved = 1, resolved_at = datetime('now')
               WHERE id = ?""",
            (photo_id, group_id),
        )
        db().conn.commit()
        return jsonify({"ok": True})

    elif action == "not_duplicate":
        db().conn.execute(
            "UPDATE photos SET duplicate_group_id = NULL, duplicate_role = NULL WHERE duplicate_group_id = ?",
            (group_id,),
        )
        db().conn.execute("DELETE FROM duplicate_groups WHERE id = ?", (group_id,))
        db().conn.commit()
        return jsonify({"ok": True})

    else:
        return jsonify({"ok": False, "error": "invalid action"}), 400


@app.route("/settings/zones")
def zones():
    zone_rows = db().conn.execute(
        "SELECT * FROM geofence_zones ORDER BY name"
    ).fetchall()
    return render_template("zones.html", zones=[dict(r) for r in zone_rows])


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------

@app.route("/api/decide", methods=["POST"])
def api_decide():
    """Record a review decision. Optionally push to Flickr."""
    data = request.get_json(force=True)
    photo_id  = data.get("photo_id")
    decision  = data.get("decision")   # make_public | keep_private | skip
    notes     = data.get("notes", "")
    push      = data.get("push", False)
    tags      = data.get("tags")        # optional updated tag list

    if not photo_id or decision not in ("make_public", "keep_private", "skip"):
        return jsonify({"ok": False, "error": "invalid params"}), 400

    photo = db().get_photo(photo_id)
    if not photo:
        return jsonify({"ok": False, "error": "not found"}), 404

    # Capture current state for undo before writing anything
    old = db().conn.execute(
        "SELECT privacy_state, review_decision FROM photos WHERE id = ?", (photo_id,)
    ).fetchone()
    if old:
        history = session.get("undo_history", [])
        history.append({"photo_id": photo_id, "prev_state": dict(old)})
        session["undo_history"] = history[-20:]
        session.modified = True

    # Update tags if provided
    if tags is not None:
        db().conn.execute(
            "UPDATE photos SET proposed_tags = ? WHERE id = ?",
            (json.dumps(tags), photo_id),
        )
        db().conn.commit()

    db().record_review(photo_id, decision, notes)

    # Push to Flickr in a background thread so the response returns immediately
    if push and photo.get("flickr_id"):
        c = client()
        if c:
            _flickr_id  = photo["flickr_id"]
            _decision   = decision
            _photo_id   = photo_id
            _final_tags = tags if tags is not None else photo.get("proposed_tags", [])
            _existing   = photo.get("flickr_tags") or []

            def _push():
                try:
                    perms_ok = False
                    tags_ok  = False

                    if _decision == "make_public":
                        try:
                            c.set_permissions(_flickr_id, is_public=1)
                            perms_ok = True
                        except FlickrError as e:
                            log.error("background push: setPerms failed flickr_id=%s: %s", _flickr_id, e)

                    if _final_tags:
                        try:
                            from analyzer.tagger import merge_tags
                            from flickr.flickr_client import FLICKR_ERR_MAX_TAGS
                            merged = merge_tags(_existing, _final_tags)
                            c.add_tags(_flickr_id, merged)
                            tags_ok = True
                        except FlickrError as e:
                            if e.code == FLICKR_ERR_MAX_TAGS:
                                log.warning(
                                    "background push: addTags skipped flickr_id=%s: 75-tag limit",
                                    _flickr_id,
                                )
                                tags_ok = True
                            else:
                                log.error("background push: addTags failed flickr_id=%s: %s", _flickr_id, e)

                    if perms_ok:
                        db().conn.execute(
                            "UPDATE photos SET perms_pushed_flickr = 1 WHERE id = ?", (_photo_id,)
                        )
                    if tags_ok:
                        db().conn.execute(
                            "UPDATE photos SET tags_pushed_flickr = 1 WHERE id = ?", (_photo_id,)
                        )
                    if perms_ok or tags_ok:
                        db().conn.commit()

                    # Album push: for make_public, wait until perms are confirmed;
                    # for keep_private, push immediately (private photos still belong in photosets).
                    do_album_push = (perms_ok and _decision == "make_public") or _decision == "keep_private"
                    if do_album_push:
                        try:
                            from flickr.album_pusher import push_photo_to_albums
                            n = push_photo_to_albums(db(), c, _photo_id)
                            if n:
                                log.info(
                                    "background push: added to %d photoset(s) photo_id=%s",
                                    n, _photo_id,
                                )
                        except Exception as album_err:
                            log.error(
                                "background push: album sync failed photo_id=%s: %s",
                                _photo_id, album_err,
                            )

                except Exception as e:
                    log.error("background push failed photo_id=%s: %s", _photo_id, e)
                finally:
                    db().close()

            threading.Thread(target=_push, name="_push", daemon=True).start()

    return jsonify({"ok": True})


@app.route("/api/tags", methods=["POST"])
def api_tags():
    """Update proposed tags for a photo."""
    data = request.get_json(force=True)
    photo_id = data.get("photo_id")
    tags = data.get("tags", [])

    if not photo_id:
        return jsonify({"ok": False, "error": "missing photo_id"}), 400

    db().conn.execute(
        "UPDATE photos SET proposed_tags = ? WHERE id = ?",
        (json.dumps([t.strip().lower() for t in tags if t.strip()]), photo_id),
    )
    db().conn.commit()
    return jsonify({"ok": True})


@app.route("/api/undo", methods=["POST"])
def api_undo():
    """Undo the most recent review decision recorded in this session."""
    history = session.get("undo_history", [])
    if not history:
        return jsonify({"ok": False, "error": "nothing to undo"}), 400
    entry = history.pop()
    session["undo_history"] = history
    session.modified = True
    success = db().undo_decision(entry["photo_id"])
    return jsonify({"ok": success, "photo_id": entry["photo_id"]})


@app.route("/api/zone", methods=["POST"])
def api_zone():
    """Create or update a geofence zone."""
    data = request.get_json(force=True)
    required = ("name", "latitude", "longitude", "radius_m")
    if not all(data.get(k) for k in required):
        return jsonify({"ok": False, "error": "missing fields"}), 400

    zone_id = db().upsert_zone({
        "name":      data["name"],
        "label":     data.get("label", data["name"]),
        "latitude":  float(data["latitude"]),
        "longitude": float(data["longitude"]),
        "radius_m":  float(data["radius_m"]),
        "policy":    data.get("policy", "auto_private"),
        "active":    1,
        "notes":     data.get("notes", ""),
    })
    return jsonify({"ok": True, "id": zone_id})


@app.route("/api/zone/<int:zone_id>", methods=["DELETE"])
def api_zone_delete(zone_id: int):
    db().conn.execute(
        "UPDATE geofence_zones SET active = 0 WHERE id = ?", (zone_id,)
    )
    db().conn.commit()
    return jsonify({"ok": True})


@app.route("/api/stats")
def api_stats():
    return jsonify(db().stats())


@app.route("/api/open-in-photos/<int:photo_id>", methods=["POST"])
def open_in_photos(photo_id: int):
    """
    Open the photo in Photos.app via AppleScript spotlight.
    Only meaningful when called from the Mac running the reviewer.
    """
    photo = db().get_photo(photo_id)
    if not photo or not photo.get("uuid"):
        return jsonify({"ok": False, "error": "no uuid for this photo"}), 404
    uuid = photo["uuid"]
    try:
        result = subprocess.run(
            ["osascript",
             "-e", 'tell application "Photos"',
             "-e", "activate",
             "-e", f'spotlight media item id "{uuid}"',
             "-e", "end tell"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or "osascript failed"
            log.warning("open-in-photos failed for %s (uuid=%s): %s", photo_id, uuid, err)
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True})
    except Exception as e:
        log.warning("open-in-photos exception for %s: %s", photo_id, e)
        return jsonify({"ok": False, "error": str(e)})


@app.route("/conflicts")
def conflicts():
    """Show unresolved metadata conflicts queue."""
    rows = db().get_unresolved_conflicts(limit=200)
    # Group rows by photo_id so one card shows all fields for a photo
    from collections import OrderedDict
    grouped: dict = OrderedDict()
    for row in rows:
        pid = row["photo_id"]
        if pid not in grouped:
            grouped[pid] = {
                "photo_id":         pid,
                "flickr_id":        row["flickr_id"],
                "uuid":             row["uuid"],
                "original_filename": row["original_filename"],
                "thumbnail_path":   row["thumbnail_path"],
                "flickr_secret":    row["flickr_secret"],
                "flickr_server":    row["flickr_server"],
                "fields":           [],
            }
        grouped[pid]["fields"].append({
            "conflict_id":  row["id"],
            "field":        row["field"],
            "flickr_value": row["flickr_value"],
            "photos_value": row["photos_value"],
            "created_at":   row["created_at"],
        })
    return render_template(
        "conflicts.html",
        conflict_groups=list(grouped.values()),
        stats=db().stats(),
    )


@app.route("/api/conflict/<int:conflict_id>/resolve", methods=["POST"])
def api_conflict_resolve(conflict_id: int):
    """
    Resolve a single metadata conflict.
    Body JSON: {"resolution": "flickr" | "photos" | "manual"}
    Resolution is recorded in the DB only — no automatic Photos write.
    """
    data = request.get_json(silent=True) or {}
    resolution = data.get("resolution", "")
    if resolution not in ("flickr", "photos", "manual"):
        return jsonify({"ok": False, "error": "resolution must be flickr, photos, or manual"}), 400
    try:
        db().resolve_metadata_conflict(conflict_id, resolution)
        return jsonify({"ok": True})
    except Exception as e:
        log.error("conflict resolve failed id=%s: %s", conflict_id, e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/proposals")
def proposals():
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    offset   = (page - 1) * per_page
    items    = db().get_pending_proposals(limit=per_page, offset=offset)
    counts   = db().get_proposal_counts()
    total    = counts["total"]
    return render_template(
        "proposals.html",
        proposals=items,
        counts=counts,
        page=page,
        total_pages=max(1, (total + per_page - 1) // per_page),
        total=total,
    )


@app.route("/api/proposals/<int:proposal_id>/approve", methods=["POST"])
def api_proposal_approve(proposal_id: int):
    from flickr.proposal_applier import apply_proposal
    library_path = str(Path(_config.get("photos_library", {}).get("path", "")).expanduser())
    result = apply_proposal(db(), proposal_id, library_path, flickr_client=client())
    if result.get("ok"):
        sibling = db().find_collision_sibling(proposal_id)
        if sibling:
            db().resolve_proposal(sibling, "rejected", "collision sibling approved")
    return jsonify(result)


@app.route("/api/proposals/<int:proposal_id>/approve-reverse", methods=["POST"])
def api_proposal_approve_reverse(proposal_id: int):
    """Write the current Photos value to Flickr, resolving the collision."""
    from flickr.proposal_applier import apply_collision_reverse
    result = apply_collision_reverse(db(), proposal_id, flickr_client=client())
    return jsonify(result)


@app.route("/api/proposals/<int:proposal_id>/apply-manual", methods=["POST"])
def api_proposal_apply_manual(proposal_id: int):
    """Apply a user-constructed merged tag set to both Photos and Flickr."""
    data = request.get_json() or {}
    custom_tags = data.get("value")
    if not isinstance(custom_tags, list):
        return jsonify({"ok": False, "reason": "missing or invalid 'value' list"}), 400
    from flickr.proposal_applier import apply_manual_merge
    library_path = str(Path(_config.get("photos_library", {}).get("path", "")).expanduser())
    result = apply_manual_merge(db(), proposal_id, custom_tags, library_path, flickr_client=client())
    if result.get("ok"):
        sibling = db().find_collision_sibling(proposal_id)
        if sibling:
            db().resolve_proposal(sibling, "applied", "resolved via manual merge of sibling")
    return jsonify(result)


@app.route("/api/proposals/<int:proposal_id>/reject", methods=["POST"])
def api_proposal_reject(proposal_id: int):
    _d = db()
    _d.resolve_proposal(proposal_id, "rejected")
    sibling = _d.find_collision_sibling(proposal_id)
    if sibling:
        _d.resolve_proposal(sibling, "rejected", "collision sibling rejected")
    return jsonify({"ok": True})


@app.route("/api/proposals/bulk-approve", methods=["POST"])
def api_proposals_bulk_approve():
    from flickr.proposal_applier import apply_batch
    data          = request.get_json() or {}
    conflict_type = data.get("conflict_type", "non_conflict")
    library_path  = str(Path(_config.get("photos_library", {}).get("path", "")).expanduser())
    totals = apply_batch(
        db(), library_path,
        flickr_client=client(),
        conflict_types=[conflict_type],
        limit=500,
    )
    return jsonify({"ok": True, **totals})


@app.route("/api/push_approved", methods=["POST"])
def api_push_approved():
    """
    Batch-push all approved_public photos to Flickr.
    Sets permissions to public and writes tags for each.
    Returns counts of successes and failures.
    """
    c = client()
    if not c:
        return jsonify({"ok": False, "error": "Flickr client not available"}), 503

    rows = db().conn.execute(
        """SELECT id, flickr_id, proposed_tags
           FROM photos
           WHERE privacy_state = 'approved_public'
             AND flickr_id IS NOT NULL
             AND perms_pushed_flickr = 0"""
    ).fetchall()

    if not rows:
        return jsonify({"ok": True, "pushed": 0, "failed": 0, "message": "Nothing to push"})

    pushed = failed = skipped = 0
    for row in rows:
        photo_id  = row["id"]
        flickr_id = row["flickr_id"]
        tags      = _json_loads_safe(row["proposed_tags"])
        errors    = []
        not_found = False

        try:
            c.set_permissions(flickr_id, is_public=1)
            db().conn.execute(
                "UPDATE photos SET perms_pushed_flickr = 1 WHERE id = ?", (photo_id,)
            )
        except FlickrError as e:
            if e.code == FLICKR_ERR_NOT_FOUND:
                log.warning(f"Photo {flickr_id} not found on Flickr (possibly deleted); skipping")
                db().conn.execute(
                    "UPDATE photos SET perms_pushed_flickr = 1, tags_pushed_flickr = 1 WHERE id = ?",
                    (photo_id,)
                )
                not_found = True
            else:
                errors.append(str(e))

        if not not_found and tags:
            try:
                from analyzer.tagger import merge_tags
                c.add_tags(flickr_id, tags)
                db().conn.execute(
                    "UPDATE photos SET tags_pushed_flickr = 1 WHERE id = ?", (photo_id,)
                )
            except FlickrError as e:
                errors.append(str(e))

        if not_found:
            skipped += 1
        elif errors:
            failed += 1
            log.warning(f"Push failed for {flickr_id}: {errors}")
        else:
            pushed += 1

    db().conn.commit()
    return jsonify({"ok": True, "pushed": pushed, "failed": failed, "skipped": skipped})


def _json_loads_safe(value):
    if not value:
        return []
    try:
        import json as _json
        return _json.loads(value)
    except Exception:
        return []


@app.route("/api/photos/<int:photo_id>/rotate-flickr", methods=["POST"])
def api_rotate_flickr(photo_id: int):
    """Rotate a photo on Flickr clockwise by 90, 180, or 270 degrees.
    Destructive and irreversible — re-encodes the image stored on Flickr."""
    data = request.get_json(force=True, silent=True) or {}
    degrees = data.get("degrees")
    if degrees not in (90, 180, 270):
        return jsonify({"ok": False, "error": "degrees must be 90, 180, or 270"}), 400
    photo = db().get_photo(photo_id)
    if not photo:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not photo.get("flickr_id"):
        return jsonify({"ok": False, "error": "photo has no Flickr ID"}), 400
    c = client()
    if not c:
        return jsonify({"ok": False, "error": "Flickr client not available"}), 503
    try:
        c.rotate(photo["flickr_id"], degrees)
    except FlickrError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    # Accumulate rotation in DB so all views can apply the CSS correction
    current = photo.get("display_rotation") or 0
    new_rotation = (current + degrees) % 360

    # Flickr re-encodes the image on rotation, which invalidates the stored
    # secret (and therefore the CDN URL). Refresh secret/server before busting
    # the thumbnail cache so the next thumbnailer run fetches the right URL.
    new_secret = photo.get("flickr_secret") or ""
    new_server = photo.get("flickr_server") or ""
    try:
        info = c.get_photo_info(photo["flickr_id"])
        p = info.get("photo", {})
        new_secret = p.get("secret") or new_secret
        new_server = p.get("server") or new_server
    except FlickrError:
        pass  # stale secret is better than crashing; thumbnailer will retry

    db().conn.execute(
        """UPDATE photos
           SET display_rotation = ?,
               flickr_secret    = ?,
               flickr_server    = ?,
               thumbnail_path   = NULL,
               updated_at       = datetime('now')
           WHERE id = ?""",
        (new_rotation, new_secret, new_server, photo_id),
    )
    db().conn.commit()

    # Delete the stale local file (thumbnail_path already cleared above)
    old_path = photo.get("thumbnail_path") or ""
    if old_path and not old_path.startswith("http"):
        try:
            Path(old_path).unlink(missing_ok=True)
        except OSError:
            pass

    return jsonify({"ok": True, "display_rotation": new_rotation})


@app.route("/api/poll", methods=["POST"])
def api_poll():
    """Trigger a manual Flickr poll in-process (quick, last 24h only)."""
    import subprocess
    config_path = _config.get("_config_path", "config/config.yml")
    proc = subprocess.Popen(
        [sys.executable, "poller/poller.py", "--config", config_path, "--no-thumbs"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return jsonify({"ok": True, "pid": proc.pid})


# ---------------------------------------------------------------------------
# Thumbnail serving
# ---------------------------------------------------------------------------

@app.route("/thumb/<int:photo_id>")
def thumb(photo_id: int):
    """
    Serve a thumbnail. Priority order:
      1. Local file (Photos derivative or downloaded Flickr thumb)
      2. Stored URL (redirect to Flickr CDN)
      3. Flickr URL constructed on the fly from flickr_id/secret/server
      4. Placeholder SVG
    """
    row = db().conn.execute(
        "SELECT thumbnail_path, flickr_id, flickr_secret, flickr_server FROM photos WHERE id = ?",
        (photo_id,)
    ).fetchone()

    if not row:
        return _placeholder_svg("no preview")

    path = row["thumbnail_path"] or ""

    # 1. Stored URL — redirect to CDN
    if path.startswith("http"):
        return redirect(path)

    # 2. Local file
    if path:
        p = Path(path)
        if p.exists():
            return send_file(str(p), mimetype="image/jpeg")

    # 3. Construct Flickr URL on the fly if we have the pieces
    flickr_id = row["flickr_id"] or ""
    secret     = row["flickr_secret"] or ""
    server     = row["flickr_server"] or ""
    if flickr_id and secret and server:
        url = f"https://live.staticflickr.com/{server}/{flickr_id}_{secret}_b.jpg"
        return redirect(url)

    # 4. Placeholder
    label = "not downloaded" if path else "no preview"
    return _placeholder_svg(label)


def _placeholder_svg(label: str) -> Response:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240">' 
        f'<rect width="100%" height="100%" fill="#1e1e1e"/>' 
        f'<text x="50%" y="50%" fill="#555" font-family="sans-serif" ' 
        f'font-size="13" text-anchor="middle" dominant-baseline="middle">{label}</text>' 
        f'</svg>'
    )
    return Response(svg, mimetype="image/svg+xml")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _validate_config(config: dict, config_path: str):
    """
    Validate required config fields at startup.
    Raises SystemExit with a clear message rather than a cryptic KeyError later.
    """
    import sys

    required = {
        "flickr.api_key":            "Flickr API key",
        "flickr.api_secret":         "Flickr API secret",
        "flickr.oauth_token":        "Flickr OAuth token (run flickr/flickr_auth.py)",
        "flickr.oauth_token_secret": "Flickr OAuth token secret (run flickr/flickr_auth.py)",
        "database.path":             "SQLite database path",
        "thumbnails.path":           "Thumbnail cache path",
        "photos_library.path":       "Apple Photos library path",
    }

    errors = []
    for dotted_key, description in required.items():
        parts = dotted_key.split(".")
        val = config
        try:
            for part in parts:
                val = val[part]
        except (KeyError, TypeError):
            val = None
        if not val:
            errors.append(f"  {dotted_key}: {description}")

    if errors:
        print(f"\nConfiguration errors in {config_path}:")
        for e in errors:
            print(e)
        print("\nCopy config/config.example.yml to config/config.yml and fill in the missing values.")
        sys.exit(1)


def create_app(config_path: str) -> Flask:
    global _db, _config, _client

    with open(config_path) as f:
        _config = yaml.safe_load(f)
    _config["_config_path"] = config_path

    _validate_config(_config, config_path)

    db_path = Path(_config["database"]["path"]).expanduser()
    _db = Database(db_path)

    try:
        _client = FlickrClient.from_config(_config)
        _client.test_login()
        log.info("Flickr client ready")
    except Exception as e:
        log.warning(f"Flickr client unavailable: {e} — push to Flickr disabled")
        _client = None

    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Blue Pearmain review UI")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--port",   type=int, default=5173)
    parser.add_argument("--host",   default="0.0.0.0")
    parser.add_argument("--debug",  action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    create_app(args.config)
    log.info(f"Starting review UI at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
