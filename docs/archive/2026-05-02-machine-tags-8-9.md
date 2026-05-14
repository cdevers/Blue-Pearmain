# flickr-curator: EXIF machine tag backfill idea ([GH #8](https://github.com/cdevers/Blue-Pearmain/issues/8), [GH #9](https://github.com/cdevers/Blue-Pearmain/issues/9))

**Status: Closed as wontfix (May 2026).** Machine tags still exist on Flickr but the ecosystem has effectively collapsed — see decision note below.

---

## Background

Years ago, a Perl script scanned EXIF tags and wrote them to Flickr as **machine tags** — structured `namespace:predicate=value` tags that made photos searchable by camera, lens, focal length, aperture, etc.

Example from [5691165623](https://www.flickr.com/photos/cdevers/5691165623/):

    exif:aperture=f/8.0
    exif:exposure=0.004 sec (1/250)
    exif:exposure_bias=0 EV
    exif:filename=DSC_8051.JPG
    exif:flash=Off, Did not fire
    exif:focal_length=18 mm
    exif:iso_speed=100
    exif:lens=18-200mm f/3.5-5.6
    camera:make=NIKON CORPORATION
    camera:model=NIKON D7000
    exif:orientation=Horizontal (normal)
    exif:shutter_count=9317
    exif:vari_program=Auto(Flash Off)
    meta:exif=1350394705

## Current state (as of April–May 2026)

- Tags still **display** on photo pages (confirmed via screenshot, April 2026)
- Clicking a machine tag (e.g. `flickr.com/photos/tags/camera:model=nikond7000`) goes to a **blank page** — even for a user with many matching photos
- Machine tag search appears **broken on the web UI** as of April 2026
- The `flickr.photos.search` API with `machine_tags=` was not tested, but given the broader ecosystem status (see below), not expected to meaningfully change the decision

## Why we closed this

Machine tags are **not dead** — they still exist on Flickr and existing tags remain stored and visible. But the ecosystem around them has effectively collapsed:

- Flickr no longer actively develops or promotes the feature
- Machine tag search is broken in the web UI (tags function as plain text tags)
- The third-party services that once made machine tags valuable (MusicBrainz integrations, semantic web tooling, etc.) have largely faded
- No meaningful audience is consuming machine tags as structured metadata today

Building a backfill pipeline would be ongoing maintenance cost for zero practical benefit. The existing tags on old photos are harmless to leave in place.

**If Flickr ever meaningfully revives machine tag search**, this design doc provides a solid starting point for re-evaluation.

## Original proposed feature

Add a **machine tag backfill** mode that:

1. Pulls EXIF from local source (Apple Photos metadata, sidecar, or NAS-cached EXIF)
2. Formats tags using the established `namespace:predicate=value` schema
3. Calls `flickr.photos.addTags` to write them back
4. Skips photos that already have machine tags (idempotent)

### Minimum viable tag set

| Tag | Source |
|-----|--------|
| `camera:make` | EXIF Make |
| `camera:model` | EXIF Model |
| `exif:lens` | EXIF LensModel |
| `exif:focal_length` | EXIF FocalLength |
| `exif:aperture` | EXIF FNumber |
| `exif:iso_speed` | EXIF ISO |
| `exif:exposure` | EXIF ExposureTime |
| `exif:flash` | EXIF Flash |

### Notes

- Flickr also exposes EXIF via `flickr.photos.getExif` — could use as source for photos without local files
- Reference project: [FlickrExifTagger](https://github.com/languitar/FlickrExifTagger)
- Tag format should match historical schema exactly so old searches still work if/when Flickr fixes search
