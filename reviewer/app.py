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
import re
import subprocess
import sys
import threading
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Any, TypedDict

import yaml
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
)
from flask.typing import ResponseReturnValue

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import Database, _json_loads_safe as _parse_json_list
from flickr.flickr_client import FlickrClient, FlickrError, FLICKR_ERR_NOT_FOUND

log = logging.getLogger("blue-pearmain.reviewer")
app = Flask(__name__)

# Return type alias used by JSON API route annotations.
# Flask routes can return a plain Response (jsonify) or a (Response, status) tuple.
_JsonResp = Response | tuple[Response, int]
app.secret_key = os.urandom(24)

# Globals set at startup
_db: Database | None = None
_config: dict = {}
_client: FlickrClient | None = None


def db() -> Database:
    assert _db is not None
    return _db


@app.before_request
def _require_xhr_for_api() -> _JsonResp | None:
    # Require X-Requested-With on all state-changing /api/ routes.
    # This blocks casual cross-origin POST requests from other pages on the same network
    # because browsers cannot set custom headers cross-origin without a CORS preflight
    # (which this server never grants). It is not a substitute for a synchronizer token
    # against a targeted attack. The reviewer UI is designed for trusted local networks only.
    if app.config.get("TESTING"):
        return None
    if (
        request.path.startswith("/api/") or request.path.startswith("/rate/")
    ) and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return jsonify({"ok": False, "error": "CSRF check failed"}), 403
    return None


def client() -> FlickrClient | None:
    return _client


@app.teardown_appcontext
def _close_db_connection(exc: BaseException | None) -> None:
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


def _parse_float(v: str | None) -> float | None:
    """Parse a query-string value to float. Returns None on missing or non-numeric input."""
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------


@app.route("/")
def dashboard() -> str:
    stats = db().stats()
    recent = (
        db()
        .conn.execute(
            """SELECT id, flickr_id, uuid, original_filename, thumbnail_path,
                  privacy_state, review_decision, reviewed_at
           FROM photos
           WHERE reviewed_at IS NOT NULL
           ORDER BY reviewed_at DESC LIMIT 12"""
        )
        .fetchall()
    )
    return render_template(
        "dashboard.html",
        stats=stats,
        recent=[dict(r) for r in recent],
    )


@app.route("/review")
def review() -> str:
    state_filter = request.args.get("state", "candidate_public")
    person_filter = request.args.get("person", "").strip()
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 120))
    offset = (page - 1) * per_page

    valid_states = [
        "candidate_public",
        "needs_review",
        "auto_private",
        "already_public",
        "approved_public",
        "keep_private",
        "skipped",
        "screenshot_unreviewed",
        "screenshot_public",
        "screenshot_private",
    ]
    _screenshot_sql: dict[str, str] = {
        "screenshot_unreviewed": "is_screenshot = 1 AND privacy_state = 'auto_private'",
        "screenshot_public": "is_screenshot = 1 AND privacy_state = 'approved_public'",
        "screenshot_private": "is_screenshot = 1 AND privacy_state = 'keep_private'",
    }
    if state_filter not in valid_states:
        state_filter = "candidate_public"

    if person_filter:
        # Filter by person using json_each
        rows = (
            db()
            .conn.execute(
                """SELECT DISTINCT photos.*
               FROM photos, json_each(photos.apple_persons) AS p
               WHERE p.value = ?
                 AND photos.privacy_state = ?
               ORDER BY photos.date_taken ASC
               LIMIT ? OFFSET ?""",
                (person_filter, state_filter, per_page, offset),
            )
            .fetchall()
        )
        photos = []
        for row in rows:
            d = dict(row)
            import json as _json

            for field in ("apple_labels", "apple_persons", "proposed_tags"):
                if isinstance(d.get(field), str):
                    try:
                        d[field] = _json.loads(d[field])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        d[field] = []
            photos.append(d)

        total_row = (
            db()
            .conn.execute(
                """SELECT COUNT(DISTINCT photos.id) AS n
               FROM photos, json_each(photos.apple_persons) AS p
               WHERE p.value = ? AND photos.privacy_state = ?""",
                (person_filter, state_filter),
            )
            .fetchone()
        )
        total = total_row["n"] if total_row else 0
    elif state_filter in _screenshot_sql:
        condition = _screenshot_sql[state_filter]
        rows = (
            db()
            .conn.execute(
                f"""SELECT id, flickr_id, original_filename,
                       apple_unknown_faces, apple_named_faces, proposed_tags,
                       display_rotation, updated_at
                FROM photos
                WHERE {condition}
                ORDER BY date_taken DESC, id DESC
                LIMIT ? OFFSET ?""",
                [per_page, offset],
            )
            .fetchall()
        )
        photos = []
        for row in rows:
            d = dict(row)
            if isinstance(d.get("proposed_tags"), str):
                try:
                    d["proposed_tags"] = json.loads(d["proposed_tags"])
                except (json.JSONDecodeError, TypeError, ValueError):
                    d["proposed_tags"] = []
            photos.append(d)
        total_row = (
            db().conn.execute(f"SELECT COUNT(*) AS n FROM photos WHERE {condition}").fetchone()
        )
        total = total_row["n"] if total_row else 0
    else:
        exclude_ss = state_filter == "candidate_public"
        photos = db().review_queue(
            states=[state_filter],
            limit=per_page,
            offset=offset,
            exclude_screenshots=exclude_ss,
        )
        total = db().review_queue_count(states=[state_filter], exclude_screenshots=exclude_ss)

    total_pages = max(1, (total + per_page - 1) // per_page)

    # Attach album count to each photo dict for the grid badge
    photo_ids = [p["id"] for p in photos]
    album_counts = db().get_album_counts_for_photos(photo_ids)
    for p in photos:
        p["album_count"] = album_counts.get(p["id"], 0)

    # Compute protection annotation for the guardrail UI
    policies = db().get_person_policies()
    private_person_names = [n for n, p in policies.items() if p == "always_private"]
    private_person_set = set(private_person_names)
    for photo in photos:
        reasons: list[str] = []
        if photo.get("geofence_zone"):
            reasons.append(f"Geofence: {photo['geofence_zone']}")
        for person in photo.get("apple_persons") or []:
            if person in private_person_set:
                reasons.append(f"Private person: {person}")
        photo["is_protected"] = bool(reasons)
        photo["protected_reasons"] = reasons

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
        private_person_names=private_person_names,
    )


@app.route("/photo/<int:photo_id>")
def photo_detail(photo_id: int) -> str:
    photo = db().get_photo(photo_id)
    if not photo:
        abort(404)

    state = request.args.get("state", photo.get("privacy_state", "candidate_public"))
    person_filter = request.args.get("person", "").strip()

    prev_id, next_id = db().get_photo_nav(
        photo_id, state, photo.get("date_taken"), person_filter or None
    )

    flickr_url = None
    flickr_edit_url: str | None = None
    if photo.get("flickr_id"):
        flickr_username = _config.get("flickr", {}).get("username") or _config.get(
            "flickr", {}
        ).get("user_nsid", "")
        flickr_base = f"https://www.flickr.com/photos/{flickr_username}/{photo['flickr_id']}"
        flickr_url = flickr_base
        if flickr_username:
            flickr_edit_url = f"{flickr_base}/edit/"

    albums = db().get_photo_albums(photo_id)

    # Compute age-at-time for each named person with a known birthday (#152)
    person_ages: dict[str, int | None] = {}
    date_taken_str = photo.get("date_taken") or ""
    if date_taken_str:
        try:
            photo_date = _date.fromisoformat(date_taken_str[:10])
            for name in photo.get("apple_persons") or []:
                bday = db().get_person_birthdays().get(name)
                if bday and len(bday) == 10:  # only YYYY-MM-DD allows age calculation
                    birth_year = int(bday[:4])
                    month, day = int(bday[5:7]), int(bday[8:10])
                    age = photo_date.year - birth_year
                    if (photo_date.month, photo_date.day) < (month, day):
                        age -= 1
                    person_ages[name] = age
        except (ValueError, TypeError):
            pass

    return render_template(
        "photo.html",
        photo=photo,
        flickr_url=flickr_url,
        flickr_edit_url=flickr_edit_url,
        prev_id=prev_id,
        next_id=next_id,
        state=state,
        person_filter=person_filter,
        albums=albums,
        person_ages=person_ages,
    )


@app.route("/faces")
def faces() -> str:
    """People directory — aggregated from apple_persons across all photos."""
    # Aggregate named persons using SQLite's json_each
    rows = (
        db()
        .conn.execute(
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
        )
        .fetchall()
    )

    named = [dict(r) for r in rows]

    # Count unknown separately
    unknown_count = (
        db()
        .conn.execute(
            """SELECT COUNT(*) AS n
           FROM photos, json_each(photos.apple_persons) AS p
           WHERE p.value = '_UNKNOWN_'"""
        )
        .fetchone()["n"]
    )

    unknown_photos = (
        db()
        .conn.execute(
            """SELECT COUNT(DISTINCT photos.id) AS n
           FROM photos, json_each(photos.apple_persons) AS p
           WHERE p.value = '_UNKNOWN_'"""
        )
        .fetchone()["n"]
    )

    return render_template(
        "faces.html",
        named=named,
        unknown_count=unknown_count,
        unknown_photos=unknown_photos,
        stats=db().stats(),
        person_policies=db().get_person_policies(),
        birthdays=db().get_person_birthdays(),
    )


