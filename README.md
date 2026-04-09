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
| `flickr/flickr_client.py` | Flickr API client |
| `poller/poller.py` | Scheduled sync: Flickr → local DB |
| `poller/scanner.py` | Apple Photos → local DB sync and matching |
| `poller/thumbnailer.py` | Populate thumbnail paths for the review UI |
| `reviewer/app.py` | Flask web UI |
| `reviewer/templates/` | Jinja2 templates |
| `config/` | Configuration templates and launchd plists |
| `tests/` | Unit tests (60 tests) |

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

Run these once to populate the database initially:

```bash
# Pull recent Flickr uploads into the DB
python poller/poller.py --config config/config.yml

# Scan your entire Apple Photos library and cross-reference with Flickr records
python poller/scanner.py --config config/config.yml --all

# Populate thumbnail paths
python poller/thumbnailer.py --config config/config.yml
```

The thumbnailer populates `thumbnail_path` for each photo using the best available source: a locally cached Photos derivative JPEG, a stored Flickr URL, or nothing. When serving thumbnails, the review UI falls back to fetching directly from Flickr's CDN for any matched photo that has a Flickr ID but no local file — so photos that haven't been downloaded from iCloud will still display if they've been uploaded to Flickr. Purely local photos with no Flickr match will show a "no preview" placeholder until iCloud downloads them.

Then start the review UI:

```bash
python reviewer/app.py --config config/config.yml
# Open http://localhost:5173
```

For ongoing use, both the poller and the review UI run as launchd agents — no terminal window required. Create the log directory first, then install both plists:

```bash
mkdir -p ~/Library/Logs/BluePearmain

cp config/com.cdevers.blue-pearmain.poller.plist ~/Library/LaunchAgents/
cp config/com.cdevers.blue-pearmain.reviewer.plist ~/Library/LaunchAgents/

# Edit paths in both plists to match your install location, then:
launchctl load ~/Library/LaunchAgents/com.cdevers.blue-pearmain.poller.plist
launchctl load ~/Library/LaunchAgents/com.cdevers.blue-pearmain.reviewer.plist
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

## Review UI

The grid view shows photos with proposed tags and action buttons. Keyboard shortcuts are available throughout:

| Key | Action |
|---|---|
| `J` / `↓` | Next photo |
| `K` / `↑` | Previous photo |
| `P` | Make public (approve + push to Flickr) |
| `X` | Keep private |
| `Space` | Skip (decide later) |
| `Enter` | Open detail view |
| `Esc` | Return to grid |

In the detail view, `J` / `K` navigate between photos without returning to the grid, and any decision automatically advances to the next photo.

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

## Tests

```bash
python tests/test_core.py
```

41 tests covering the privacy classifier, tagger, database layer, and scanner matching logic.

## License

MIT
