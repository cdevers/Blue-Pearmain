# Blue Pearmain

A privacy-aware tool for reviewing, tagging, and managing a large Flickr photo library, with integration into Apple Photos.

Named for the [Blue Pearmain apple](https://en.wikipedia.org/wiki/Blue_Pearmain), an American variety mentioned by Henry David Thoreau in his 1862 essay *Wild Apples*.

---

## The problem

Flickr's iOS app syncs your entire camera roll automatically — which is convenient for backup, but means a large backlog of private photos accumulates over time. Manually reviewing tens of thousands of photos to decide what to make public, what to tag, and what to keep private is impractical.

Blue Pearmain automates the triage:

- Polls Flickr for newly uploaded photos
- Cross-references them against your Apple Photos library to harvest Apple's existing AI analysis (scene labels, face detection, captions, GPS)
- Applies geofence rules to automatically keep photos from home and other private locations out of the review queue
- Flags photos containing unidentified people for manual review
- Proposes tags derived from Apple's ML labels and location data
- Serves a local web UI for working through the review queue — approving tags, setting photos public, or keeping them private

Nothing is pushed to Flickr without explicit human confirmation.

## Architecture

```
Apple Photos Library          Flickr (cloud)
      ↓  osxphotos                  ↓  API poll (hourly)
      └──────────── Matcher ────────┘
                       ↓
           Geofence filter  →  auto-private
                       ↓
           Face / people detection
                       ↓
           Tag proposals (from Apple ML labels + location)
                       ↓
           SQLite database + thumbnail cache
                       ↓
           Review UI  →  Flickr API (setPerms, addTags)
```

## Components

| Path | Purpose |
|---|---|
| `db/schema.sql` | SQLite schema |
| `db/db.py` | Database access layer |
| `analyzer/privacy.py` | Privacy classification logic |
| `analyzer/tagger.py` | Tag proposal logic |
| `flickr/flickr_auth.py` | One-time Flickr OAuth setup |
| `flickr/flickr_client.py` | Flickr API client (with retry/backoff) |
| `poller/poller.py` | Scheduled sync: Flickr → local DB |
| `poller/scanner.py` | Apple Photos → local DB sync and matching |
| `poller/thumbnailer.py` | Populate thumbnail paths for the review UI |
| `poller/reconcile.py` | Compare DB push state against actual Flickr state |
| `reviewer/app.py` | Flask web UI |
| `reviewer/templates/` | Jinja2 templates (dashboard, review grid, photo detail, faces, zones) |
| `config/` | Configuration templates and launchd plists |
| `db/migrate_001_privacy_state_check.py` | DB migration: adds CHECK constraint on privacy_state |
| `db/migrate_002_updated_at_and_indexes.py` | DB migration: adds updated_at, indexes on push state and tags, schema_migrations table |
| `bp` | Unified command-line entry point |
| `tests/` | Unit tests (88 tests) |

## Requirements

- macOS (Apple Photos integration via [osxphotos](https://github.com/RhetTbull/osxphotos))
- Python 3.11+
- A Flickr Pro account and API key (register at [flickr.com/services/apps](https://www.flickr.com/services/apps/create/))

## Setup

```bash
# 1. Clone and install dependencies
git clone https://github.com/cdevers/Blue-Pearmain.git
cd Blue-Pearmain
pip3 install requests requests-oauthlib pyyaml flask osxphotos

# 2. Configure
cp config/config.example.yml config/config.yml
# Edit config/config.yml — add your Flickr API key and secret

# 3. Authorise with Flickr (one-time, opens browser)
python flickr/flickr_auth.py --config config/config.yml
```

## Running

The `bp` script at the repo root is the unified entry point for all commands:

```bash
chmod +x bp   # once, after cloning

bp stats                           # Photo counts by privacy state (includes approved+pushed count)
bp stats --oneliner                # Single-line summary for watch loops (includes pushed=N)
bp poll                            # Pull recent Flickr uploads (incremental)
bp poll --backfill --days 365      # Backfill a year of Flickr history
bp poll --backfill --days 100000   # Full historical backfill
bp scan --all                      # Scan entire Apple Photos library
bp scan                            # Scan recent Photos additions (last 7 days)
bp thumbs                          # Populate missing thumbnail paths
bp reconcile                       # Check DB vs actual Flickr state
bp reconcile --fix                 # Check and repair mismatches
bp ui                              # Start the review UI (http://localhost:5173)
```

All commands accept `--config PATH` (default: `config/config.yml`) and `--verbose`.

Initial population sequence:

```bash
bp poll --backfill --days 100000  # Pull full Flickr history into DB
bp scan --all                     # Cross-reference Apple Photos library
bp thumbs                         # Cache thumbnail paths
bp ui                             # Open http://localhost:5173 and start reviewing
```

The thumbnailer populates `thumbnail_path` for each photo using the best available source: a locally cached Photos derivative JPEG, a stored Flickr URL, or nothing. When serving thumbnails, the review UI falls back to fetching directly from Flickr's CDN for any matched photo that has a Flickr ID but no local file — so photos that haven't been downloaded from iCloud will still display if they've been uploaded to Flickr. Purely local photos with no Flickr match will show a "no preview" placeholder until iCloud downloads them.

For ongoing use, both the poller and the review UI run as launchd agents — no terminal window required. Create the log directory first, then install both plists:

```bash
mkdir -p ~/Library/Logs/BluePearmain

cp config/com.cdevers.blue-pearmain.poller.plist ~/Library/LaunchAgents/
cp config/com.cdevers.blue-pearmain.reviewer.plist ~/Library/LaunchAgents/

# Edit paths in both plists to match your install location, then:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cdevers.blue-pearmain.poller.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cdevers.blue-pearmain.reviewer.plist
```

The reviewer starts immediately and restarts automatically if it crashes. The poller runs hourly. Logs are written to `~/Library/Logs/BluePearmain/` and are visible in Console.app:

```bash
tail -f ~/Library/Logs/BluePearmain/reviewer.log
tail -f ~/Library/Logs/BluePearmain/poller.log
```

To restart either service:

```bash
launchctl stop com.cdevers.blue-pearmain.reviewer
launchctl start com.cdevers.blue-pearmain.reviewer
```

If you get "Input/output error" from launchctl (stale state after an unclean stop), use `bootout`/`bootstrap` instead of `unload`/`load`:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.cdevers.blue-pearmain.reviewer.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cdevers.blue-pearmain.reviewer.plist
```

## Review UI

The grid view shows photos with proposed tags and action buttons. Keyboard shortcuts are available throughout:

**Grid view:**

| Key | Action |
|---|---|
| `J` / `↓` | Next photo |
| `K` / `↑` | Previous photo |
| `P` | Make public + push tags to Flickr |
| `X` | Keep private + push tags to Flickr |
| `Space` | Skip (decide later) |
| `Enter` | Open detail view |

**Detail view:**

| Key | Action |
|---|---|
| `P` | Make public + push to Flickr |
| `A` | Approve (don't push yet) |
| `X` | Keep private + push tags |
| `Space` | Skip |
| `T` | Focus tag editor |
| `N` | Go to Faces page for this person |
| `J` / `→` | Next photo |
| `K` / `←` | Previous photo |
| `Esc` | Return to grid |

In the detail view the action buttons are pinned to the top of the sidebar, so their position stays consistent regardless of how much metadata or how many tags a photo has. Any decision automatically advances to the next photo.

Both public and private decisions push tags to Flickr — tags are useful for search even on private photos.

## Faces

The Faces page (`/faces`) lists every named person in your Apple Photos library, sorted by photo count. For each person you can:

- **Review** — open a filtered review queue showing only photos containing that person
- **All private** — batch-mark every photo of that person as private (tags still pushed to Flickr)
- **All public** — batch-mark every photo as approved public

A confirmation dialog prevents accidental bulk actions. Unknown/unidentified faces appear as a separate count at the bottom with no batch actions.

In the photo detail view, each named person is a link to the filtered review queue for that person — so you can immediately start browsing all photos containing them. The `N` keyboard shortcut goes to the Faces directory page, anchored to that person's row, where the batch actions and photo counts are available.

When browsing a person-filtered review queue, prev/next navigation (J/K, ‹ ›) stays scoped to that person's photos throughout — including after opening a photo in detail view.

## Privacy classification

Photos are automatically classified into one of these states:

| State | Meaning |
|---|---|
| `auto_private` | Home location, screenshot, or geofenced zone — never enters review queue |
| `needs_review` | People detected — human must decide |
| `candidate_public` | No people signals — tags proposed, ready for quick confirmation |
| `approved_public` | Human approved, queued for Flickr push |
| `already_public` | Was already public on Flickr before this tool existed |
| `keep_private` | Human said no |
| `skipped` | Deferred |

## Geofence zones

Add private locations (home, school, etc.) via the Zones page in the UI. Each zone has a centre point, radius in metres, and a policy (`auto_private`, `flag_review`, or `auto_public`). Apple Photos' own home flag is also used automatically.

## Workflow note: Photos-only approvals

When you approve a photo that exists in Apple Photos but hasn't yet been uploaded to Flickr, Blue Pearmain marks it `approved_public` locally but cannot push anything to Flickr yet — there is no Flickr ID to push to.

Once the Flickr iOS app eventually uploads those photos, the poller will detect the new upload, match it against the existing `approved_public` record by capture timestamp, and automatically push the permissions and tags to Flickr without requiring further action. The approved decision you made earlier is honoured as soon as the photo arrives on Flickr.

Photos that are already on Flickr when you approve them are pushed immediately.

## Reliability

The Flickr API client uses exponential backoff with jitter on transient failures (HTTP 429/5xx, timeouts, connection errors), retrying up to 4 times with delays of approximately 1, 2, 4, and 8 seconds. HTTP 4xx errors (400, 401, 403, 404, etc.) are treated as permanent and raise immediately without retrying. Flickr application-level errors are classified as transient (codes 0, 106) or permanent, and handled accordingly. Every API call has a 30-second timeout.

Write operations (permissions and tags) only update the DB push flags after each operation succeeds individually — a failed tag push does not mark tags as pushed, and a failed permission change does not mark the photo as public. Both failures are logged with the Flickr photo ID for traceability.

If you suspect a push operation failed silently, the reconciliation script checks your DB's expected state against what Flickr actually has:

```bash
bp reconcile          # Report mismatches (exit 1 if mismatches, 2 if API errors)
bp reconcile --fix    # Repair mismatches (exit 2 if any fix fails, 0 if all resolved)
```

The structured summary output distinguishes checked, mismatched, fixed, and failed counts. Exit codes are differentiated: 0 = clean, 1 = mismatches found (without `--fix`), 2 = operational errors (API failures or fix failures). `bp poll` also exits non-zero if any auto-push Flickr write fails.

**Config validation** — both the poller and reviewer validate required config fields at startup and exit immediately with a readable error message if anything is missing, rather than failing deep in a request.

**Database integrity** — `privacy_state` has a SQL `CHECK` constraint enforcing valid values. Applied migrations are tracked in the `schema_migrations` table. If you are upgrading an existing installation, run both migrations in order:

```bash
python db/migrate_001_privacy_state_check.py --config config/config.yml
python db/migrate_002_updated_at_and_indexes.py --config config/config.yml
```

Both scripts are idempotent and safe to re-run.

> **Note (migration 001):** If any photos have an unrecognised `privacy_state` value (e.g. from manual DB edits or a future code change), the migration will reset them to `needs_review` before adding the constraint. Check the output for any rows that are reset.

## Tests

```bash
python tests/test_core.py
```

110 tests covering the privacy classifier, tagger, database layer, scanner matching, Flickr client retry/jitter/4xx/429 handling, batch person actions, schema migrations, reconcile exit codes and precedence, and the `bp` CLI entry point.

## License

MIT