@app.route("/api/batch_person", methods=["POST"])
def api_batch_person() -> _JsonResp:
    """
    Batch-set privacy decision for all photos containing a named person.
    decision: 'keep_private' | 'make_public'
    """
    data = request.get_json(force=True)
    person = data.get("person", "").strip()
    decision = data.get("decision")

    if not person or decision not in ("keep_private", "make_public"):
        return jsonify({"ok": False, "error": "invalid params"}), 400

    new_state = "approved_public" if decision == "make_public" else "keep_private"

    # Find all photos containing this person that haven't been reviewed yet
    rows = (
        db()
        .conn.execute(
            """SELECT DISTINCT photos.id
           FROM photos, json_each(photos.apple_persons) AS p
           WHERE p.value = ?
             AND photos.privacy_state NOT IN ('already_public')""",
            (person,),
        )
        .fetchall()
    )

    count = 0
    for row in rows:
        db().conn.execute(
            """UPDATE photos
               SET privacy_state = ?, privacy_reason = ?,
                   review_decision = ?, reviewed_at = datetime('now')
               WHERE id = ?""",
            (new_state, f"batch: {person}", decision, row["id"]),
        )
        count += 1

    db().conn.commit()
    return jsonify({"ok": True, "updated": count, "person": person, "decision": decision})


@app.route("/api/person_policy", methods=["POST"])
def api_person_policy() -> _JsonResp:
    """
    Set or clear a privacy policy for a named person.

    Request body: {"person": "Alice", "policy": "always_private" | null}
    policy=null removes any existing policy for that person.
    """
    data = request.get_json(force=True)
    person = (data.get("person") or "").strip()
    policy = data.get("policy")

    if not person:
        return jsonify({"ok": False, "error": "person name required"}), 400

    valid_policies = {"always_private", None}
    if policy not in valid_policies:
        return jsonify({"ok": False, "error": f"unknown policy: {policy!r}"}), 400

    if policy is None:
        db().delete_person_policy(person)
    else:
        db().set_person_policy(person, policy)

    return jsonify({"ok": True, "person": person, "policy": policy})


@app.route("/api/person_policy/<path:person_name>", methods=["GET"])
def api_get_person_policy(person_name: str) -> _JsonResp:
    """Return the current policy for a named person, or null if none."""
    policies = db().get_person_policies()
    return jsonify({"person": person_name, "policy": policies.get(person_name)})


_BIRTHDAY_RE = re.compile(r"^\d{2}-\d{2}$|^\d{4}-\d{2}-\d{2}$")


@app.route("/api/person-birthday", methods=["POST"])
def api_set_person_birthday() -> _JsonResp:
    """Upsert a birthday for a named person.

    Body: {"person_name": str, "birthday": "MM-DD" | "YYYY-MM-DD"}
    """
    data = request.get_json(force=True) or {}
    person_name = (data.get("person_name") or "").strip()
    birthday = (data.get("birthday") or "").strip()
    if not person_name or not birthday:
        return jsonify({"ok": False, "error": "person_name and birthday required"}), 400
    if not _BIRTHDAY_RE.match(birthday):
        return jsonify({"ok": False, "error": "birthday must be MM-DD or YYYY-MM-DD"}), 400
    db().set_person_birthday(person_name, birthday)
    return jsonify({"ok": True})


@app.route("/api/person-birthday/<path:person_name>", methods=["DELETE"])
def api_delete_person_birthday(person_name: str) -> _JsonResp:
    """Remove the birthday for a named person."""
    db().delete_person_birthday(person_name)
    return jsonify({"ok": True})


