# Propagate legacy keywords/tags into matched photos (#168)

**Status:** design approved 2026-05-31
**Issue:** https://github.com/cdevers/Blue-Pearmain/issues/168
**Follows:** #162 (legacy indexer), #166 (`bp match-legacy --apply`)
**Size:** M (grew from S — title staging needs a schema migration)

---

## Problem

`bp match-legacy --apply` (#166) matches a Flickr-only `candidate_public` photo to a
legacy Photos-4 NAS asset and reclassifies its `privacy_state` via the shared
`classify()`. But the matched legacy asset also carries **descriptive metadata** —
`keywords`, `labels`, `title`, `description` — that the original Flickr upload often
lacks. The legacy library is the richest source of curated keywords for these older
family photos, yet that metadata is indexed and then never surfaced into the
review/publish flow.

The Apple scanner path already proposes tags for Apple photos
(`analyzer.tagger.propose_tags` → `photos.proposed_tags`). This spec extends the
`match-legacy --apply` path to do the equivalent for matched Flickr-only photos.

## Goal

For every Flickr-only `candidate_public` photo that `match-legacy --apply` matches to a
legacy asset (demoted or not), propagate the matched asset's descriptive metadata into
the photo's staged-for-review fields:

- `keywords` + `labels` → `proposed_tags` (merge, de-dupe, normalise — same as Apple).
- `title` → `proposed_title` (**new column**), confident matches only, fill-if-empty.
- `description` → `proposed_description` (existing column), confident matches only, fill-if-empty.

Nothing is written to Flickr. This only edits local staging columns and appends an
`operation_log` audit row.

## Non-goals

- The match/reclassify logic itself (#166) — unchanged.
- Writing anything to Flickr.
- Building metadata-sync Phase 6 (title/description harmonisation). We add the
  `proposed_title` staging column but do not wire up any push/sync of it.
- Propagating to photos outside the `match-legacy --apply` universe (i.e. only
  `candidate_public` Flickr-only photos; the `SELECT` is unchanged).

---

## Decisions (resolved during brainstorming)

| Question | Decision | Rationale |
|---|---|---|
| Keywords only, or + labels? | **Keywords + labels** | Reuse `propose_tags()` so legacy behaves identically to the Apple path — keywords plus blocklist-filtered/remapped ML labels, for free. |
| Confident only, or ambiguous too? | **Tags: both. Title/description: confident only.** | Tags are descriptive, dedup-safe, review-gated → safe to union across ambiguous candidates. Scalar title/description have no safe merge for an ambiguous (multi-candidate) match, so restrict to the single-source confident tier. |
| Title/description too? | **Yes, both.** | User wants the full descriptive payload surfaced for review. |
| Title staging | **Add `proposed_title` (migration 027)** | Only `proposed_description` exists today; mirror it. |
| Atomicity for demoted photos | **Two independent per-photo writes** | Leave #166's frozen, 5-round-reviewed `reclassify_legacy_match` untouched. Metadata write is additive + idempotent; a failure simply re-propagates on the next run. |
| Idempotency / de-dupe | `merge_tags` set-union for tags; fill-if-empty for scalars | Re-running never duplicates tags or clobbers a human-edited draft. |

---

## Architecture

Three layers, mirroring #166's split:

1. **Pure shaping/decision logic** — `poller/legacy_match.py`
2. **Orchestration over db + decision logic** — `poller/legacy_apply.py`
3. **Atomic per-photo persistence** — `db/db.py`

No new osxphotos, NAS, or Flickr access. Pure SQLite + the already-indexed
`legacy_assets` rows.

### Layer 1 — `poller/legacy_match.py` (pure)

Add one function that turns a set of matched legacy assets into the **legacy-derived**
metadata payload — the tags to *add* (not yet merged with the photo's existing tags;
that merge happens once, in the db layer). Reuses `analyzer.tagger.propose_tags` and the
existing `_json_list` helper.

```python
from analyzer.tagger import propose_tags  # add to existing imports

def legacy_metadata_payload(tier: str, matched_assets: list[dict]) -> dict:
    """Build the legacy-derived staging payload for a matched photo.

    add_tags: union of propose_tags() over every matched asset (keywords+labels),
              for both confident and ambiguous tiers. Sorted, deduped, lowercased
              (propose_tags does the casing/blocklist/remap). NOT merged with the
              photo's existing proposed_tags — db.apply_legacy_metadata does that.
    title/description: only for confident (single-asset) matches; None otherwise.
    Returns {"add_tags": [...], "title": str|None, "description": str|None}.
    """
    tags: set[str] = set()
    for asset in matched_assets:
        shaped = {
            "keywords": _json_list(asset.get("keywords")),
            "labels": _json_list(asset.get("labels")),
        }
        tags.update(propose_tags(shaped))

    title = None
    description = None
    if tier == CONFIDENT and matched_assets:
        asset = matched_assets[0]
        title = (asset.get("title") or "").strip() or None
        description = (asset.get("description") or "").strip() or None

    return {"add_tags": sorted(tags), "title": title, "description": description}
```

Notes:
- `CONFIDENT` is the existing constant in this module.
- `propose_tags` already lowercases, applies `LABEL_BLOCKLIST`/`LABEL_REMAP`, and sorts.
- For ambiguous matches, `matched_assets` has >1 entry → tags union across all of them.
- The merge with the photo's *existing* `proposed_tags` is deliberately deferred to the
  db layer (single merge site, and the orchestrator never has to fetch tags itself).

### Layer 3 — `db/db.py`

**Migration 027** (`db/migrations/migrate_027_proposed_title.py`, following the
026 pattern):

```sql
ALTER TABLE photos ADD COLUMN proposed_title TEXT;
```

Also add `proposed_title TEXT` to `db/schema.sql` next to `proposed_description` (line
~91) so fresh databases match migrated ones.

**New method** on the db class (placed next to `reclassify_legacy_match`, ~line 619):

```python
def apply_legacy_metadata(
    self,
    photo_id: int,
    *,
    add_tags: list[str],
    title: str | None = None,
    description: str | None = None,
    trigger: str,
) -> bool:
    """Stage propagated legacy metadata for one photo (one txn).

    - add_tags: legacy-derived tags to merge into the photo's existing
      proposed_tags (set-union, sorted). Already normalised by propose_tags.
    - title / description: written to proposed_title / proposed_description only
      if that column is currently NULL or empty (never clobber a human draft or
      a prior fill).
    Writes one operation_log audit row iff at least one field changed.
    Returns True if anything changed, else False (no write, no log).
    """
```

Behaviour:
- Read the current `proposed_tags`, `proposed_title`, `proposed_description`.
- `proposed_tags`: `merged = sorted(set(current) | set(add_tags))`; update only if
  `merged != current`. (Plain set-union — no `analyzer` import; inputs are already
  lowercased/normalised by `propose_tags` upstream, and existing `proposed_tags` are
  stored normalised. This keeps db.py from depending on the analyzer layer.)
- Scalars: update only when current is NULL/empty *and* the incoming value is non-empty.
- If nothing changed → return False, no UPDATE, no log row (keeps idempotent reruns
  from spamming the audit log).
- If something changed → single `with self.conn:` txn: `UPDATE photos SET ...` (only
  the changed columns) + `INSERT INTO operation_log (..., operation='match_legacy_tags',
  target='proposed_tags', ..., trigger=?, actor='bp')`. Return True.

`reclassify_legacy_match` is **not** modified.

### Layer 2 — `poller/legacy_apply.py` (orchestration)

Extend `apply_legacy_matches`. The loop already visits every candidate_public
Flickr-only photo and its `candidates`. Add a metadata step for matched photos:

```python
from legacy_match import (
    classify_match,            # add
    format_legacy_trigger,
    legacy_metadata_payload,   # add
    normalise_wall_clock,
    resolve_apply_decision,
)
```

In the per-photo loop, after the existing demotion handling:

```python
tier, matched = classify_match(photo, candidates)
if matched:  # confident or ambiguous; no-match has matched == []
    payload = legacy_metadata_payload(tier, matched)
    meta_trigger = format_legacy_trigger(
        matched[0].get("asset_uuid", ""), tier, classifier_version
    )
    try:
        changed = db.apply_legacy_metadata(
            photo["id"],
            add_tags=payload["add_tags"],
            title=payload["title"],
            description=payload["description"],
            trigger=meta_trigger,
        )
        if changed:
            counts["tagged"] += 1
    except Exception:
        counts["metadata_failed"] += 1
```

- The orchestrator does **not** need to fetch `proposed_tags`: `apply_legacy_metadata`
  reads the photo's current tags and merges internally. The existing `SELECT` is
  unchanged.
- `matched[0]` is deterministic — `candidates` come from `iter_legacy_assets`
  (`ORDER BY asset_uuid`), so the trigger provenance and the confident-tier
  title/description source are stable across reruns.
- The demotion path (`resolve_apply_decision` → `reclassify_legacy_match`) is unchanged
  and runs independently of the metadata step. Order: do the demotion first (critical),
  then metadata (additive).

### Counts (additive to #166's frozen schema)

```python
counts = {
    "eligible": len(photos),
    "reclassified": 0,
    "needs_review": 0,
    "auto_private": 0,
    "unchanged": 0,
    "failed": 0,
    "tagged": 0,            # NEW: photos that had metadata propagated
    "metadata_failed": 0,   # NEW: photos whose metadata write raised
}
```

Existing keys keep their #166 meaning. `unchanged` still means *privacy unchanged*; a
photo can be both `unchanged` (privacy) and `tagged` (metadata). The #166 invariants
still hold: `reclassified + unchanged + failed == eligible`, and
`needs_review + auto_private == reclassified`. New invariant: `tagged + metadata_failed
<= eligible` (only matched photos are touched, and only when something actually changed).

### CLI

The `--apply` summary in `bp cmd_match_legacy` gains two lines (tagged,
metadata_failed). No new flags. The `match-legacy` preview path is unchanged.

---

## Data flow

```
match-legacy --apply
  └─ apply_legacy_matches(db, lib, ...)
       per candidate_public Flickr-only photo:
         candidates = by_date[wall_clock(photo.date_taken)]
         ── demotion (unchanged from #166) ──
         decision = resolve_apply_decision(...)
         if decision: db.reclassify_legacy_match(...)   # txn 1 (privacy)
         ── metadata (new, #168) ──
         tier, matched = classify_match(photo, candidates)
         if matched:
           payload = legacy_metadata_payload(tier, matched)
           db.apply_legacy_metadata(...)                # txn 2 (metadata)
```

## Testing

New tests (`tests/test_legacy_tag_propagation.py`):

- **`legacy_metadata_payload` (pure):**
  - confident: `add_tags` from the single asset's keywords+labels; title/description filled.
  - ambiguous: `add_tags` unioned across multiple assets; title/description are None.
  - label blocklist/remap applied (e.g. `people` dropped, `automobile`→`car`).
  - empty keywords/labels → `add_tags == []`, title/description still filled if confident.
- **`db.apply_legacy_metadata`:**
  - merges add_tags into existing proposed_tags (set-union, sorted, deduped, no clobber).
  - fills empty proposed_title/description; leaves a non-empty draft untouched.
  - rerun with same input → returns False, no UPDATE, no new log row (idempotent).
  - add_tags already all present → no change, returns False.
  - writes exactly one operation_log row when something changes; none when nothing does.
  - txn atomicity: a forced failure rolls back the photos update.
- **`apply_legacy_matches` orchestration:**
  - matched-but-not-demoted photo gets `tagged`, stays candidate_public.
  - demoted photo gets both reclassified (txn 1) and tagged (txn 2).
  - no-match photo: untouched, not counted as tagged.
  - idempotent rerun: counts stable, no duplicate tags, no new audit rows.
  - metadata_failed isolates one bad write and continues.
  - counts invariants hold.

Run: `python -m pytest tests/ -q` — full suite green before commit.

## Docs / housekeeping

- `README.md`: note that `match-legacy --apply` also stages legacy keywords/labels into
  `proposed_tags` and legacy title/description into the review fields.
- Reference `#168` in commits; close with a retrospective comment.
- `make lint` clean (mypy + ruff) on every touched file.
- Branch + PR (main is protection-locked); version bump on merge.
