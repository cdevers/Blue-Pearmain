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

### macOS notifications for daemon errors ([#113](https://github.com/cdevers/Blue-Pearmain/issues/113)) `size:S`

Surface high-signal failures (auth expiry, sustained API errors, unresolved reconcile drift) as macOS system notifications rather than log-only events. Optional via config flag; fire-and-forget so it doesn't block daemon operation.

---

## Priority 2 — Policies

*Letting the operator declare intent once, rather than repeating manual decisions.*

BP's current model is reactive: classify what arrives, queue what needs review, apply what's unambiguous. Two gaps have emerged where a persistent *policy* would be more appropriate than a repeated manual action.

### Per-person privacy policy ([#114](https://github.com/cdevers/Blue-Pearmain/issues/114)) `size:M` · ✓ done

A way to declare "any photo containing Person X is always auto-private" — stored in the DB, checked at scan time. Currently, batch-marking all of a person's photos private is a one-shot action; new photos of that person re-enter the queue. A persistent policy is the right primitive for people who should never appear on Flickr (children, people who've asked not to be photographed publicly).

### Tag protection rules ([#115](https://github.com/cdevers/Blue-Pearmain/issues/115)) `size:S`

A config-driven way to declare tag namespaces or specific tags as protected from auto-removal. The sync engine currently treats a tag's absence from one side as a non-conflict eligible for auto-correction. For archival tags (`family/*`, `scanned-film`, `archive/*`) that may not exist in Apple Photos' keyword set, this can produce harmful removals. A lightweight `tag_protection:` section in `config.yml` prevents this without a full policy engine.

---

## Priority 3 — Auditability

*Making BP's decisions explainable — not just now, but years from now.*

BP records state, not causation. You can see that a photo is `auto_private`, but not whether it was geofenced, flagged by a person policy, or manually decided. You can see that a tag is on Flickr, but not when it was added or what triggered it. For a tool designed around archival stewardship, this is a meaningful gap.

### Operation journal ([#116](https://github.com/cdevers/Blue-Pearmain/issues/116)) `size:L` · plan ready

An append-only `operation_log` table in the DB that records every mutation BP makes — to Flickr, to Apple Photos, to DB state — along with the reason and trigger. Covers proposal auto-apply, manual review decisions, reconcile --fix writes, tag-writeback, album pushes, and privacy state changes. Makes BP behave more like an archival system of record than a sync utility.

### `bp reconcile --explain` ([#117](https://github.com/cdevers/Blue-Pearmain/issues/117)) `size:S` · ✓ done

A richer dry-run mode that shows, for each proposed reconcile change: current Flickr state, desired state, source of truth, and the reason for the discrepancy. The current `--dry-run` tells you *what* would change; `--explain` tells you *why*. Especially valuable years after deployment when the original context has been forgotten. If the operation journal (#116) is implemented first, `--explain` can reference journal entries to show when a state was last changed and by what.

---

## Priority 4 — Portability

*Ensuring BP's metadata survives beyond any particular tool or service.*

BP's SQLite database is the authoritative record of everything it knows about your photo archive. But it is a binary format tied to BP's schema. If Flickr changes its API, goes dark, or needs to be migrated away from, acting on that data requires reverse-engineering the DB. A periodic export to portable formats gives durable access independent of any tool or service.

### Export mode ([#118](https://github.com/cdevers/Blue-Pearmain/issues/118)) `size:M` · plan ready

A `bp export` command that serialises the full BP state — per-photo metadata, review decisions, sync state, geofence zones — to JSON or YAML. One file (or directory of files) that can be read without BP, understood without documentation, and imported into a future tool. Not a backup of the DB; a human-readable record of the archive's metadata.

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