@app.route("/duplicates")
def duplicates() -> str:
    try:
        rows = (
            db()
            .conn.execute("""
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
                p.date_precision,
                p.date_approximate,
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
                    WHEN 'snapbridge'      THEN 0
                    WHEN 'edit_pair'       THEN 1
                    WHEN 'local_duplicate' THEN 2
                    WHEN 'device_upload'   THEN 3
                    ELSE 4
                END,
                dg.id,
                CASE p.duplicate_role
                    WHEN 'keeper'  THEN 0
                    WHEN 'discard' THEN 1
                    ELSE 2
                END,
                p.id
        """)
            .fetchall()
        )
    except Exception as exc:
        log.error("duplicates query failed: %s", exc, exc_info=True)
        rows = []

    # Aggregate rows into groups, preserving ORDER BY order
    groups: dict[int, dict] = {}
    for r in rows:
        gid = r["group_id"]
        if gid not in groups:
            key = r["match_key"] or ""
            gtype = r["group_type"]
            if gtype in ("reupload", "reupload_uncertain"):
                parts = key.split(":")
                filename = f"{parts[1]} → {parts[2]}" if len(parts) == 3 else key
                date_key = ""
                try:
                    notes_parsed = json.loads(r["notes"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    notes_parsed = {}
            else:
                filename, _, date_key = key.partition("|")
                notes_parsed = {}
            groups[gid] = {
                "id": gid,
                "match_key": key,
                "group_type": gtype,
                "photo_count": r["photo_count"],
                "keeper_id": r["keeper_id"],
                "resolved": r["resolved"],
                "notes": r["notes"],
                "notes_parsed": notes_parsed,
                "filename": filename,
                "date_key": date_key,
                "photos": [],
            }
        groups[gid]["photos"].append(
            {
                "id": r["photo_id"],
                "flickr_id": r["flickr_id"],
                "uuid": r["uuid"],
                "original_filename": r["original_filename"],
                "width": r["width"],
                "height": r["height"],
                "date_taken": r["date_taken"],
                "date_precision": r["date_precision"],
                "date_approximate": r["date_approximate"],
                "duplicate_role": r["duplicate_role"],
                "thumbnail_path": r["thumbnail_path"],
                "flickr_secret": r["flickr_secret"],
                "flickr_server": r["flickr_server"],
                "privacy_state": r["privacy_state"],
            }
        )

    # Annotate each group with thumbnail availability and merge candidate data
    for g in groups.values():
        g["has_all_thumbs"] = all(
            p["thumbnail_path"] or (p["flickr_secret"] and p["flickr_server"]) for p in g["photos"]
        )
        # Photos-linked records sorted highest-res first (merge targets)
        photos_targets = sorted(
            [p for p in g["photos"] if p.get("uuid")],
            key=lambda p: (p.get("width") or 0) * (p.get("height") or 0),
            reverse=True,
        )
        g["flickr_only_ids"] = {
            p["id"] for p in g["photos"] if p.get("flickr_id") and not p.get("uuid")
        }
        g["photos_targets"] = [
            {
                "id": p["id"],
                "label": (
                    f"{p['original_filename']} ({p['width']}×{p['height']}px)"
                    if p.get("width") and p.get("height")
                    else p["original_filename"]
                ),
            }
            for p in photos_targets
        ]

    sections = []
    for gtype, label, description in (
        (
            "snapbridge",
            "Snapbridge",
            "Low-res phone preview vs. full-res card import — keeper is the higher-resolution copy",
        ),
        (
            "edit_pair",
            "Edit pair",
            "Same filename and timestamp, different content — typically an original and an edited, "
            "cropped, or colour-corrected version. Use 'Not a duplicate' if you want to keep both.",
        ),
        (
            "local_duplicate",
            "Local duplicate",
            "Same image imported multiple times into your Photos library. "
            "One copy is already on Flickr; the others were never uploaded. "
            "Use 'Not a duplicate' to dismiss from review.",
        ),
        (
            "device_upload",
            "Device upload",
            "Same file uploaded from multiple devices — keeper is the earlier Flickr upload",
        ),
        (
            "uncertain",
            "Uncertain",
            "Same filename and timestamp but pattern unclear. "
            "May be intentional edits, camera firmware quirks, or burst-mode stills. "
            "Review carefully — “Not a duplicate” is safe to use if you want to keep both.",
        ),
        (
            "reupload",
            "Re-upload duplicate",
            "Higher-res Flickr copy of a local photo — discard has been marked duplicate_flickr.",
        ),
        (
            "reupload_uncertain",
            "Possible re-upload",
            "Probable re-upload — needs human review before marking or deleting.",
        ),
    ):
        type_groups = [g for g in groups.values() if g["group_type"] == gtype]
        if type_groups:
            # Groups with all thumbnails first; missing-thumbnail groups last
            type_groups.sort(key=lambda g: (0 if g["has_all_thumbs"] else 1, g["id"]))
            sections.append(
                {
                    "type": gtype,
                    "label": label,
                    "description": description,
                    "groups": type_groups,
                }
            )

    total_unresolved = sum(len(s["groups"]) for s in sections)
    flickr_username = _config.get("flickr", {}).get("username") or _config.get("flickr", {}).get(
        "user_nsid", ""
    )
    return render_template(
        "duplicates.html",
        sections=sections,
        total_unresolved=total_unresolved,
        stats=db().stats(),
        flickr_username=flickr_username,
    )


@app.route("/api/duplicates/<int:group_id>/resolve", methods=["POST"])
def api_dup_resolve(group_id: int) -> _JsonResp:
    row = db().conn.execute("SELECT id FROM duplicate_groups WHERE id = ?", (group_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    db().conn.execute(
        "UPDATE duplicate_groups SET resolved = 1, resolved_at = datetime('now') WHERE id = ?",
        (group_id,),
    )
    db().conn.commit()
    return jsonify({"ok": True})


@app.route("/api/duplicates/<int:group_id>/assign", methods=["POST"])
def api_dup_assign(group_id: int) -> _JsonResp:
    data = request.get_json(force=True)
    action = data.get("action")

    group = (
        db().conn.execute("SELECT id FROM duplicate_groups WHERE id = ?", (group_id,)).fetchone()
    )
    if not group:
        return jsonify({"ok": False, "error": "not found"}), 404

    if action == "set_keeper":
        photo_id = data.get("photo_id")
        if not photo_id:
            return jsonify({"ok": False, "error": "missing photo_id"}), 400
        member = (
            db()
            .conn.execute(
                "SELECT id FROM photos WHERE id = ? AND duplicate_group_id = ?",
                (photo_id, group_id),
            )
            .fetchone()
        )
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

    elif action == "merge":
        donor_id = data.get("donor_id")
        target_id = data.get("target_id")
        if not donor_id or not target_id:
            return jsonify({"ok": False, "error": "missing donor_id or target_id"}), 400
        for pid in (donor_id, target_id):
            member = (
                db()
                .conn.execute(
                    "SELECT id FROM photos WHERE id = ? AND duplicate_group_id = ?",
                    (pid, group_id),
                )
                .fetchone()
            )
            if not member:
                return jsonify({"ok": False, "error": f"photo {pid} not in group"}), 400
        try:
            db().merge_flickr_donor_in_group(donor_id, target_id, group_id)
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    else:
        return jsonify({"ok": False, "error": "invalid action"}), 400


@app.route("/settings/zones")
def zones() -> str:
    zone_rows = db().conn.execute("SELECT * FROM geofence_zones ORDER BY name").fetchall()
    return render_template("zones.html", zones=[dict(r) for r in zone_rows])


@app.route("/albums")
def albums_index() -> str:
    albums = db().get_all_albums_with_counts()
    return render_template("albums.html", albums=albums)


@app.route("/api/albums/<int:album_id>", methods=["PATCH"])
def api_album_rename(album_id: int) -> _JsonResp:
    row = (
        db()
        .conn.execute(
            "SELECT id, name FROM albums WHERE id = ? AND deleted_at IS NULL", (album_id,)
        )
        .fetchone()
    )
    if not row:
        return jsonify({"ok": False, "error": "album not found"}), 404

    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    if not isinstance(name, str) or not name.strip():
        return jsonify({"ok": False, "error": "name must be a non-empty string"}), 400

    name = name.strip()
    db().rename_album(album_id, name)
    return jsonify({"ok": True, "name": name})


@app.route("/api/albums/<int:album_id>", methods=["DELETE"])
def api_album_delete(album_id: int) -> _JsonResp:
    # Check the album row exists at all (regardless of deleted_at)
    row = (
        db().conn.execute("SELECT id, deleted_at FROM albums WHERE id = ?", (album_id,)).fetchone()
    )
    if not row:
        return jsonify({"ok": False, "error": "album not found"}), 404

    # Idempotent: already-deleted albums are a no-op, not an error
    if row["deleted_at"] is None:
        db().mark_album_deleted(album_id)
    return jsonify({"ok": True})


def _safe_year(key: str) -> int | None:
    """Parse a year from request.args[key]; kept for any callers outside normalize_shared_filters."""
    raw = request.args.get(key)
    if not raw:
        return None
    try:
        y = int(raw)
    except ValueError:
        return None
    return y if 1800 <= y <= 2099 else None


def _safe_date(key: str) -> str | None:
    """Parse a YYYY-MM-DD date string from request.args[key]; return None if missing/invalid."""
    val = (request.args.get(key) or "").strip()
    if not val:
        return None
    try:
        return str(_date.fromisoformat(val))  # canonical YYYY-MM-DD; rejects non-YYYY-MM-DD formats
    except ValueError:
        return None


class SharedFilters(TypedDict):
    time_pattern: str
    date_from: str | None  # YYYY-MM-DD or None
    date_to: str | None  # YYYY-MM-DD inclusive end, or None
    album_id: int | None
    person: str
    status: str
    expand: str
    tag: str  # "" when absent; whitespace-stripped


# Valid status values — mirrors _STATUS_STATES in db.py
_VALID_STATUSES: frozenset[str] = frozenset(
    ["public", "friends", "family", "friends_family", "private", "pending"]
)


@app.template_filter("format_date")
def _format_date_filter(s: str) -> str:
    """Format a YYYY-MM-DD string as 'Jun 15, 2018'."""
    try:
        return _date.fromisoformat(s).strftime("%b %-d, %Y")
    except (ValueError, AttributeError):
        return s


@app.template_filter("date_display")
def _date_display_filter(
    date_taken: str | None,
    precision: str | None = None,
    approximate: int = 0,
) -> str:
    """Jinja filter: {{ photo.date_taken | date_display(photo.date_precision, photo.date_approximate) }}"""
    from db.date_precision import format_date_precision

    return format_date_precision(date_taken, precision, bool(approximate))


def normalize_shared_filters() -> SharedFilters:
    """Parse and normalize the shared filter params from request.args.

    Single normalization entry point for both library() and map_view().
    Reads date_from/date_to directly; falls back to legacy year_from/year_to
    params (converting them to ISO date strings) when date params are absent.
    """
    # Primary: explicit date params
    date_from = _safe_date("date_from")
    date_to = _safe_date("date_to")

    # Legacy compat: year_from / year_to → ISO date strings
    if date_from is None:
        y = _safe_year("year_from")
        if y is not None:
            date_from = f"{y:04d}-01-01"
    if date_to is None:
        y = _safe_year("year_to")
        if y is not None:
            date_to = f"{y:04d}-12-31"

    # Swap if inverted
    if date_from is not None and date_to is not None and date_from > date_to:
        date_from, date_to = date_to, date_from

    album_id: int | None = None
    raw_album = (request.args.get("album_id") or "").strip()
    if raw_album:
        try:
            album_id = int(raw_album)
        except ValueError:
            pass

    raw_status = (request.args.get("status") or "").strip()
    status = raw_status if raw_status in _VALID_STATUSES else ""

    return SharedFilters(
        time_pattern=(request.args.get("time_pattern") or "").strip(),
        date_from=date_from,
        date_to=date_to,
        album_id=album_id,
        person=(request.args.get("person") or "").strip(),
        status=status,
        expand=(request.args.get("expand") or "").strip(),
        tag=(request.args.get("tag") or "").strip(),
    )


@app.route("/map")
def map_view() -> str:
    photo_id_param = request.args.get("photo_id", type=int)
    highlight_id: int | None = None
    center_lat: float
    center_lon: float

    if photo_id_param is not None:
        row = (
            db()
            .conn.execute(
                "SELECT latitude, longitude FROM photos WHERE id = ? AND latitude IS NOT NULL AND longitude IS NOT NULL",
                (photo_id_param,),
            )
            .fetchone()
        )
        if row:
            center_lat = row["latitude"]
            center_lon = row["longitude"]
            highlight_id = photo_id_param
        else:
            photo_id_param = None  # fall through to average below

    if photo_id_param is None:
        row = (
            db()
            .conn.execute(
                "SELECT AVG(latitude) AS lat, AVG(longitude) AS lon "
                "FROM photos WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
            )
            .fetchone()
        )
        center_lat = row["lat"] if row["lat"] is not None else 20.0
        center_lon = row["lon"] if row["lon"] is not None else 0.0

    # Gather template vars for filter bar dropdowns
    albums = db().get_all_albums()

    person_names_rows = (
        db()
        .conn.execute(
            """
        SELECT DISTINCT je.value
        FROM photos p, json_each(p.apple_persons) je
        WHERE je.value != '_UNKNOWN_'
          AND je.value != ''
          AND p.apple_persons IS NOT NULL
        ORDER BY je.value
        """
        )
        .fetchall()
    )
    person_names = [r[0] for r in person_names_rows]
    tag_names = db().tag_names()

    sf = normalize_shared_filters()
    initial_filters = {
        "time_pattern": sf["time_pattern"],
        "date_from": sf["date_from"] if sf["date_from"] is not None else "",
        "date_to": sf["date_to"] if sf["date_to"] is not None else "",
        "album_id": sf["album_id"],
        "person": sf["person"],
        "status": sf["status"],
        "expand": sf["expand"],
        "tag": sf["tag"],
    }

    return render_template(
        "map.html",
        center_lat=center_lat,
        center_lon=center_lon,
        highlight_id=highlight_id,
        birthday_people=db().get_person_birthdays(),
        albums=albums,
        person_names=person_names,
        tag_names=tag_names,
        initial_filters=initial_filters,
    )


@app.route("/api/map-photos")
def api_map_photos() -> Response:
    flickr_username = _config.get("flickr", {}).get("username", "")
    time_pattern = request.args.get("time_pattern") or None
    time_expand = 2 if request.args.get("expand") == "1" else 0

    # ── Shared filter params ─────────────────────────────────────────────
    sf = normalize_shared_filters()
    album_id = sf["album_id"]
    person = (sf["person"] or "").strip()

    # ── Build WHERE fragments ────────────────────────────────────────────
    where_frags: list[str] = []
    where_params: list = []

    # Time pattern (existing logic — unchanged)
    if time_pattern:
        from db.time_patterns import parse_pattern, birthday_clause

        if time_pattern.startswith("birthday:"):
            person_name = time_pattern[9:]
            bday = db().get_person_birthdays().get(person_name)
            if bday:
                all_years = [
                    r[0]
                    for r in db()
                    .conn.execute(
                        "SELECT DISTINCT CAST(strftime('%Y', date_taken) AS INTEGER) AS y "
                        "FROM photos WHERE date_taken IS NOT NULL ORDER BY y"
                    )
                    .fetchall()
                    if r[0] is not None
                ]
                month, day = (int(x) for x in bday[-5:].split("-"))
                frag, frag_params = birthday_clause(month, day, time_expand, all_years)
                if frag != "1=1":
                    where_frags.append(frag)
                    where_params.extend(frag_params)
        else:
            years = (
                [
                    r[0]
                    for r in db()
                    .conn.execute(
                        "SELECT DISTINCT CAST(strftime('%Y', date_taken) AS INTEGER) AS y "
                        "FROM photos WHERE date_taken IS NOT NULL ORDER BY y"
                    )
                    .fetchall()
                    if r[0] is not None
                ]
                if time_pattern.startswith("holiday:")
                else []
            )
            frag, frag_params = parse_pattern(time_pattern, time_expand, years)
            if frag != "1=1":
                where_frags.append(frag)
                where_params.extend(frag_params)

    # Date range — from SharedFilters (handles legacy year params via normalization)
    if sf["date_from"]:
        where_frags.append("p.date_taken >= ?")
        where_params.append(sf["date_from"])
    if sf["date_to"]:
        exclusive_end = str(_date.fromisoformat(sf["date_to"]) + timedelta(days=1))
        where_frags.append("p.date_taken < ?")
        where_params.append(exclusive_end)

    # Album — correlated EXISTS to avoid row duplication
    if album_id is not None:
        where_frags.append(
            "EXISTS (SELECT 1 FROM photo_albums pa2 "
            "WHERE pa2.photo_id = p.id AND pa2.album_id = ? AND pa2.removed_at IS NULL)"
        )
        where_params.append(album_id)

    # Person — case-insensitive exact match against apple_persons JSON array
    if person:
        where_frags.append(
            "EXISTS (SELECT 1 FROM json_each(p.apple_persons) je WHERE LOWER(je.value) = LOWER(?))"
        )
        where_params.append(person)

    # Tag — case-sensitive exact match in either photos_tags or flickr_tags.
    # Intentionally case-sensitive for consistency with _library_where() tag logic;
    # datalist suggestions always produce exact-case values so this is correct for
    # normal usage. Do not add LOWER() without also changing _library_where().
    map_tag = (sf["tag"] or "").strip()
    if map_tag:
        where_frags.append(
            "(EXISTS (SELECT 1 FROM json_each(p.flickr_tags) WHERE value = ?) "
            "OR EXISTS (SELECT 1 FROM json_each(p.photos_tags) WHERE value = ?))"
        )
        where_params.extend([map_tag, map_tag])

    # Status (privacy scope) — dataset-level filter; same semantics as library
    _MAP_STATUS_CLAUSES: dict[str, str] = {
        "public": "p.privacy_state IN ('already_public','approved_public')",
        "friends": "p.privacy_state = 'approved_friends'",
        "family": "p.privacy_state = 'approved_family'",
        "friends_family": "p.privacy_state = 'approved_friends_family'",
        "private": "p.privacy_state IN ('keep_private','auto_private')",
        "pending": "p.privacy_state IN ('needs_review','candidate_public')",
    }
    map_status = sf["status"]
    if map_status and map_status in _MAP_STATUS_CLAUSES:
        where_frags.append(_MAP_STATUS_CLAUSES[map_status])
        # No bound parameter — SQL literals only (all values are hard-coded)

    extra_where = (" AND " + " AND ".join(where_frags)) if where_frags else ""

    rows = (
        db()
        .conn.execute(
            "SELECT p.id, p.latitude, p.longitude, p.photos_title, p.flickr_title, "
            "       p.date_taken, p.flickr_id, p.privacy_state "
            "FROM photos p "
            f"WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL "
            f"AND p.flickr_deleted = 0{extra_where} "
            "ORDER BY p.date_taken, p.id",
            # NOTE: ORDER BY date_taken, id ensures deterministic ordering for trail/animation.
            # Do NOT add a date_taken IS NOT NULL filter — photos with NULL dates are valid
            # map dots; they are excluded from trail/animation client-side.
            where_params,
        )
        .fetchall()
    )
    result = []
    for r in rows:
        title = (r["photos_title"] or r["flickr_title"] or "").strip() or "(untitled)"
        flickr_url = (
            f"https://www.flickr.com/photos/{flickr_username}/{r['flickr_id']}"
            if r["flickr_id"] and flickr_username
            else None
        )
        result.append(
            {
                "id": r["id"],
                "lat": r["latitude"],
                "lon": r["longitude"],
                "title": title,
                "date": (r["date_taken"] or "")[:10],
                "flickr_url": flickr_url,
                "privacy_state": r["privacy_state"],
            }
        )
    return jsonify(result)


@app.route("/library")
def library() -> str:
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 120))
    offset = (page - 1) * per_page

    untitled_only = request.args.get("untitled") == "1"
    no_location = request.args.get("no_location") == "1"
    confirmed_none = request.args.get("confirmed_none") == "1"

    sf = normalize_shared_filters()
    album_id = sf["album_id"]
    person: str | None = sf["person"] or None
    tag: str | None = sf["tag"] or None
    status: str | None = sf["status"] or None
    time_pattern: str | None = sf["time_pattern"] or None
    time_expand = 2 if sf["expand"] == "1" else 0

    # Use date_from/date_to from SharedFilters (handles legacy year params too)
    date_from: str | None = sf["date_from"] or None
    # date_to is the inclusive end the user selected (YYYY-MM-DD).
    # db.library_photos() uses <=, so pass next-day for correct day-level inclusion.
    date_to_display: str | None = sf["date_to"] or None
    date_to: str | None = None
    if date_to_display:
        date_to = str(_date.fromisoformat(date_to_display) + timedelta(days=1))

    q = request.args.get("q", "").strip() or None
    country = request.args.get("country") or None
    state = request.args.get("state") or None
    city = request.args.get("city") or None
    neighborhood = request.args.get("neighborhood") or None
    date_alias = request.args.get("date") or None
    if date_alias:
        date_from = date_from or date_alias
        if date_to_display is None:
            date_to_display = date_alias
            date_to = date_alias + "T23:59:59"

    lat_min = _parse_float(request.args.get("lat_min"))
    lat_max = _parse_float(request.args.get("lat_max"))
    lon_min = _parse_float(request.args.get("lon_min"))
    lon_max = _parse_float(request.args.get("lon_max"))
    # Require all four; ignore a partial set
    if not all(v is not None for v in (lat_min, lat_max, lon_min, lon_max)):
        lat_min = lat_max = lon_min = lon_max = None
    else:
        assert lat_min is not None and lat_max is not None
        assert lon_min is not None and lon_max is not None
        # Clamp to valid geographic bounds
        lat_min = max(-90.0, min(90.0, lat_min))
        lat_max = max(-90.0, min(90.0, lat_max))
        lon_min = max(-180.0, min(180.0, lon_min))
        lon_max = max(-180.0, min(180.0, lon_max))
        # Normalise ordering — silently swap inverted values
        if lat_min > lat_max:
            lat_min, lat_max = lat_max, lat_min
        if lon_min > lon_max:
            lon_min, lon_max = lon_max, lon_min

    photos = db().library_photos(
        date_from=date_from,
        date_to=date_to,
        album_id=album_id,
        tag=tag,
        status=status,
        untitled_only=untitled_only,
        no_location=no_location,
        confirmed_none=confirmed_none,
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
        limit=per_page,
        offset=offset,
    )
    total = db().library_photo_count(
        date_from=date_from,
        date_to=date_to,
        album_id=album_id,
        tag=tag,
        status=status,
        untitled_only=untitled_only,
        no_location=no_location,
        confirmed_none=confirmed_none,
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
    )
    no_location_count = db().no_location_count()
    confirmed_none_count = db().confirmed_none_count()
    location_tree = db().location_data()
    person_list = db().person_names()
    tag_names = db().tag_names()
    albums = db().get_all_albums()

    current_album = None
    if album_id is not None:
        current_album = next((a for a in albums if a["id"] == album_id), None)

    return render_template(
        "library.html",
        photos=photos,
        albums=albums,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=max(1, (total + per_page - 1) // per_page),
        current_album=current_album,
        location_tree=location_tree,
        person_names=person_list,
        tag_names=tag_names,
        no_location_count=no_location_count,
        confirmed_none_count=confirmed_none_count,
        filters={
            "date_from": date_from or "",
            "date_to": date_to_display or "",
            "album_id": album_id,
            "tag": tag or "",
            "status": status or "",
            "untitled": "1" if untitled_only else "",
            "no_location": "1" if no_location else "",
            "confirmed_none": "1" if confirmed_none else "",
            "time_pattern": time_pattern or "",
            "expand": "1" if time_expand > 0 else "",
            "q": q or "",
            "country": country or "",
            "state": state or "",
            "city": city or "",
            "neighborhood": neighborhood or "",
            "person": person or "",
            "lat_min": f"{lat_min:.5f}" if lat_min is not None else "",
            "lat_max": f"{lat_max:.5f}" if lat_max is not None else "",
            "lon_min": f"{lon_min:.5f}" if lon_min is not None else "",
            "lon_max": f"{lon_max:.5f}" if lon_max is not None else "",
        },
    )


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------


@app.route("/api/decide", methods=["POST"])
def api_decide() -> _JsonResp:
    """Record a review decision. Optionally push to Flickr."""
    data = request.get_json(force=True)
    photo_id = data.get("photo_id")
    decision = data.get("decision")  # make_public | keep_private | skip
    notes = data.get("notes", "")
    push = data.get("push", False)
    tags = data.get("tags")  # optional updated tag list
    override_note = data.get("override_note")  # None if absent; "" if blank override

    if not photo_id or decision not in (
        "make_public",
        "confirm_public",
        "keep_private",
        "skip",
        "make_friends",
        "make_family",
        "make_friends_family",
    ):
        return jsonify({"ok": False, "error": "invalid params"}), 400

    photo = db().get_photo(photo_id)
    if not photo:
        return jsonify({"ok": False, "error": "not found"}), 404

    # Capture current state for undo before writing anything
    old = (
        db()
        .conn.execute("SELECT privacy_state, review_decision FROM photos WHERE id = ?", (photo_id,))
        .fetchone()
    )
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

    # Journal the decision
    _new_state_row = (
        db().conn.execute("SELECT privacy_state FROM photos WHERE id = ?", (photo_id,)).fetchone()
    )
    _new_state = _new_state_row["privacy_state"] if _new_state_row else None
    db().log_operation(
        photo_id=photo_id,
        operation="review_decision",
        target="privacy_state",
        old_value=old["privacy_state"] if old else None,
        new_value=_new_state,
        trigger=f"decision:{decision}",
        actor="user",
    )

    # Log guardrail override if override_note was provided.
    # None means a normal (non-override) decision. "" means override with no note — still log.
    if override_note is not None and decision in ("make_public", "confirm_public"):
        _zone = photo.get("geofence_zone") or ""
        _raw_persons = photo.get("apple_persons") or "[]"
        if isinstance(_raw_persons, str):
            try:
                _persons_list = json.loads(_raw_persons)
            except Exception:
                _persons_list = []
        else:
            _persons_list = list(_raw_persons)
        _all_policies = db().get_person_policies()
        _private_lower = {k.lower() for k, v in _all_policies.items() if v == "always_private"}
        _private_in_photo = [p for p in _persons_list if p.lower() in _private_lower]

        _has_zone = bool(_zone)
        _has_person = bool(_private_in_photo)
        if _has_zone and _has_person:
            _op = "geofence_and_policy_override"
        elif _has_zone:
            _op = "geofence_override"
        else:
            _op = "policy_override"

        _trigger: dict[str, str] = {}
        if _zone:
            _trigger["zone"] = _zone
        if _private_in_photo:
            _trigger["person"] = ", ".join(_private_in_photo)
        if override_note:
            _trigger["note"] = override_note

        db().log_operation(
            photo_id=photo_id,
            operation=_op,
            target="privacy_state",
            old_value=old["privacy_state"] if old else None,
            new_value="approved_public",
            trigger=json.dumps(_trigger),
            actor="manual",
        )

    # Push to Flickr in a background thread so the response returns immediately
    if push and photo.get("flickr_id"):
        c = client()
        if c:
            _flickr_id = photo["flickr_id"]
            _decision = decision
            _photo_id = photo_id
            _final_tags = tags if tags is not None else photo.get("proposed_tags", [])
            _existing = photo.get("flickr_tags") or []

            def _push():
                try:
                    perms_ok = False
                    tags_ok = False

                    from flickr.flickr_client import state_to_perms

                    _target_state = (
                        db()
                        .conn.execute("SELECT privacy_state FROM photos WHERE id = ?", (_photo_id,))
                        .fetchone()["privacy_state"]
                    )
                    _is_pub, _is_frn, _is_fam = state_to_perms(_target_state)
                    if _is_pub or _is_frn or _is_fam:
                        try:
                            c.set_permissions(
                                _flickr_id,
                                is_public=_is_pub,
                                is_friend=_is_frn,
                                is_family=_is_fam,
                            )
                            perms_ok = True
                        except FlickrError as e:
                            log.error(
                                "background push: setPerms failed flickr_id=%s: %s", _flickr_id, e
                            )

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
                                log.error(
                                    "background push: addTags failed flickr_id=%s: %s",
                                    _flickr_id,
                                    e,
                                )

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

                    # Album push: wait until perms are confirmed for any visibility push;
                    # for keep_private, push immediately (private photos still belong in photosets).
                    do_album_push = perms_ok or _decision == "keep_private"
                    if do_album_push:
                        try:
                            from flickr.album_pusher import push_photo_to_albums

                            n = push_photo_to_albums(db(), c, _photo_id)
                            if n:
                                log.info(
                                    "background push: added to %d photoset(s) photo_id=%s",
                                    n,
                                    _photo_id,
                                )
                        except Exception as album_err:
                            log.error(
                                "background push: album sync failed photo_id=%s: %s",
                                _photo_id,
                                album_err,
                            )

                except Exception as e:
                    log.error("background push failed photo_id=%s: %s", _photo_id, e)
                finally:
                    db().close()

            threading.Thread(target=_push, name="_push", daemon=True).start()

    return jsonify({"ok": True})


@app.route("/api/tags", methods=["POST"])
def api_tags() -> _JsonResp:
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
def api_undo() -> _JsonResp:
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
def api_zone() -> _JsonResp:
    """Create or update a geofence zone."""
    data = request.get_json(force=True)
    required = ("name", "latitude", "longitude", "radius_m")
    if not all(data.get(k) for k in required):
        return jsonify({"ok": False, "error": "missing fields"}), 400

    zone_id = db().upsert_zone(
        {
            "name": data["name"],
            "label": data.get("label", data["name"]),
            "latitude": float(data["latitude"]),
            "longitude": float(data["longitude"]),
            "radius_m": float(data["radius_m"]),
            "policy": data.get("policy", "auto_private"),
            "active": 1,
            "notes": data.get("notes", ""),
        }
    )
    return jsonify({"ok": True, "id": zone_id})


@app.route("/api/zone/<int:zone_id>", methods=["DELETE"])
def api_zone_delete(zone_id: int) -> _JsonResp:
    db().conn.execute("UPDATE geofence_zones SET active = 0 WHERE id = ?", (zone_id,))
    db().conn.commit()
    return jsonify({"ok": True})


@app.route("/api/bulk-edit", methods=["POST"])
def api_bulk_edit() -> _JsonResp:
    """
    Bulk-edit metadata across a set of photos.

    Payload (JSON):
      field        str   — 'title' | 'description' | 'tags_add' | 'tags_remove'
      dry_run      bool  — if true, return counts without creating proposals
      skip_existing bool — for title/description: skip photos that already have a value
      value        str   — new text (for title/description)
      tags         list  — tags to add/remove (for tag ops)
      photo_ids    list  — explicit selection (mutually exclusive with filter)
      filter       dict  — {date_from, date_to, album_id, tag, status, untitled}

    Returns:
      {ok, proposals_created, batch_id}               (commit)
      {ok, would_update, would_skip, batch_id:null}   (dry_run)
    """
    data = request.get_json() or {}

    field = data.get("field")
    if field not in ("title", "description", "tags_add", "tags_remove"):
        return jsonify(
            {"ok": False, "error": "field must be title/description/tags_add/tags_remove"}
        ), 400

    is_tag_op = field in ("tags_add", "tags_remove")
    value: str | None = data.get("value") if not is_tag_op else None
    tags: list | None = data.get("tags") if is_tag_op else None

    if is_tag_op and not isinstance(tags, list):
        return jsonify({"ok": False, "error": "tags must be a list for tag operations"}), 400

    dry_run = bool(data.get("dry_run", False))
    skip_existing = bool(data.get("skip_existing", True))

    # Resolve photo IDs
    _filter = data.get("filter")
    photo_ids: list[int]
    filter_json: str | None = None

    if _filter is not None:
        filter_json = json.dumps(_filter)
        lat_min_f = _parse_float(_filter.get("lat_min"))
        lat_max_f = _parse_float(_filter.get("lat_max"))
        lon_min_f = _parse_float(_filter.get("lon_min"))
        lon_max_f = _parse_float(_filter.get("lon_max"))
        if not all(v is not None for v in (lat_min_f, lat_max_f, lon_min_f, lon_max_f)):
            lat_min_f = lat_max_f = lon_min_f = lon_max_f = None
        photo_ids = db().library_photo_ids(
            date_from=_filter.get("date_from"),
            date_to=_filter.get("date_to"),
            album_id=_filter.get("album_id"),
            tag=_filter.get("tag"),
            status=_filter.get("status"),
            untitled_only=bool(_filter.get("untitled")),
            time_pattern=_filter.get("time_pattern") or None,
            time_expand=2 if _filter.get("expand") == "1" else 0,
            q=_filter.get("q") or None,
            country=_filter.get("country") or None,
            state=_filter.get("state") or None,
            city=_filter.get("city") or None,
            neighborhood=_filter.get("neighborhood") or None,
            person=_filter.get("person") or None,
            lat_min=lat_min_f,
            lat_max=lat_max_f,
            lon_min=lon_min_f,
            lon_max=lon_max_f,
        )
    elif isinstance(data.get("photo_ids"), list):
        photo_ids = [int(i) for i in data["photo_ids"]]
    else:
        return jsonify({"ok": False, "error": "provide photo_ids or filter"}), 400

    if not photo_ids:
        return jsonify(
            {
                "ok": True,
                "proposals_created": 0,
                "batch_id": None,
                "would_update": 0,
                "would_skip": 0,
            }
        )

    _db = db()

    # For dry_run: compute counts without writing
    if dry_run:
        placeholders = ",".join("?" * len(photo_ids))
        rows = _db.conn.execute(
            f"""SELECT id, flickr_id, flickr_title, flickr_description,
                       flickr_tags, photos_title
                FROM photos
                WHERE id IN ({placeholders}) AND flickr_id IS NOT NULL AND flickr_deleted = 0""",
            photo_ids,
        ).fetchall()

        would_update = would_skip = 0
        for row in rows:
            if field == "title":
                existing = (row["flickr_title"] or "").strip()
                if skip_existing and existing:
                    would_skip += 1
                else:
                    would_update += 1
            elif field == "description":
                existing = (row["flickr_description"] or "").strip()
                if skip_existing and existing:
                    would_skip += 1
                else:
                    would_update += 1
            elif field == "tags_add":
                current = _parse_json_list(row["flickr_tags"])
                missing = [t for t in (tags or []) if t not in current]
                if missing:
                    would_update += 1
                else:
                    would_skip += 1
            elif field == "tags_remove":
                current = _parse_json_list(row["flickr_tags"])
                present = [t for t in (tags or []) if t in current]
                if present:
                    would_update += 1
                else:
                    would_skip += 1

        return jsonify(
            {"ok": True, "would_update": would_update, "would_skip": would_skip, "batch_id": None}
        )

    # Commit path
    operation_map = {
        "title": "set_title",
        "description": "set_description",
        "tags_add": "tags_add",
        "tags_remove": "tags_remove",
    }
    db_field_map: dict[str, str | None] = {
        "title": "title",
        "description": "description",
        "tags_add": None,
        "tags_remove": None,
    }

    # batch creation and proposal insertion are two separate SQLite commits.
    # Clean up any orphan batch record if proposal insertion throws.
    batch_id = _db.create_bulk_batch(
        operation=operation_map[field],
        field=db_field_map[field],
        value=value,
        tags=tags,
        filter_json=filter_json,
        photo_count=len(photo_ids),
    )

    try:
        created = _db.insert_bulk_proposals(
            batch_id=batch_id,
            photo_ids=photo_ids,
            field=field,
            value=value,
            tags=tags,
            skip_existing=skip_existing,
        )
    except Exception:
        _db.conn.execute("DELETE FROM bulk_batches WHERE id=?", (batch_id,))
        _db.conn.commit()
        return jsonify({"ok": False, "error": "proposal insertion failed"}), 500

    return jsonify({"ok": True, "proposals_created": created, "batch_id": batch_id})


@app.route("/api/bulk-batches/<int:batch_id>/reject", methods=["POST"])
def api_bulk_batch_reject(batch_id: int) -> _JsonResp:
    n = db().reject_bulk_batch(batch_id)
    return jsonify({"ok": True, "rejected": n})


@app.route("/api/album-membership", methods=["POST"])
def api_album_membership_write() -> _JsonResp:
    data = request.get_json(silent=True) or {}
    photo_ids: list[int] = data.get("photo_ids", [])
    add_album_ids: list[int] = data.get("add", [])
    remove_album_ids: list[int] = data.get("remove", [])

    if not photo_ids:
        return jsonify({"ok": False, "error": "photo_ids required"}), 400
    try:
        photo_ids = [int(i) for i in photo_ids]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "photo_ids must be a list of integers"}), 400

    # Validate and coerce add/remove album ID lists
    if not isinstance(add_album_ids, list) or not isinstance(remove_album_ids, list):
        return jsonify({"ok": False, "error": "add and remove must be lists"}), 400
    try:
        add_album_ids = [int(i) for i in add_album_ids]
        remove_album_ids = [int(i) for i in remove_album_ids]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "add and remove must be lists of integers"}), 400

    # Validate album IDs exist
    all_requested = set(add_album_ids) | set(remove_album_ids)
    if all_requested:
        valid_ids = {a["id"] for a in db().get_all_albums()}
        invalid = all_requested - valid_ids
        if invalid:
            return jsonify({"ok": False, "error": f"Unknown album_id(s): {sorted(invalid)}"}), 400

    try:
        added = 0
        removed = 0
        for album_id in add_album_ids:
            added += db().bulk_upsert_photo_albums(photo_ids, album_id)
        for album_id in remove_album_ids:
            removed += db().bulk_remove_photo_albums(photo_ids, album_id)
        db().conn.commit()
    except Exception:
        try:
            db().conn.rollback()
        except Exception:
            pass
        raise

    return jsonify({"ok": True, "added": added, "removed": removed})


