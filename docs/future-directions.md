# Blue Pearmain — Future Directions

> Written after shipping v1.0.0 (2026-05-20). This document records the direction of thinking at that point in time — not a committed roadmap, but a prioritised set of ideas worth returning to. Each item is tracked as a GitHub issue; the issue is the canonical home for design notes and status. This document is the narrative overview.

---

## What Blue Pearmain is — and should remain

BP is a **personal archival tool**: deterministic, operator-controlled, and local-first. It polls rather than syncing in real time. It proposes rather than acting unilaterally. It queues conflicts for human judgment rather than guessing. It trusts the operator's decisions permanently once made.

Future work should deepen those qualities, not compromise them. The ideas below were selected because they make BP *more* trustworthy and durable — not because they make it more powerful in a generic sense.

---

## Priority 1 — Operational health

*Making it easier to know that BP is doing its job, without watching log files.*

BP runs as three always-on launchd daemons. Right now, if something goes wrong — an auth token expires, the pipeline stalls, reconcile finds drift — the signal is buried in a log file. There is no at-a-glance view of system health.

### bp status — operational health dashboard ([#112](https://github.com/cdevers/Blue-Pearmain/issues/112)) `size:M` · ✓ done

A new `bp status` command that prints a structured summary of daemon state, queue sizes, last-run times, and unresolved drift. Reads only from the local DB and `launchctl` — no network calls. Intended to answer "is everything OK?" in one command.

### macOS notifications for daemon errors ([#113](https://github.com/cdevers/Blue-Pearmain/issues/113)) `size:S` · ✓ done

Surface high-signal failures (auth expiry, sustained API errors, unresolved reconcile drift) as macOS system notifications rather than log-only events. Optional via config flag; fire-and-forget so it doesn't block daemon operation.

---

## Priority 2 — Policies

*Letting the operator declare intent once, rather than repeating manual decisions.*

BP's current model is reactive: classify what arrives, queue what needs review, apply what's unambiguous. Two gaps have emerged where a persistent *policy* would be more appropriate than a repeated manual action.

### Legacy-match demotion policy — any confident match → needs_review

Currently `match-legacy --apply` only demotes a Flickr-only `candidate_public` photo when the matched iPhoto asset carries a privacy signal: a named face, an unknown face, or a location inside a geofence zone. Photos that match confidently but carry no such signal stay `candidate_public`.

In practice the Flickr-only `candidate_public` queue (~19k photos) contains many personal family photos whose iPhoto counterparts happen to be untagged (iPhoto face recognition was manual and incomplete). A policy flag — `legacy_match.demote_all_confident: true` in `config.yml` — would say "any confident legacy match is sufficient reason to move the photo to `needs_review`, regardless of content signals." This is a deliberate trade-off: more false positives in the review queue, fewer inadvertent public posts. Worth offering as opt-in.

### Per-person privacy policy ([#114](https://github.com/cdevers/Blue-Pearmain/issues/114)) `size:M` · ✓ done

A way to declare "any photo containing Person X is always auto-private" — stored in the DB, checked at scan time. Currently, batch-marking all of a person's photos private is a one-shot action; new photos of that person re-enter the queue. A persistent policy is the right primitive for people who should never appear on Flickr (children, people who've asked not to be photographed publicly).

### Tag protection rules ([#115](https://github.com/cdevers/Blue-Pearmain/issues/115)) `size:S` · ✓ done

A config-driven way to declare tag namespaces or specific tags as protected from auto-removal. The sync engine currently treats a tag's absence from one side as a non-conflict eligible for auto-correction. For archival tags (`family/*`, `scanned-film`, `archive/*`) that may not exist in Apple Photos' keyword set, this can produce harmful removals. A lightweight `tag_protection:` section in `config.yml` prevents this without a full policy engine.

---

## Priority 3 — Auditability

*Making BP's decisions explainable — not just now, but years from now.*

BP records state, not causation. You can see that a photo is `auto_private`, but not whether it was geofenced, flagged by a person policy, or manually decided. You can see that a tag is on Flickr, but not when it was added or what triggered it. For a tool designed around archival stewardship, this is a meaningful gap.

### Operation journal ([#116](https://github.com/cdevers/Blue-Pearmain/issues/116)) `size:L` · ✓ done

An append-only `operation_log` table in the DB that records every mutation BP makes — to Flickr, to Apple Photos, to DB state — along with the reason and trigger. Covers proposal auto-apply, manual review decisions, reconcile --fix writes, tag-writeback, album pushes, and privacy state changes. Makes BP behave more like an archival system of record than a sync utility.

### `bp reconcile --explain` ([#117](https://github.com/cdevers/Blue-Pearmain/issues/117)) `size:S` · ✓ done

