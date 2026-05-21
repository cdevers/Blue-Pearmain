"""
poller/explain.py — DB-only explain logic for bp reconcile --explain

All functions are pure: they take dicts (from DB rows or queries) and
return explanation dicts or formatted strings. No Flickr API calls.
No side effects.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db.db import Database


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
    "approved_public": "public",
    "approved_friends": "friends-only",
    "approved_family": "family-only",
    "approved_friends_family": "friends & family",
    "keep_private": "private",
    "auto_private": "private (auto)",
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
        reason_codes      — list of stable machine-readable codes (never freeform text)
        reason            — human-readable explanation of the discrepancy
    """
    flickr_tags = set(
        t.lower().strip() for t in _json_loads_safe(row.get("flickr_tags")) if t.strip()
    )
    photos_tags = set(
        t.lower().strip() for t in _json_loads_safe(row.get("photos_tags")) if t.strip()
    )
    pushed_tags = set(
        t.lower().strip() for t in _json_loads_safe(row.get("pushed_tags")) if t.strip()
    )

    if not flickr_tags and not photos_tags and not pushed_tags:
        return None

    # Tags in Photos but not yet on Flickr
    to_push = photos_tags - flickr_tags
    # Tags we pushed that are no longer in the Flickr cache
    disappeared = pushed_tags - flickr_tags

    if not to_push and not disappeared:
        return None

    reason_codes: list[str] = []
    reasons: list[str] = []
    if to_push:
        reason_codes.append("missing_remote_tag")
        tag_list = ", ".join(sorted(to_push))
        reasons.append(f"in Photos but not on Flickr (not yet pushed): {tag_list}")
    if disappeared:
        reason_codes.append("disappeared_pushed_tag")
        tag_list = ", ".join(sorted(disappeared))
        reasons.append(f"previously pushed but missing from Flickr cache: {tag_list}")

    return {
        "last_known_flickr": sorted(flickr_tags),
        "desired": sorted(photos_tags),
        "reason_codes": reason_codes,
        "reason": "; ".join(reasons),
    }


def explain_photo_perms(row: dict) -> dict | None:
    """
    Return a permission explanation dict, or None if there is nothing to explain.

    Keys:
        desired      — human-readable desired permission label
        reason_code  — stable machine-readable code
        reason       — explanation of why the push has not happened
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
        "reason_code": "perms_not_yet_pushed",
        "reason": (f"review decision ({review_decision}, {reviewed_at}) not yet pushed to Flickr"),
    }


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
    rows = db.conn.execute(
        """SELECT id, flickr_id, flickr_title,
                  flickr_tags, photos_tags, pushed_tags,
                  privacy_state, review_decision, reviewed_at,
                  perms_pushed_flickr, tags_pushed_flickr
           FROM photos
           WHERE flickr_id IS NOT NULL
             AND (flickr_deleted IS NULL OR flickr_deleted = 0)
             AND (
               tags_pushed_flickr = 1
               OR (review_decision IS NOT NULL AND perms_pushed_flickr = 0)
             )
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