@app.route("/api/album-membership", methods=["GET"])
def api_album_membership_read() -> _JsonResp:
    raw = request.args.get("photo_ids", "")
    if not raw:
        return jsonify({"ok": True, "membership": {}})
    try:
        photo_ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        return jsonify({"ok": False, "error": "photo_ids must be comma-separated integers"}), 400
    membership = db().get_album_membership_for_photos(photo_ids)
    # JSON keys must be strings; convert set → list for serialisation
    serialisable = {str(k): list(v) for k, v in membership.items()}
    return jsonify({"ok": True, "membership": serialisable})


@app.route("/api/stats")
def api_stats() -> _JsonResp:
    return jsonify(db().stats())


@app.route("/api/open-in-photos/<int:photo_id>", methods=["POST"])
def open_in_photos(photo_id: int) -> _JsonResp:
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
            [
                "osascript",
                "-e",
                'tell application "Photos"',
                "-e",
                "activate",
                "-e",
                f'spotlight media item id "{uuid}"',
                "-e",
                "end tell",
            ],
            capture_output=True,
            text=True,
            timeout=10,
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
def conflicts() -> str:
    """Show unresolved metadata conflicts queue."""
    rows = db().get_unresolved_conflicts(limit=200)
    # Group rows by photo_id so one card shows all fields for a photo
    from collections import OrderedDict

    grouped: dict = OrderedDict()
    for row in rows:
        pid = row["photo_id"]
        if pid not in grouped:
            grouped[pid] = {
                "photo_id": pid,
                "flickr_id": row["flickr_id"],
                "uuid": row["uuid"],
                "original_filename": row["original_filename"],
                "thumbnail_path": row["thumbnail_path"],
                "flickr_secret": row["flickr_secret"],
                "flickr_server": row["flickr_server"],
                "fields": [],
            }
        grouped[pid]["fields"].append(
            {
                "conflict_id": row["id"],
                "field": row["field"],
                "flickr_value": row["flickr_value"],
                "photos_value": row["photos_value"],
                "created_at": row["created_at"],
            }
        )
    return render_template(
        "conflicts.html",
        conflict_groups=list(grouped.values()),
        stats=db().stats(),
    )


