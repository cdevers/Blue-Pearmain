# poller/legacy_match.py
"""Non-destructive match-preview tiers for the legacy indexer (#162).

Pure logic: given a Flickr-only photo dict and candidate legacy_assets rows,
classify into confident / ambiguous / no-match and emit deterministically
ordered rows for the report/CSV.

Timestamp matching is done on local wall-clock time, NOT UTC. Flickr
date_taken is naive local capture time (EXIF DateTimeOriginal) and legacy
Apple dates are tz-aware in the photo's local zone; the same shot therefore
has identical wall-clock digits on both sides. Converting to UTC (as the
reupload/orphan matcher does, where both sides are Flickr-naive) would inject
the local offset as a false ~4-5h skew and miss nearly every match.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from deduplicator import _parse_dt  # noqa: E402
from legacy_normalize import normalize_title  # noqa: E402

from analyzer.privacy import PEOPLE_LABELS  # noqa: E402
from analyzer.tagger import propose_tags  # noqa: E402


def normalise_wall_clock(value) -> str | None:
    """Normalise a timestamp to local wall-clock 'YYYY-MM-DD HH:MM:SS'.

    Strips any timezone offset (keeping the local wall-clock digits) rather
    than converting to UTC, so naive Flickr times compare correctly against
    tz-aware legacy times. Returns None on empty/unparseable input.
    """
    if not value:
        return None
    dt = _parse_dt(value)
    if dt is None:
        return None
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


CONFIDENT = "confident"
AMBIGUOUS = "ambiguous"
NO_MATCH = "no-match"

_TIER_ORDER = {CONFIDENT: 0, AMBIGUOUS: 1, NO_MATCH: 2}


def _norm_dt(value) -> str | None:
    return normalise_wall_clock(value)


def _dims_match(photo: dict, cand: dict) -> bool:
    return photo.get("width") == cand.get("width") and photo.get("height") == cand.get("height")


def _title_conflict(photo: dict, cand: dict) -> bool:
    """Conflict only when BOTH titles are non-empty after normalization and differ."""
    a = normalize_title(photo.get("flickr_title"))
    b = normalize_title(cand.get("title"))
    if a is None or b is None:
        return False
    return a != b


def classify_match(photo: dict, candidates: list[dict]) -> tuple[str, list[dict]]:
    """Return (tier, matched_candidates). matched_candidates are the timestamp
    matches (the rows the report should show); empty for no-match."""
    pd = _norm_dt(photo.get("date_taken"))
    if pd is None:
        return NO_MATCH, []
    time_matches = [c for c in candidates if _norm_dt(c.get("date_taken")) == pd]
    if not time_matches:
        return NO_MATCH, []
    if len(time_matches) == 1:
        c = time_matches[0]
        if _dims_match(photo, c) and not _title_conflict(photo, c):
            return CONFIDENT, time_matches
        return AMBIGUOUS, time_matches
    return AMBIGUOUS, time_matches


def preview_rows(photo_candidate_pairs) -> list[dict]:
    """Build (unordered) report rows from (photo, candidates) pairs.

    confident -> one row (the match); ambiguous -> one row per timestamp
    candidate; no-match -> one row with empty asset_uuid.
    """
    rows: list[dict] = []
    for photo, candidates in photo_candidate_pairs:
        tier, matches = classify_match(photo, candidates)
        date_norm = _norm_dt(photo.get("date_taken")) or ""
        flickr_id = str(photo.get("flickr_id", ""))
        if tier == NO_MATCH:
            rows.append(
                {
                    "tier": tier,
                    "date_norm": date_norm,
                    "flickr_id": flickr_id,
                    "asset_uuid": "",
                    "width": photo.get("width"),
                    "height": photo.get("height"),
                    "flickr_title": photo.get("flickr_title") or "",
                }
            )
            continue
        for c in matches:
            rows.append(
                {
                    "tier": tier,
                    "date_norm": date_norm,
                    "flickr_id": flickr_id,
                    "asset_uuid": c.get("asset_uuid", ""),
                    "width": photo.get("width"),
                    "height": photo.get("height"),
                    "flickr_title": photo.get("flickr_title") or "",
                    "legacy_persons": c.get("persons", "[]"),
                    "legacy_title": c.get("title") or "",
                }
            )
    return rows


def order_rows(rows: list[dict]) -> list[dict]:
    """Deterministic order: tier -> date_norm -> flickr_id -> asset_uuid."""
    return sorted(
        rows,
        key=lambda r: (
            _TIER_ORDER.get(r["tier"], 99),
            r.get("date_norm", ""),
            r.get("flickr_id", ""),
            r.get("asset_uuid", ""),
        ),
    )


def _json_list(value) -> list[str]:
    """Parse a stored JSON-list field (string or already-decoded list)."""
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str) and value:
        try:
            data = json.loads(value)
        except (ValueError, TypeError):
            return []
        if isinstance(data, list):
            return [str(x) for x in data if x]
    return []


def shape_legacy_for_classify(asset: dict) -> dict:
    """Build a classify()-ready record from a legacy_assets row.

    Reconstructs `_UNKNOWN_` sentinels from unknown_face_count so the shared
    classifier counts unknown faces the same way it does for Apple records.
    """
    persons = _json_list(asset.get("persons"))
    unknown = int(asset.get("unknown_face_count") or 0)
    persons = persons + ["_UNKNOWN_"] * unknown
    return {
        "latitude": asset.get("latitude"),
        "longitude": asset.get("longitude"),
        "persons": persons,
        "labels": _json_list(asset.get("labels")),
    }


def is_people_positive(asset: dict) -> bool:
    """True if the legacy asset shows any people signal classify() would act on."""
    if int(asset.get("named_face_count") or 0) > 0:
        return True
    if int(asset.get("unknown_face_count") or 0) > 0:
        return True
    if _json_list(asset.get("persons")):
        return True
    labels = {lbl.lower() for lbl in _json_list(asset.get("labels"))}
    return bool(labels & PEOPLE_LABELS)


# Most-private wins: lower rank = more private.
CLASSIFIER_PRECEDENCE = {"auto_private": 0, "needs_review": 1, "candidate_public": 2}


def format_legacy_reason(tier: str, asset_uuid: str, classifier_reason: str) -> str:
    """Frozen privacy_reason schema (#166). Single source of truth — never build
    this string inline; both this and format_legacy_trigger encode tier+asset and
    must stay in lockstep."""
    return f"legacy-match[tier={tier},asset={asset_uuid}]: {classifier_reason}"


def format_legacy_trigger(asset_uuid: str, tier: str, classifier_version: int) -> str:
    """Frozen operation_log.trigger schema (#166). Single source of truth — the
    orchestrator builds the string here and hands db the finished value, so the
    db layer carries no format literal and the two provenance strings can't drift."""
    return f"legacy:{asset_uuid} tier={tier} clf={classifier_version}"


def legacy_metadata_payload(tier: str, matched_assets: list[dict]) -> dict:
    """Build the legacy-derived staging payload for a matched photo.

    Contract (mirrors classify_match output): confident => exactly one asset;
    ambiguous => two or more; no-match never calls this. The empty-guard is
    defensive only.

    add_tags: propose_tags() per asset, then combined by confidence — we branch
    on tier explicitly rather than relying on cardinality. CONFIDENT yields the
    single asset's tags (tag_sets[0]); AMBIGUOUS (N>=2) yields the INTERSECTION
    (tags shared by every candidate), so an uncertain match can't pull
    event-specific tags from the wrong photo. Branching on tier (not "len==1 so
    intersection happens to work") means a future classifier that returns
    CONFIDENT with >1 asset won't silently switch to intersection semantics.
    Sorted/deduped/lowercased by propose_tags. NOT merged with the photo's
    existing proposed_tags (db does that). title/description only for confident
    matches; None otherwise.
    """
    tag_sets: list[set[str]] = []
    for asset in matched_assets:
        shaped = {
            "keywords": _json_list(asset.get("keywords")),
            "labels": _json_list(asset.get("labels")),
        }
        tag_sets.append(set(propose_tags(shaped)))

    if not tag_sets:
        tags: set[str] = set()
    elif tier == CONFIDENT:
        # Internal invariant (not user-input validation): classify_match
        # guarantees exactly one matched asset under CONFIDENT. Assert it so
        # a future classifier regression is loud immediately. `assert` is the
        # established codebase idiom for internal invariants (legacy_match,
        # db.py, app.py); the project currently does not run under python -O.
        assert len(matched_assets) == 1, (
            f"classify_match contract violation: CONFIDENT must return exactly "
            f"one matched asset, got {len(matched_assets)}"
        )
        tags = tag_sets[0]
    else:
        tags = set.intersection(*tag_sets)

    title = None
    description = None
    if tier == CONFIDENT and matched_assets:
        asset = matched_assets[0]
        title = (asset.get("title") or "").strip() or None
        description = (asset.get("description") or "").strip() or None

    return {"add_tags": sorted(tags), "title": title, "description": description}


def format_legacy_metadata_trigger(
    tier: str, matched_assets: list[dict], classifier_version: int
) -> str:
    """operation_log.trigger for a metadata propagation write. Confident names
    the single source asset; ambiguous records only the candidate count (tags
    are an intersection over N assets — naming one would misattribute).

    Only called with CONFIDENT or AMBIGUOUS tier — NO_MATCH is filtered by
    the caller. An unexpected tier is an internal contract violation; assert
    loudly rather than silently emitting a garbled provenance string.
    """
    if tier == CONFIDENT:
        uuid = str(matched_assets[0].get("asset_uuid", "")) if matched_assets else ""
        return f"legacy-meta:{uuid} tier={tier} clf={classifier_version}"
    assert tier == AMBIGUOUS, (
        f"format_legacy_metadata_trigger called with unexpected tier: {tier!r}"
    )
    return f"legacy-meta:ambiguous tier={tier} n={len(matched_assets)} clf={classifier_version}"


def resolve_apply_decision(
    photo: dict,
    candidates: list[dict],
    zones: list[dict],
    self_name: str = "",
    person_policies: dict[str, str] | None = None,
) -> dict | None:
    """Decide whether/how to reclassify a candidate_public photo from its legacy
    match. Returns {tier, state, asset_uuid, reason} when the photo should be
    demoted, else None (no-match, ambiguous-mixed, or stays candidate_public).
    """
    from analyzer.privacy import classify

    tier, matches = classify_match(photo, candidates)
    if tier == NO_MATCH:
        return None
    if tier == AMBIGUOUS and not all(is_people_positive(c) for c in matches):
        return None

    ordered = sorted(matches, key=lambda c: str(c.get("asset_uuid", "")))
    best: tuple[int, str, str, str] | None = None
    for c in ordered:
        state, reason = classify(
            shape_legacy_for_classify(c),
            zones,
            self_name=self_name,
            person_policies=person_policies or {},
        )
        rank = CLASSIFIER_PRECEDENCE.get(state, 99)
        if best is None or rank < best[0]:
            best = (rank, state, reason, str(c.get("asset_uuid", "")))

    assert best is not None  # matches is non-empty for confident/ambiguous tiers
    _, state, reason, asset_uuid = best
    if state == "candidate_public":
        return None
    return {
        "tier": tier,
        "state": state,
        "asset_uuid": asset_uuid,
        "reason": format_legacy_reason(tier, asset_uuid, reason),
    }
