# poller/legacy_apply.py
"""Apply legacy matches: demote matched Flickr-only candidate_public photos
out of the publish-candidate queue using the shared privacy classifier (#166).

Pure orchestration over db + legacy_match decision logic; no osxphotos, no NAS.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from legacy_match import (  # noqa: E402
    format_legacy_trigger,
    normalise_wall_clock,
    resolve_apply_decision,
)


def apply_legacy_matches(
    db,
    library_uuid: str,
    *,
    self_name: str,
    zones: list[dict],
    person_policies: dict[str, str],
    classifier_version: int,
) -> dict:
    """Reclassify eligible (candidate_public, Flickr-only) photos from their
    legacy matches. Returns the frozen counts dict (#166):
        {eligible, reclassified, needs_review, auto_private, unchanged, failed}

    `classifier_version` is captured once by the caller and threaded unchanged to
    every photo here — never re-read per photo — so one run is internally
    consistent and version monkeypatching in tests is deterministic.

    Atomicity is per photo (db.reclassify_legacy_match commits or rolls back a
    single photo). A photo whose write raises is rolled back, counted under
    `failed`, and the run continues — one bad row never aborts the batch. The
    operation is idempotent, so a failed photo is retried on the next pass.

    Invariants: reclassified + unchanged + failed == eligible, and
    needs_review + auto_private == reclassified.
    """
    by_date: dict[str, list[dict]] = defaultdict(list)
    for asset in db.iter_legacy_assets(library_uuid):
        norm = normalise_wall_clock(asset.get("date_taken"))
        if norm:
            by_date[norm].append(asset)

    photos = db.conn.execute(
        "SELECT id, flickr_id, date_taken, width, height, flickr_title "
        "FROM photos WHERE uuid IS NULL AND privacy_state = 'candidate_public'"
    ).fetchall()

    counts = {
        "eligible": len(photos),
        "reclassified": 0,
        "needs_review": 0,
        "auto_private": 0,
        "unchanged": 0,
        "failed": 0,
    }
    for p in photos:
        photo = dict(p)
        norm = normalise_wall_clock(photo.get("date_taken"))
        candidates = by_date.get(norm, []) if norm else []
        decision = resolve_apply_decision(
            photo,
            candidates,
            zones,
            self_name=self_name,
            person_policies=person_policies,
        )
        if decision is None:
            counts["unchanged"] += 1
            continue
        trigger = format_legacy_trigger(
            decision["asset_uuid"],
            decision["tier"],
            classifier_version,
        )
        try:
            db.reclassify_legacy_match(
                photo["id"],
                decision["state"],
                decision["reason"],
                trigger=trigger,
            )
        except Exception:
            # Per-photo atomicity: the failed write already rolled back. Isolate
            # the failure, count it, and keep processing later photos (#166).
            counts["failed"] += 1
            continue
        counts["reclassified"] += 1
        counts[decision["state"]] += 1

    return counts