@app.route("/api/conflict/<int:conflict_id>/resolve", methods=["POST"])
def api_conflict_resolve(conflict_id: int) -> _JsonResp:
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
def proposals() -> str:
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    offset = (page - 1) * per_page
    items = db().get_pending_proposals(limit=per_page, offset=offset)
    counts = db().get_proposal_counts()
    total = counts["total"]
    bulk_batches = db().get_pending_bulk_batches()
    return render_template(
        "proposals.html",
        proposals=items,
        counts=counts,
        page=page,
        total_pages=max(1, (total + per_page - 1) // per_page),
        total=total,
        bulk_batches=bulk_batches,
    )


@app.route("/api/proposals/<int:proposal_id>/approve", methods=["POST"])
def api_proposal_approve(proposal_id: int) -> _JsonResp:
    from flickr.proposal_applier import apply_proposal

    library_path = str(Path(_config.get("photos_library", {}).get("path", "")).expanduser())
    result = apply_proposal(db(), proposal_id, library_path, flickr_client=client())
    if result.get("ok"):
        sibling = db().find_collision_sibling(proposal_id)
        if sibling:
            db().resolve_proposal(sibling, "rejected", "collision sibling approved")
    return jsonify(result)


@app.route("/api/proposals/<int:proposal_id>/approve-reverse", methods=["POST"])
def api_proposal_approve_reverse(proposal_id: int) -> _JsonResp:
    """Write the current Photos value to Flickr, resolving the collision or divergence."""
    row = (
        db()
        .conn.execute("SELECT field FROM metadata_proposals WHERE id=?", (proposal_id,))
        .fetchone()
    )
    if row and row["field"] == "geo_location":
        from flickr.proposal_applier import apply_geo_reverse

        result = apply_geo_reverse(db(), proposal_id, flickr_client=client())
    else:
        from flickr.proposal_applier import apply_collision_reverse

        result = apply_collision_reverse(db(), proposal_id, flickr_client=client())
    return jsonify(result)


@app.route("/api/proposals/<int:proposal_id>/apply-manual", methods=["POST"])
def api_proposal_apply_manual(proposal_id: int) -> _JsonResp:
    """Apply a user-constructed merged tag set to both Photos and Flickr."""
    data = request.get_json() or {}
    custom_tags = data.get("value")
    if not isinstance(custom_tags, list):
        return jsonify({"ok": False, "reason": "missing or invalid 'value' list"}), 400
    from flickr.proposal_applier import apply_manual_merge

    library_path = str(Path(_config.get("photos_library", {}).get("path", "")).expanduser())
    result = apply_manual_merge(
        db(), proposal_id, custom_tags, library_path, flickr_client=client()
    )
    if result.get("ok"):
        sibling = db().find_collision_sibling(proposal_id)
        if sibling:
            db().resolve_proposal(sibling, "applied", "resolved via manual merge of sibling")
    return jsonify(result)


@app.route("/api/proposals/<int:proposal_id>/reject", methods=["POST"])
def api_proposal_reject(proposal_id: int) -> _JsonResp:
    _d = db()
    _d.resolve_proposal(proposal_id, "rejected")
    sibling = _d.find_collision_sibling(proposal_id)
    if sibling:
        _d.resolve_proposal(sibling, "rejected", "collision sibling rejected")
    return jsonify({"ok": True})


@app.route("/api/proposals/bulk-approve", methods=["POST"])
def api_proposals_bulk_approve() -> _JsonResp:
    from flickr.proposal_applier import apply_batch

    data = request.get_json() or {}
    conflict_type = data.get("conflict_type", "non_conflict")
    library_path = str(Path(_config.get("photos_library", {}).get("path", "")).expanduser())
    totals = apply_batch(
        db(),
        library_path,
        flickr_client=client(),
        conflict_types=[conflict_type],
        limit=500,
    )
    return jsonify({"ok": True, **totals})


@app.route("/api/push_approved", methods=["POST"])
def api_push_approved() -> _JsonResp:
    """
    Batch-push all approved_public photos to Flickr.
    Sets permissions to public and writes tags for each.
    Returns counts of successes and failures.
    """
    c = client()
    if not c:
        return jsonify({"ok": False, "error": "Flickr client not available"}), 503

    rows = (
        db()
        .conn.execute(
            """SELECT id, flickr_id, proposed_tags, privacy_state
           FROM photos
           WHERE privacy_state IN (
               'approved_public', 'approved_friends',
               'approved_family', 'approved_friends_family'
           )
             AND flickr_id IS NOT NULL
             AND perms_pushed_flickr = 0"""
        )
        .fetchall()
    )

    if not rows:
        return jsonify({"ok": True, "pushed": 0, "failed": 0, "message": "Nothing to push"})

    from flickr.flickr_client import state_to_perms

    pushed = failed = skipped = 0
    for row in rows:
        photo_id = row["id"]
        flickr_id = row["flickr_id"]
        tags = _json_loads_safe(row["proposed_tags"])
        errors = []
        not_found = False

        is_pub, is_frn, is_fam = state_to_perms(row["privacy_state"])
        try:
            c.set_permissions(flickr_id, is_public=is_pub, is_friend=is_frn, is_family=is_fam)
            db().conn.execute("UPDATE photos SET perms_pushed_flickr = 1 WHERE id = ?", (photo_id,))
        except FlickrError as e:
            if e.code == FLICKR_ERR_NOT_FOUND:
                log.warning(f"Photo {flickr_id} not found on Flickr (possibly deleted); skipping")
                db().conn.execute(
                    "UPDATE photos SET perms_pushed_flickr = 1, tags_pushed_flickr = 1 WHERE id = ?",
                    (photo_id,),
                )
                not_found = True
            else:
                errors.append(str(e))

        if not not_found and tags:
            try:
                c.add_tags(flickr_id, tags)
                db().conn.execute(
                    "UPDATE photos SET tags_pushed_flickr = 1, pushed_tags = ? WHERE id = ?",
                    (json.dumps(sorted(tags)), photo_id),
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


def _json_loads_safe(value: Any) -> Any:
    if not value:
        return []
    try:
        import json as _json

        return _json.loads(value)
    except Exception:
        return []


@app.route("/api/photos/<int:photo_id>/rotate-flickr", methods=["POST"])
def api_rotate_flickr(photo_id: int) -> _JsonResp:
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

    # display_rotation is a temporary CSS correction for the stale-thumbnail
    # window. If get_photo_info returns a fresh secret, the /thumb/ route will
    # redirect to the post-rotation CDN URL directly — no CSS correction needed.
    # Only set it when we can't refresh the secret and the CDN URL is still stale.
    current = photo.get("display_rotation") or 0

    new_secret = photo.get("flickr_secret") or ""
    new_server = photo.get("flickr_server") or ""
    info_refreshed = False
    try:
        info = c.get_photo_info(photo["flickr_id"])
        p = info.get("photo", {})
        fetched_secret = p.get("secret")
        if fetched_secret:
            new_secret = fetched_secret
            new_server = p.get("server") or new_server
            info_refreshed = True
    except FlickrError:
        pass  # stale secret is better than crashing; thumbnailer will retry

    new_rotation = 0 if info_refreshed else (current + degrees) % 360

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


@app.route("/api/photos/<int:photo_id>/set-text", methods=["POST"])
def api_set_photo_text(photo_id: int) -> _JsonResp:
    """Write title and description to both Apple Photos and Flickr."""
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    from flickr.proposal_applier import set_photo_text

    library_path = str(Path(_config.get("photos_library", {}).get("path", "")).expanduser())
    result = set_photo_text(
        db(), photo_id, title, description, library_path, flickr_client=client()
    )
    if result.get("ok"):
        return jsonify(result)
    status = 404 if result.get("reason") == "photo not found" else 502
    return jsonify(result), status


@app.route("/api/photos/<int:photo_id>/set-date-precision", methods=["POST"])
def api_set_date_precision(photo_id: int) -> _JsonResp:
    """Update date_precision and date_approximate for a photo."""
    from db.date_precision import PRECISION_VALUES

    data = request.get_json(force=True, silent=True) or {}
    precision = data.get("precision")
    approximate = bool(data.get("approximate", False))

    if precision not in PRECISION_VALUES:
        return jsonify({"ok": False, "error": "invalid precision"}), 400

    photo = db().get_photo(photo_id)
    if not photo:
        return jsonify({"ok": False, "error": "not found"}), 404

    db().conn.execute(
        "UPDATE photos SET date_precision = ?, date_approximate = ? WHERE id = ?",
        (precision, int(approximate), photo_id),
    )
    db().conn.commit()
    return jsonify({"ok": True})


@app.route("/api/geo_confirm_none", methods=["POST"])
def api_geo_confirm_none() -> _JsonResp:
    """Set or clear geo_confirmed_none for one or more photos.

    Body: {"photo_ids": [1, 2, 3], "clear": false}
    clear=true  → geo_confirmed_none = 0 (undo)
    clear=false → geo_confirmed_none = 1 (mark as no location)

    When setting (clear=false), any pending geo_location proposals are rejected.
    """
    data = request.get_json() or {}
    photo_ids = data.get("photo_ids")
    if not isinstance(photo_ids, list) or not photo_ids:
        return (
            jsonify({"ok": False, "reason": "photo_ids must be a non-empty list"}),
            400,
        )
    try:
        photo_ids = [int(i) for i in photo_ids]
    except (TypeError, ValueError):
        return (
            jsonify({"ok": False, "reason": "photo_ids must be integers"}),
            400,
        )

    clear = bool(data.get("clear", False))
    new_val = 0 if clear else 1
    _db = db()
    placeholders = ",".join("?" * len(photo_ids))
    _db.conn.execute(
        f"UPDATE photos SET geo_confirmed_none = ?, updated_at = datetime('now')"
        f" WHERE id IN ({placeholders})",
        [new_val] + photo_ids,
    )
    if not clear:
        # Cancel any pending geo proposals for the affected photos
        _db.conn.execute(
            f"UPDATE metadata_proposals SET status='rejected', resolved_at=datetime('now')"
            f" WHERE photo_id IN ({placeholders})"
            f"   AND field='geo_location' AND status='pending'",
            photo_ids,
        )
    _db.conn.commit()
    return jsonify({"ok": True, "updated": len(photo_ids)})


@app.route("/api/poll", methods=["POST"])
def api_poll() -> _JsonResp:
    """Trigger a manual Flickr poll in-process (quick, last 24h only)."""
    import subprocess

    config_path = _config.get("_config_path", "config/config.yml")
    proc = subprocess.Popen(
        [sys.executable, "poller/poller.py", "--config", config_path, "--no-thumbs"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return jsonify({"ok": True, "pid": proc.pid})


@app.route("/rate/<int:photo_id>", methods=["POST"])
def rate_photo(photo_id: int) -> _JsonResp:
    """Set a star rating (0–5) on a photo. Writes back to Apple Photos via photoscript."""
    data = request.get_json(silent=True) or {}
    rating = data.get("rating")
    if rating is None:
        return jsonify({"ok": False, "error": "missing rating"}), 400
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid rating"}), 400
    if not 0 <= rating <= 5:
        return jsonify({"ok": False, "error": "rating must be 0–5"}), 400

    # Check photo exists (uuid may be None for Flickr-only records not yet matched to Photos)
    _row = db().conn.execute("SELECT uuid FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if _row is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    uuid = _row["uuid"]

    db().set_bp_rating(photo_id, rating)

    # Write-back to Apple Photos (macOS only, fire-and-forget; skipped for Flickr-only photos)
    if uuid:
        try:
            import photoscript  # type: ignore[import]

            photo = photoscript.Photo(uuid)
            photo.favorite = rating >= 1
        except Exception as exc:
            log.warning("photoscript write failed for %s: %s", uuid, exc)

    return jsonify({"ok": True, "bp_rating": rating})


# ---------------------------------------------------------------------------
# Thumbnail serving
# ---------------------------------------------------------------------------

# Sentinel written to thumbnail_path when no derivative exists on disk.
# Prevents repeated filesystem probing for permanently-missing derivatives.
# Clear this value manually to force re-probing (e.g. after Photos regenerates
# derivatives for an import).
_SENTINEL_NO_DERIVATIVE = "__none__"


@app.route("/thumb/<int:photo_id>")
def thumb(photo_id: int) -> ResponseReturnValue:
    """
    Serve a thumbnail. Priority order:
      1. Stored URL (redirect to CDN)
      2. Local file (thumbnail_path on disk)
      3. Live derivative lookup (uuid → Photos library):
         - Hit: writes real path to DB, serves file.
         - Miss: writes '__none__' sentinel to DB so future requests
           skip filesystem probing without probing all three paths again.
      4. Flickr URL constructed on the fly from flickr_id/secret/server
      5. Placeholder SVG
    """
    row = (
        db()
        .conn.execute(
            "SELECT thumbnail_path, flickr_id, flickr_secret, flickr_server, uuid"
            " FROM photos WHERE id = ?",
            (photo_id,),
        )
        .fetchone()
    )

    if not row:
        return _placeholder_svg("no preview")

    path = row["thumbnail_path"] or ""

    # 1. Stored URL — redirect to CDN
    if path.startswith("http"):
        return redirect(path)

    # 2. Local file (skip sentinel value)
    if path and path != _SENTINEL_NO_DERIVATIVE:
        p = Path(path)
        if p.exists():
            return send_file(str(p), mimetype="image/jpeg")

    # 3. Live derivative lookup from Photos library.
    #    Skipped when path == _SENTINEL_NO_DERIVATIVE (known miss).
    uuid = row["uuid"] or ""
    if uuid and path != _SENTINEL_NO_DERIVATIVE:
        try:
            library_path = str(Path(_config.get("photos_library", {}).get("path", "")).expanduser())
            if library_path and library_path != ".":
                from poller.thumbnailer import derivative_path as _derivative_path

                deriv = _derivative_path(uuid, library_path)
                if deriv:
                    db().conn.execute(
                        "UPDATE photos SET thumbnail_path = ? WHERE id = ?",
                        (deriv, photo_id),
                    )
                    db().conn.commit()
                    return send_file(deriv, mimetype="image/jpeg")
                else:
                    # Write sentinel: no derivative found; skip probing next time.
                    db().conn.execute(
                        "UPDATE photos SET thumbnail_path = ? WHERE id = ?",
                        (_SENTINEL_NO_DERIVATIVE, photo_id),
                    )
                    db().conn.commit()
        except OSError:
            pass  # Photos library inaccessible; fall through to Flickr/placeholder

    # 4. Construct Flickr URL on the fly if we have the pieces
    flickr_id = row["flickr_id"] or ""
    secret = row["flickr_secret"] or ""
    server = row["flickr_server"] or ""
    if flickr_id and secret and server:
        url = f"https://live.staticflickr.com/{server}/{flickr_id}_{secret}_b.jpg"
        return redirect(url)

    # 5. Placeholder
    label = "no preview"
    return _placeholder_svg(label)


def _placeholder_svg(label: str) -> Response:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240">'
        f'<rect width="100%" height="100%" fill="#1e1e1e"/>'
        f'<text x="50%" y="50%" fill="#555" font-family="sans-serif" '
        f'font-size="13" text-anchor="middle" dominant-baseline="middle">{label}</text>'
        f"</svg>"
    )
    return Response(svg, mimetype="image/svg+xml")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _validate_config(config: dict, config_path: str) -> None:
    """
    Validate required config fields at startup.
    Raises SystemExit with a clear message rather than a cryptic KeyError later.
    """
    import sys

    required = {
        "flickr.api_key": "Flickr API key",
        "flickr.api_secret": "Flickr API secret",
        "flickr.oauth_token": "Flickr OAuth token (run flickr/flickr_auth.py)",
        "flickr.oauth_token_secret": "Flickr OAuth token secret (run flickr/flickr_auth.py)",
        "database.path": "SQLite database path",
        "thumbnails.path": "Thumbnail cache path",
        "photos_library.path": "Apple Photos library path",
    }

    errors = []
    for dotted_key, description in required.items():
        parts = dotted_key.split(".")
        val: Any = config
        try:
            for part in parts:
                val = val[part]  # type: ignore[index]
        except (KeyError, TypeError):
            val = None
        if not val:
            errors.append(f"  {dotted_key}: {description}")

    if errors:
        print(f"\nConfiguration errors in {config_path}:")
        for e in errors:
            print(e)
        print(
            "\nCopy config/config.example.yml to config/config.yml and fill in the missing values."
        )
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


def _start_mdns(host: str, port: int, lan_ip: str | None) -> None:
    """Register a Bonjour _http._tcp.local. service when binding on LAN.

    Called from main() when host != localhost and lan_ip is known.
    Handles missing zeroconf package gracefully (logs a warning and returns).
    Registers an atexit handler to unregister the service on shutdown.
    """
    if host in ("127.0.0.1", "localhost") or lan_ip is None:
        return
    try:
        import atexit
        import socket as _socket
        from zeroconf import ServiceInfo, Zeroconf

        info = ServiceInfo(
            "_http._tcp.local.",
            "blue-pearmain._http._tcp.local.",
            addresses=[_socket.inet_aton(lan_ip)],
            port=port,
            properties={"path": "/"},
            server="blue-pearmain.local.",
        )
        zc = Zeroconf()
        try:
            zc.register_service(info)
        except Exception:
            # Name already registered (e.g. rapid daemon restart). Unregister first.
            zc.unregister_service(info)
            zc.register_service(info)
        log.info("mDNS: registered blue-pearmain.local at http://blue-pearmain.local:%d", port)

        def _shutdown() -> None:
            zc.unregister_service(info)
            zc.close()

        atexit.register(_shutdown)
    except ImportError:
        log.warning("zeroconf not installed; mDNS registration skipped")
    except Exception as exc:
        log.warning("mDNS registration failed: %s", exc)


def main() -> None:
    import argparse

    # Pre-parse --config so we can read reviewer defaults from the config file.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default="config/config.yml")
    pre_args, _ = pre.parse_known_args()
    try:
        with open(pre_args.config) as _f:
            _pre_cfg = yaml.safe_load(_f) or {}
    except (OSError, yaml.YAMLError):
        _pre_cfg = {}
    _review_cfg = _pre_cfg.get("review", {})

    parser = argparse.ArgumentParser(description="Blue Pearmain review UI")
    parser.add_argument("--config", default=pre_args.config)
    parser.add_argument("--port", type=int, default=_review_cfg.get("port", 5173))
    parser.add_argument(
        "--host",
        default=_review_cfg.get("host", "127.0.0.1"),
        help="Interface to bind (default: 127.0.0.1, or review.host from config). "
        "Use 0.0.0.0 for LAN access, but note the UI is not hardened for "
        "internet-facing deployment.",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    from poller.bp_logging import configure

    configure("reviewer", args.debug)

    if args.host not in ("127.0.0.1", "localhost"):
        import socket

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
        except OSError:
            lan_ip = None
        log.warning(
            "Binding to all interfaces — the reviewer UI is designed for trusted local "
            "networks only and is not hardened for internet-facing deployment."
        )
        if lan_ip:
            log.warning("Accessible at http://%s:%s (LAN)", lan_ip, args.port)
    else:
        lan_ip = None

    create_app(args.config)
    log.info(
        f"Starting review UI at http://localhost:{args.port}"
        + (f"  (also http://{lan_ip}:{args.port} on LAN)" if lan_ip else "")
    )
    _start_mdns(args.host, args.port, lan_ip)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
