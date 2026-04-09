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
           SQLite database + thumbnail cache (NAS)
                       ↓
           Review UI  →  Flickr API (setPerms, addTags)
```

## Components

| Path | Purpose |
|---|---|
| `db/` | SQLite schema and database access layer |
| `analyzer/` | Privacy classification and tag proposal logic |
| `flickr/` | Flickr OAuth setup and API client |
| `poller/` | Scheduled sync: Flickr → local DB |
| `reviewer/` | Flask web UI for the review queue |
| `config/` | Configuration templates |

## Requirements

- macOS (for Apple Photos integration via [osxphotos](https://github.com/RhetTbull/osxphotos))
- Python 3.11+
- A Flickr Pro account and API key
- A NAS or other always-on storage for the database and thumbnail cache (optional but recommended)

## Setup

```bash
# 1. Clone and install dependencies
git clone https://github.com/cdevers/Blue-Pearmain.git
cd Blue-Pearmain
uv sync

# 2. Configure
cp config/config.example.yml config/config.yml
# Edit config/config.yml — add your Flickr API key and secret

# 3. Authorise with Flickr (one-time)
python flickr/flickr_auth.py --config config/config.yml

# 4. Run the poller (coming soon)
# python poller/poller.py --config config/config.yml

# 5. Run the review UI (coming soon)
# python reviewer/app.py --config config/config.yml
```

## Status

Early development. The database schema, privacy classifier, tag proposer, and Flickr API client are implemented. The poller and review UI are in progress.

## License

MIT
