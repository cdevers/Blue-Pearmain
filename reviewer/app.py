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
import sys
from pathlib import Path

import yaml
from flask import (
    Flask, Response, abort, jsonify, redirect,
    render_template, request, send_file, url_for,
)

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.db import Database
from flickr.flickr_client import FlickrClient, FlickrError

log = logging.getLogger("blue-pearmain.reviewer")
app = Flask(__name__)

# Globals set at startup
_db: Database | None = None
_config: dict = {}
_client: FlickrClient | None = None


def db() -> Database:
    assert _db is not None
    return _db


def client() -> FlickrClient | None:
    return _client


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
        """SELECT flickr_id, uuid, original_filename, thumbnail_path,
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
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 48))
    offset = (page - 1) * per_page

    valid_states = ["candidate_public", "needs_review", "auto_private",
                    "already_public", "approved_public", "keep_private", "skipped"]
    if state_filter not in valid_states:
        state_filter = "candidate_public"

    photos = db().review_queue(
        states=[state_filter],
        limit=per_page,
        offset=offset,
    )
    total = db().review_queue_count(states=[state_filter])
    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "review.html",
        photos=photos,
        state_filter=state_filter,
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

    state = request.args.get("state", photo.get("privacy_state", "candidate_public"))

    # Find prev/next within the same state queue, ordered by date_taken
    nav = db().conn.execute(
        """SELECT id,
               LAG(id)  OVER (ORDER BY date_taken, id) AS prev_id,
               LEAD(id) OVER (ORDER BY date_taken, id) AS next_id
           FROM photos
           WHERE privacy_state = ?
        """,
        (state,),
    ).fetchall()
    prev_id = next_id = None
    for row in nav:
        if row["id"] == photo_id:
            prev_id = row["prev_id"]
            next_id = row["next_id"]
            break

    flickr_url = None
    if photo.get("flickr_id"):
        flickr_url = f"https://www.flickr.com/photos/cdevers/{photo['flickr_id']}"

    return render_template(
        "photo.html",
        photo=photo,
        flickr_url=flickr_url,
        prev_id=prev_id,
        next_id=next_id,
        state=state,
    )


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

    # Update tags if provided
    if tags is not None:
        db().conn.execute(
            "UPDATE photos SET proposed_tags = ? WHERE id = ?",
            (json.dumps(tags), photo_id),
        )
        db().conn.commit()

    db().record_review(photo_id, decision, notes)

    # Optionally push to Flickr
    if push and decision == "make_public" and photo.get("flickr_id"):
        c = client()
        if c:
            errors = []
            flickr_id = photo["flickr_id"]

            try:
                c.set_permissions(flickr_id, is_public=1)
                db().conn.execute(
                    "UPDATE photos SET perms_pushed_flickr = 1 WHERE id = ?",
                    (photo_id,)
                )
            except FlickrError as e:
                errors.append(f"perms: {e}")

            final_tags = tags if tags is not None else photo.get("proposed_tags", [])
            if final_tags:
                try:
                    existing = photo.get("flickr_tags") or []
                    from analyzer.tagger import merge_tags
                    merged = merge_tags(existing, final_tags)
                    c.add_tags(flickr_id, merged)
                    db().conn.execute(
                        "UPDATE photos SET tags_pushed_flickr = 1 WHERE id = ?",
                        (photo_id,)
                    )
                except FlickrError as e:
                    errors.append(f"tags: {e}")

            db().conn.commit()

            if errors:
                return jsonify({"ok": False, "errors": errors}), 500

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
    Serve a thumbnail. If thumbnail_path is a local file, serve it directly.
    If it's a URL, redirect to it. If missing, return a placeholder.
    """
    row = db().conn.execute(
        "SELECT thumbnail_path FROM photos WHERE id = ?", (photo_id,)
    ).fetchone()

    if not row or not row["thumbnail_path"]:
        # Return a minimal grey placeholder SVG
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240"><rect width="100%" height="100%" fill="#2a2a2a"/><text x="50%" y="50%" fill="#555" font-family="sans-serif" font-size="13" text-anchor="middle" dominant-baseline="middle">no preview</text></svg>'
        return Response(svg, mimetype="image/svg+xml")

    path = row["thumbnail_path"]

    if path.startswith("http"):
        return redirect(path)

    p = Path(path)
    if p.exists():
        return send_file(str(p), mimetype="image/jpeg")

    # File was expected locally but is missing
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240"><rect width="100%" height="100%" fill="#1a1a2a"/><text x="50%" y="50%" fill="#555" font-family="sans-serif" font-size="13" text-anchor="middle" dominant-baseline="middle">not downloaded</text></svg>'
    return Response(svg, mimetype="image/svg+xml")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def create_app(config_path: str) -> Flask:
    global _db, _config, _client

    with open(config_path) as f:
        _config = yaml.safe_load(f)
    _config["_config_path"] = config_path

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
    parser.add_argument("--host",   default="127.0.0.1")
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
