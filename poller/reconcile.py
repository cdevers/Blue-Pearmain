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
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import Database
from flickr.flickr_client import FlickrClient, FlickrError

log = logging.getLogger("blue-pearmain.reconcile")


def setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


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
    flickr_id      = row["flickr_id"]
    db_state       = row["privacy_state"]
    db_perms_pushed = row["perms_pushed_flickr"]
    db_tags_pushed  = row["tags_pushed_flickr"]
    db_tags         = row["proposed_tags"] or []
    if isinstance(db_tags, str):
        try:    db_tags = json.loads(db_tags)
        except: db_tags = []

    result = {
        "flickr_id":    flickr_id,
        "status":       "ok",
        "row_id":       row["id"],
        "perm_expected": None,
        "perm_actual":   None,
        "tags_expected": [],
        "tags_missing":  [],
        "fixes":         [],
        "errors":        [],
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
        visibility   = photo.get("visibility", {})
        flickr_pub   = int(visibility.get("ispublic", 0))
        expected_pub = 1 if db_state in ("approved_public", "already_public") else 0

        result["perm_expected"] = "public" if expected_pub else "private"
        result["perm_actual"]   = "public" if flickr_pub  else "private"

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
        missing       = sorted(expected_tags - flickr_tags)

        result["tags_expected"] = sorted(expected_tags)
        result["tags_missing"]  = missing

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


def main():
    parser = argparse.ArgumentParser(description="Blue Pearmain reconciliation")
    parser.add_argument("--config",  default="config/config.yml")
    parser.add_argument("--fix",     action="store_true", help="Re-push mismatches")
    parser.add_argument("--limit",   type=int, default=500)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    log.info("Blue Pearmain reconciliation starting")

    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_path = Path(config["database"]["path"]).expanduser()
    db = Database(db_path)

    try:
        client = FlickrClient.from_config(config)
        client.test_login()
        log.info(f"Flickr auth OK")
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
        (args.limit,)
    ).fetchall()

    total    = len(rows)
    ok_count = mismatch_count = error_count = 0

    log.info(f"Checking {total} photos against Flickr...")

    for row in rows:
        result = check_photo(client, dict(row), fix=args.fix, verbose=args.verbose)

        if result["status"] == "ok":
            ok_count += 1
            if args.verbose:
                print(f"  ok  {result['flickr_id']}")

        elif result["status"] == "flickr_error":
            error_count += 1
            print(f"  ERR {result['flickr_id']}")
            for e in result["errors"]:
                print(f"        error:    {e}")

        else:
            mismatch_count += 1
            fid = result["flickr_id"]
            url = f"https://www.flickr.com/photos/cdevers/{fid}"
            print(f"  [{result['status']}]  {fid}  {url}")

            if result["perm_expected"] and result["perm_expected"] != result["perm_actual"]:
                print(f"        perm:     expected={result['perm_expected']}"
                      f"  actual={result['perm_actual']}")

            if result["tags_missing"]:
                missing_str = ", ".join(result["tags_missing"][:8])
                extra = f" (+{len(result['tags_missing'])-8} more)" if len(result["tags_missing"]) > 8 else ""
                print(f"        tags:     missing from Flickr: {missing_str}{extra}")

            if result["fixes"]:
                print(f"        fixed:    {', '.join(result['fixes'])}")

            if result["errors"]:
                for e in result["errors"]:
                    print(f"        error:    {e}")

    print()
    log.info(
        f"Done: {ok_count} ok, {mismatch_count} mismatches, {error_count} errors "
        f"(out of {total} checked)"
    )

    if mismatch_count > 0 and not args.fix:
        log.info("Run with --fix to attempt automatic repair.")

    db.close()
    return mismatch_count + error_count  # non-zero exit if problems found


if __name__ == "__main__":
    sys.exit(main() or 0)