A richer dry-run mode that shows, for each proposed reconcile change: current Flickr state, desired state, source of truth, and the reason for the discrepancy. The current `--dry-run` tells you *what* would change; `--explain` tells you *why*. Especially valuable years after deployment when the original context has been forgotten. If the operation journal (#116) is implemented first, `--explain` can reference journal entries to show when a state was last changed and by what.

---

## Priority 4 — Portability

*Ensuring BP's metadata survives beyond any particular tool or service.*

BP's SQLite database is the authoritative record of everything it knows about your photo archive. But it is a binary format tied to BP's schema. If Flickr changes its API, goes dark, or needs to be migrated away from, acting on that data requires reverse-engineering the DB. A periodic export to portable formats gives durable access independent of any tool or service.

### Export mode ([#118](https://github.com/cdevers/Blue-Pearmain/issues/118)) `size:M` · ✓ done

A `bp export` command that serialises the full BP state — per-photo metadata, review decisions, sync state, geofence zones — to JSON or YAML. One file (or directory of files) that can be read without BP, understood without documentation, and imported into a future tool. Not a backup of the DB; a human-readable record of the archive's metadata.

---

## Priority 5 — Duplicate detection maintenance

*Keeping the duplicate review queue accurate without manual intervention.*

The deduplicator is currently a manually-invoked script. As the scanner adds new Photos records over time, they can silently accumulate as "orphaned" photos — sharing a key with an existing duplicate group but not linked to it. A periodic deduplicator run (weekly, or after each scan cycle) would self-heal this automatically.

### Deduplicator in poller cycle ([#147](https://github.com/cdevers/Blue-Pearmain/issues/147)) `size:S` · ✓ done

Run `deduplicator --write` as part of the poller's regular cycle (or as a separate weekly launchd job). This ensures orphaned siblings are linked to their groups promptly, and stale `photo_count` values stay accurate. The `--prune` pass could also be folded in so zombie groups are cleaned up automatically.

---

## Discovery and memory

*Features that make BP a richer tool for exploring and revisiting the archive — not just managing it.*

These ideas came out of studying [Iris Photos](https://irisphotos.app/) (launched 2026-05-27), which is doing similar work in the local-first photo library space. They are listed roughly in order of how concrete and near-term they feel.

### Photo Trails — trip retracing on the map ([#151](https://github.com/cdevers/Blue-Pearmain/issues/151)) `size:M` · [spec](superpowers/specs/2026-05-27-photo-trails-151.md) · ✓ done

The map currently shows all geotagged photos as independent dots. A "Photo Trails" mode would connect photos chronologically — within a single day, or within a user-selected date range — drawing a path across the map that shows where you went and in what order. Clicking a segment would show the photos taken along it. This is a natural extension of the existing Leaflet map and the temporal filter already present. Exploratory value is high: it's one thing to see *where* you took photos; it's another to see the *journey*.

### Person birthdays and birthday-aware filtering ([#152](https://github.com/cdevers/Blue-Pearmain/issues/152)) `size:M` · [spec](superpowers/specs/2026-05-27-person-birthdays-152.md) · ✓ done

### Map filter scoping — year range, album, person, privacy ([#154](https://github.com/cdevers/Blue-Pearmain/issues/154)) `size:M` · [spec](superpowers/specs/2026-05-28-map-filter-scoping-154.md) · ✓ done

The map filter bar gains four new dimensions that AND with the existing time-pattern dropdown: year range (from/to, either optional), album, person (type-ahead against `apple_persons`), and an animation-only privacy toggle (All / Public only / Private only). All filters affect map dots, trail, and animation; privacy affects animation only. Enables workflows like "every place I've met Marcin" or "find which August trip included Spain, then animate it."

### Unified filter widget — shared macro across library and map ([#155](https://github.com/cdevers/Blue-Pearmain/issues/155)) `size:M` · [spec](superpowers/specs/2026-05-28-unified-filter-widget-155.md) · ✓ done

Extracted the five shared filter dimensions (time pattern, year range, album, person, privacy/status) into a reusable Jinja macro (`_filter_bar.html`) used by both `/library` and `/map`. Library gains instant-apply JS (no Apply button), a filter chip row, and a View-on-map link that preserves filter state. Map gets a compact bar + collapsible panel with deep-link support. `normalize_shared_filters()` is the single normalization entry point for both routes.

### Unified filter widget: tags dimension ([#156](https://github.com/cdevers/Blue-Pearmain/issues/156)) `size:S` · ✓ done

Add a tag type-ahead to the shared `_filter_bar.html` macro. Tags are already in the DB; this wires them as a sixth filter dimension on both `/library` and `/map`, with the same chip-row, deep-link, and cross-page nav treatment as the other dimensions. Single-tag filter to start; multi-tag OR can follow separately.

### Animated map trail — Indiana Jones-style route animation ([#153](https://github.com/cdevers/Blue-Pearmain/issues/153)) `size:L` · [spec](superpowers/specs/2026-05-27-map-trail-animation-153.md) · ✓ done

Animate the photo trail so the route draws itself: a moving point traces the journey and leaves a growing line behind it. BP already has the data (#151 computes the ordered sequence); the question is rendering.

Three phases: (1) in-browser Leaflet animation as proof-of-concept — an Animate button progressively draws the polyline with a moving icon, user screen-records; (2) headless Playwright + ffmpeg for automated MP4 export; (3) pure-Python raster rendering with OSM tiles + Pillow for full cinematic control.

**Privacy**: the trail is just lat/lon — no faces — but thumbnail overlays must respect the privacy model. A public version filters to `approved_public`/`already_public` photos; a family version includes private photos for local/restricted sharing. Album membership is a natural scope boundary ("animate this album's photos").

Storing a known birthday for named people (in a `people` table or similar) enables several useful features:
- Display age-at-time in the photo detail view ("Chris, age 8")
- Filter the library or map by "photos taken on a person's birthday"
- Filter by "photos where an identified person appears" for any named person
- Eventually: "photos taken within a week of someone's birthday" for fuzzy milestone browsing

Not immediate — needs a people schema that BP doesn't have yet — but a coherent direction worth designing for.

### Unified filter widget: date range ([#159](https://github.com/cdevers/Blue-Pearmain/issues/159)) `size:S` · ✓ done

Replace the integer year-range inputs with native `<input type="date">` pickers (`date_from` / `date_to`) across the shared filter bar, library, and map. Either field is optional (open-ended ranges). `normalize_shared_filters()` handles validation, the `date_from > date_to` swap, and backward compat for legacy `year_from`/`year_to` URL params. The library and map SQL use day-level `>=` / `<` boundaries; `date_to` is inclusive on the user side, translated to an exclusive next-day bound in SQL.

### Approximate / fuzzy dates for historical photos ([#157](https://github.com/cdevers/Blue-Pearmain/issues/157)) `size:M`

Pre-digital or scanned photos often have only a year, or a decade, or "sometime in the 1970s." The DB currently treats `date_taken` as either a precise timestamp or NULL. A `date_precision` field (`exact`, `day`, `month`, `year`, `decade`, `unknown`) alongside a `date_approximate` flag would let BP represent and surface these photos without either lying about their date or discarding the approximate information entirely. Useful for iPhoto migrations and scanned film.

### Command palette (⌘K) for the reviewer UI ([#158](https://github.com/cdevers/Blue-Pearmain/issues/158)) `size:M`

A keyboard-driven command palette — jump to photo, filter by person, open map, navigate to date — would meaningfully speed up the review workflow. BP's web UI is currently mouse-heavy; this would let power users drive it without reaching for the mouse. Relatively self-contained as a JS feature.

### Native macOS / iOS client

Iris ships native apps across Mac, iPhone, iPad, and Apple TV. BP's reviewer is a local web UI served over Flask, which works well enough for Mac and passably on iPad over LAN. A native client would give better platform integration (keyboard shortcuts, share sheets, Continuity Camera, widgets) but represents a much larger engineering surface. Not a current line of thinking — the web UI meets immediate needs — but worth noting as the project matures.

---

## What we are intentionally *not* building

These directions were considered and declined. Recording the reasoning here prevents re-litigating them later.

**Real-time sync / event-driven architecture.** Polling + reconciliation is understandable, debuggable, and recoverable. Real-time distributed sync rapidly becomes fragile and hard to reason about after failures. BP's polling model is a feature, not a limitation.

**Full web UI.** The reviewer UI is a local tool for a specific job. A full web application would carry a large maintenance surface, change the security model (BP is explicitly not hardened for internet-facing deployment), and alter the character of the project entirely.

**AI/ML tagging.** BP is deterministic infrastructure. Probabilistic suggestions from a model introduce low-trust, hard-to-audit outputs into an archival workflow. If this ever becomes interesting, it should be an isolated import pipeline that produces *proposals* subject to the same human-review path as everything else — not a core feature.

**Multi-account support.** Supporting multiple Flickr accounts (personal, family, archive) changes ownership semantics, reconcile logic, and auth handling in ways that touch the whole stack. Deferred until after v1.x stabilises.

**Perceptual hash deduplication.** BP's existing duplicate detection handles the main cases (Snapbridge, device uploads, reuploads) well. Perceptual hashing adds a significant dependency and edge-case surface. The `uncertain` and `reupload_uncertain` categories already route hard cases to human review — which is the right answer.

---

## Related reading

- [Filing Vivian Maier](https://cdevers.github.io/2026/05/12/Filing-Vivian-Maier.html) — the archival philosophy behind BP
- [`docs/pipeline.md`](pipeline.md) — stage contracts and idempotency guarantees
- [`docs/reliability.md`](reliability.md) — failure recovery and operational guarantees
- [GitHub Issues](https://github.com/cdevers/Blue-Pearmain/issues) — canonical backlog
