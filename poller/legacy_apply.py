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
    classify_match,
    format_legacy_metadata_trigger,
    format_legacy_trigger,
    legacy_metadata_payload,
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
    demote_all_confident: bool = False,
) -> dict:
    """Reclassify eligible (candidate_public, Flickr-only) photos from their
    legacy matches. Returns the counts dict:
        {eligible, reclassified, needs_review, auto_private, unchanged, failed,
         metadata_matched, metadata_applied, metadata_failed}

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
        "metadata_matched": 0,
        "metadata_applied": 0,
        "metadata_failed": 0,
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
            demote_all_confident=demote_all_confident,
        )
        if decision is None:
            counts["unchanged"] += 1
        else:
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
                counts["reclassified"] += 1
                counts[decision["state"]] += 1
            except Exception:
                # Per-photo atomicity: the failed write already rolled back. Isolate
                # the failure, count it, and keep processing later photos (#166).
                counts["failed"] += 1

        # Metadata propagation (#168) — independent of the demotion above; runs
        # for every photo with a legacy match, demoted or not. We deliberately
        # recompute classify_match here rather than reusing resolve_apply_decision:
        # `decision is None` covers BOTH no-match and matched-but-not-demoted, so
        # it can't tell us whether to propagate. classify_match is pure and cheap,
        # and recomputing keeps #166's resolve_apply_decision untouched.
        # Consistency is guaranteed because both classify_match and
        # resolve_apply_decision are pure functions over the same (photo,
        # candidates) inputs — the recomputation always produces the same tier
        # and matched list as the earlier call inside resolve_apply_decision.
        tier, matched = classify_match(photo, candidates)
        if matched:
            counts["metadata_matched"] += 1
            payload = legacy_metadata_payload(tier, matched)
            meta_trigger = format_legacy_metadata_trigger(tier, matched, classifier_version)
            try:
                if db.apply_legacy_metadata(
                    photo["id"],
                    add_tags=payload["add_tags"],
                    title=payload["title"],
                    description=payload["description"],
                    trigger=meta_trigger,
                ):
                    counts["metadata_applied"] += 1
            except Exception:
                counts["metadata_failed"] += 1

    return counts
