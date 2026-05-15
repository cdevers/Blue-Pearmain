"""
reconcile.py — compare DB state to actual Flickr state

Fetches current permissions and tags from Flickr for photos the DB
believes have been pushed, and reports any mismatches.

Useful for:
  - Verifying that push operations actually took effect
  - Recovering from partial failures
  - Auditing before bulk operations

Usage:
    python poller/reconcile.py --config config/config.yml
    python poller/reconcile.py --config config/config.yml --fix
    python poller/reconcile.py --config config/config.yml --limit 100

Options:
    --fix       Attempt to re-push any mismatches found
    --limit N   Check at most N photos (default: 500)
    --verbose   Show all results, not just mismatches
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database
from flickr.flickr_client import FlickrClient, FlickrError

log = logging.getLogger("blue-pearmain.reconcile")


def setup_logging(verbose: bool) -> None:
    from poller.bp_logging import configure

    configure("reconcile", verbose)


def check_photo(
    client: FlickrClient,
    row: dict,
    fix: bool,
    verbose: bool,
) -> dict:
    """
    Check a single photo against Flickr. Returns a result dict with fields:
        flickr_id, status, details
    where status is one of: ok | perm_mismatch | tag_mismatch | both_mismatch | flickr_error
    """
    flickr_id = row["flickr_id"]
    db_state = row["privacy_state"]
    db_perms_pushed = row["perms_pushed_flickr"]
    db_tags_pushed = row["tags_pushed_flickr"]
    db_tags = row["proposed_tags"] or []
    if isinstance(db_tags, str):
        try:
            db_tags = json.loads(db_tags)
        except (json.JSONDecodeError, TypeError, ValueError):
            db_tags = []

    result = {
        "flickr_id": flickr_id,
        "status": "ok",
        "row_id": row["id"],
        "perm_expected": None,
        "perm_actual": None,
        "tags_expected": [],
        "tags_missing": [],
        "fixes": [],
        "errors": [],
    }

    try:
        info = client.get_photo_info(flickr_id)
    except FlickrError as e:
        result["status"] = "flickr_error"
        result["errors"] = [str(e)]
        return result

    photo = info.get("photo", {})

    # --- Permission check ---
    if db_perms_pushed:
        visibility = photo.get("visibility", {})
        flickr_pub = int(visibility.get("ispublic", 0))
        expected_pub = 1 if db_state in ("approved_public", "already_public") else 0

        result["perm_expected"] = "public" if expected_pub else "private"
        result["perm_actual"] = "public" if flickr_pub else "private"

        if flickr_pub != expected_pub:
            result["status"] = "perm_mismatch"
            if fix:
                try:
                    client.set_permissions(flickr_id, is_public=expected_pub)
                    result["fixes"].append("perm")
                except FlickrError as e:
                    result["errors"].append(f"perm fix failed: {e}")

    # --- Tag check ---
    if db_tags_pushed and db_tags:
        tags_container = photo.get("tags", {})
        flickr_tags = set()
        if isinstance(tags_container, dict):
            for t in tags_container.get("tag", []):
                flickr_tags.add(t.get("raw", "").lower().strip())

        expected_tags = set(t.lower().strip() for t in db_tags if t.strip())
        missing = sorted(expected_tags - flickr_tags)

        result["tags_expected"] = sorted(expected_tags)
        result["tags_missing"] = missing

        if missing:
            result["status"] = (
                "both_mismatch" if result["status"] == "perm_mismatch" else "tag_mismatch"
            )
            if fix:
                try:
                    client.add_tags(flickr_id, missing)
                    result["fixes"].append("tags")
                except FlickrError as e:
                    result["errors"].append(f"tag fix failed: {e}")

    if verbose and result["status"] == "ok":
        log.debug(f"{flickr_id}: ok")

    return result


def format_result_line(result: dict, url: str, ts: str) -> str:
    """Format one reconcile result as a single log line.

    Column order: <ts> [<status>] <url> <corrective-action> <diagnostics>
    Corrective actions (fixed:, errors:) come before diagnostics (perm:,
    missing:) so the left-hand columns stay stable and scannable.
    """
    status = result["status"]
    if status == "ok":
        return f"{ts} [ok] {url}"
    if status == "flickr_error":
        return f"{ts} [ERR] {url}"

    parts = []
    if result.get("fixes"):
        parts.append(f"fixed:{','.join(result['fixes'])}")
    if result.get("errors"):
        parts.append(f"errors:{len(result['errors'])}")
    if result.get("perm_expected") and result["perm_expected"] != result.get("perm_actual"):
        parts.append(f"perm:{result['perm_expected']}→{result['perm_actual']}")
    if result.get("tags_missing"):
        missing_str = ", ".join(result["tags_missing"][:8])
        extra = f" +{len(result['tags_missing']) - 8}" if len(result["tags_missing"]) > 8 else ""
        parts.append(f"missing:{missing_str}{extra}")

    detail = (" " + " ".join(parts)) if parts else ""
    return f"{ts} [{status}] {url}{detail}"


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain reconciliation")
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--fix", action="store_true", help="Re-push mismatches")
    parser.add_argument(
        "--apply-proposals",
        action="store_true",
        help="Apply pending non-conflict proposals to Photos/Flickr",
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = Path(config["database"]["path"]).expanduser()
    db = Database(db_path)

    # --apply-proposals: apply pending non-conflict tag proposals and exit
    if args.apply_proposals:
        from flickr.proposal_applier import apply_batch

        library_path = str(Path(config.get("photos_library", {}).get("path", "")).expanduser())
        try:
            client = FlickrClient.from_config(config)
        except Exception:
            client = None
        totals = apply_batch(db, library_path, flickr_client=client, limit=args.limit)
        print(
            f"applied={totals['applied']}  superseded={totals['superseded']}  failed={totals['failed']}"
        )
        db.close()
        return 1 if totals["failed"] else 0

    log.info("Blue Pearmain reconciliation starting")

    try:
        client = FlickrClient.from_config(config)
        client.test_login()
        log.info("Flickr auth OK")
    except Exception as e:
        log.error(f"Flickr auth failed: {e}")
        sys.exit(1)

    # Fetch photos where we believe we've pushed something to Flickr
    rows = db.conn.execute(
        """SELECT id, flickr_id, privacy_state, proposed_tags,
                  perms_pushed_flickr, tags_pushed_flickr
           FROM photos
           WHERE flickr_id IS NOT NULL
             AND (perms_pushed_flickr = 1 OR tags_pushed_flickr = 1)
           ORDER BY reviewed_at DESC
           LIMIT ?""",
        (args.limit,),
    ).fetchall()

    total = len(rows)
    ok_count = 0
    mismatch_count = 0
    error_count = 0
    fix_ok_count = 0
    fix_fail_count = 0

    log.info(f"Checking {total} photos against Flickr...")

    flickr_username = config.get("flickr", {}).get("username") or config.get("flickr", {}).get(
        "user_nsid", ""
    )

    try:
        for row in rows:
            result = check_photo(client, dict(row), fix=args.fix, verbose=args.verbose)
            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            fid = result["flickr_id"]
            url = f"https://www.flickr.com/photos/{flickr_username}/{fid}"

            if result["status"] == "ok":
                ok_count += 1
                if args.verbose:
                    print(format_result_line(result, url, ts))

            elif result["status"] == "flickr_error":
                error_count += 1
                print(format_result_line(result, url, ts))
                for msg in result["errors"]:
                    print(f"      error: {msg}")

            else:
                mismatch_count += 1
                fix_ok_count += len(result["fixes"])
                fix_fail_count += len(result["errors"])
                print(format_result_line(result, url, ts))
                for msg in result["errors"]:
                    print(f"      error: {msg}")

    except Exception as e:
        log.error(f"Reconcile interrupted: {e}")
        error_count += 1

    print()
    # Structured summary always emitted — machine-readable and human-readable
    print(
        f"  checked={total}"
        f"  ok={ok_count}"
        f"  mismatched={mismatch_count}"
        + (f"  fixed={fix_ok_count}  fix-failed={fix_fail_count}" if args.fix else "")
        + f"  api-errors={error_count}"
    )

    if mismatch_count > 0 and not args.fix:
        print("  → Run with --fix to attempt automatic repair.")

    db.close()
    # Exit code differentiation:
    #   0 = clean (no mismatches, no errors)
    #   1 = mismatches found (without --fix), or unfixed mismatches remain (with --fix)
    #   2 = operational errors (API failures, fix failures)
    # This lets callers distinguish "needs attention" from "broken".
    if error_count or fix_fail_count:
        return 2
    if mismatch_count and not args.fix:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
