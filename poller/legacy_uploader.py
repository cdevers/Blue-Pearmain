# poller/legacy_uploader.py
"""Upload unmatched legacy assets to Flickr (#230).

Phase 1 (recovery): find assets with uploaded_flickr_id but no photos row
and create the missing photos rows. Repairs partial failures from a prior run.

Phase 2 (upload loop): for each asset in report_unmatched() that has no
uploaded_flickr_id, classify it, upload to Flickr, mark the legacy_assets row,
then write the photos row + operation_log entry atomically.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from legacy_match import shape_legacy_for_classify  # noqa: E402
from legacy_report import report_unmatched  # noqa: E402

log = logging.getLogger("blue-pearmain.legacy-uploader")

_UPLOAD_TRIGGER = "legacy:{asset_uuid} clf={clf}"


def _trigger(asset_uuid: str, classifier_version: int) -> str:
    return _UPLOAD_TRIGGER.format(asset_uuid=asset_uuid, clf=classifier_version)


def _classify_asset(asset: dict, zones: list, self_name: str, person_policies: dict):
    from analyzer.privacy import classify

    shaped = shape_legacy_for_classify(asset)
    return classify(shaped, zones, self_name, person_policies)


def _do_record(
    db,
    flickr_id: str,
    asset: dict,
    privacy_state: str,
    privacy_reason: str,
    classifier_version: int,
) -> None:
    """Write photos row + operation_log atomically. Raises on failure."""
    db.record_legacy_upload(
        flickr_id=flickr_id,
        privacy_state=privacy_state,
        privacy_reason=privacy_reason,
        date_taken=asset.get("date_taken"),
        width=asset.get("width"),
        height=asset.get("height"),
        flickr_title=asset.get("title") or "",
        flickr_tags=asset.get("keywords") or "[]",
        flickr_description=asset.get("description") or "",
        trigger=_trigger(asset["asset_uuid"], classifier_version),
    )


def upload_unmatched_assets(
    db,
    library_uuid: str,
    library_path: Path,
    flickr_client,
    *,
    self_name: str,
    zones: list[dict],
    person_policies: dict[str, str],
    classifier_version: int,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Upload legacy assets with no Flickr counterpart.

    Execution order:
      1. Collect the Phase 2 candidate list via report_unmatched() — this must
         happen BEFORE Phase 1 writes so that assets recovered in Phase 1 are
         still visible in the pre-fetched list and can be counted as
         skipped_already_uploaded rather than silently disappearing.
      2. Run Phase 1 (recovery) over assets returned by iter_unrecovered_legacy_uploads().
      3. Iterate the pre-fetched Phase 2 list. Assets with uploaded_flickr_id
         already set (whether recovered in Phase 1 or via a prior run) are
         counted as skipped_already_uploaded; the remainder go through the
         upload flow.

    eligible counts only Phase 2 candidates that do NOT already have
    uploaded_flickr_id set (i.e. the true upload workload).

    Returns counts dict with keys:
        eligible, uploaded, recovered,
        skipped_already_uploaded, skipped_missing_file,
        auto_private, needs_review, candidate_public,
        date_set_failed, db_write_failed, upload_failed.
    """
    counts: dict[str, int] = {
        "eligible": 0,
        "uploaded": 0,
        "recovered": 0,
        "skipped_already_uploaded": 0,
        "skipped_missing_file": 0,
        "auto_private": 0,
        "needs_review": 0,
        "candidate_public": 0,
        "date_set_failed": 0,
        "db_write_failed": 0,
        "upload_failed": 0,
    }

    # ── Pre-fetch Phase 2 candidates ─────────────────────────────────────────
    # Must happen BEFORE Phase 1 writes so that Phase-1-recovered assets
    # (uploaded_flickr_id set, no photos row) are included in the pre-fetched
    # list and counted as skipped_already_uploaded rather than vanishing after
    # the photos row is written.
    report = report_unmatched(db, library_uuid)
    all_phase2_assets = report["assets"]

    # eligible = assets that are genuine upload candidates (no prior upload)
    fresh_assets = [a for a in all_phase2_assets if not a.get("uploaded_flickr_id")]
    if limit is not None:
        fresh_assets = fresh_assets[:limit]
    counts["eligible"] = len(fresh_assets)

    # ── Phase 1: recover partial failures ────────────────────────────────────
    unrecovered = db.iter_unrecovered_legacy_uploads(library_uuid)
    for asset in unrecovered:
        privacy_state, privacy_reason = _classify_asset(asset, zones, self_name, person_policies)
        flickr_id = asset["uploaded_flickr_id"]
        if not dry_run:
            try:
                _do_record(db, flickr_id, asset, privacy_state, privacy_reason, classifier_version)
                counts["recovered"] += 1
            except Exception as exc:
                log.error(f"Phase 1: failed to create photos row for {flickr_id}: {exc}")
        else:
            counts["recovered"] += 1  # report-only in dry-run

    # ── Phase 2: upload loop ──────────────────────────────────────────────────
    # Iterate ALL pre-fetched assets. Those with uploaded_flickr_id already set
    # (Phase-1-recovered or previously uploaded) are skipped with a counter.
    # Only fresh_assets (no uploaded_flickr_id) are subject to the upload limit.
    uploaded_this_run = 0
    for asset in all_phase2_assets:
        # Idempotency guard: uploaded_flickr_id already set (Phase 1 recovered
        # or prior partial run) — don't upload again.
        if asset.get("uploaded_flickr_id"):
            counts["skipped_already_uploaded"] += 1
            continue

        # Enforce limit on true upload candidates
        if limit is not None and uploaded_this_run >= limit:
            break

        # File must exist
        rel = asset.get("master_rel_path")
        if not rel:
            counts["skipped_missing_file"] += 1
            continue
        file_path = library_path / rel
        if not file_path.exists():
            counts["skipped_missing_file"] += 1
            continue

        # Classify
        privacy_state, privacy_reason = _classify_asset(asset, zones, self_name, person_policies)

        if dry_run:
            counts[privacy_state] += 1
            continue

        # Build tags string from JSON array
        kw = asset.get("keywords") or "[]"
        try:
            tags_list = json.loads(kw) if isinstance(kw, str) else kw
        except (ValueError, TypeError):
            tags_list = []
        tags_str = " ".join(str(t) for t in tags_list)

        # Upload to Flickr
        try:
            flickr_id, date_set_ok = flickr_client.upload_photo(
                file_path,
                title=asset.get("title") or "",
                description=asset.get("description") or "",
                tags=tags_str,
                date_taken=asset.get("date_taken"),
            )
        except Exception as exc:
            log.error(f"Upload failed for {asset['asset_uuid']}: {exc}")
            counts["upload_failed"] += 1
            continue

        # date_set_failed is informational only. A successful upload is always
        # recorded (uploaded_flickr_id set, photos row created) even when setDates
        # fails. Remediation is bp sync-metadata on a later run — not a re-upload.
        if not date_set_ok:
            counts["date_set_failed"] += 1

        # Mark upload in legacy_assets (idempotency guard for re-runs)
        try:
            db.mark_legacy_uploaded(library_uuid, asset["asset_uuid"], flickr_id)
        except Exception as exc:
            log.error(f"ORPHAN: uploaded {flickr_id} but failed to mark in legacy_assets: {exc}")
            print(
                f"ORPHAN UPLOAD — flickr_id={flickr_id} asset={asset['asset_uuid']}",
                flush=True,
            )
            counts["db_write_failed"] += 1
            continue

        # Create photos row + operation_log (atomic)
        try:
            _do_record(db, flickr_id, asset, privacy_state, privacy_reason, classifier_version)
        except Exception as exc:
            log.error(
                f"photos row write failed for {flickr_id}: {exc} "
                "— will be recovered on next run (uploaded_flickr_id is set)"
            )
            counts["db_write_failed"] += 1
            continue

        counts["uploaded"] += 1
        counts[privacy_state] += 1
        uploaded_this_run += 1

    return counts
