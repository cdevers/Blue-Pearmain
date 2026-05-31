# `bp match-legacy --apply` — reclassify confident legacy matches out of the review queue

**Issue:** [#166](https://github.com/cdevers/Blue-Pearmain/issues/166)
**Follow-up to:** #162 (legacy library indexer)
**Status:** design / awaiting plan
**Target release:** 1.5.0

---

## Problem

The legacy indexer (#162) populated `legacy_assets` with ~34k assets from the
migrated iPhoto/Photos-4 NAS library, including rich Apple metadata that the
Flickr-only photos never had: named persons, unknown-face counts, ML labels,
and geolocation.

Those ~34k Flickr-only photos currently sit in `candidate_public` — the
"propose tags, awaiting confirmation to publish" pool — only because, lacking
Apple people metadata, they had no people signal to demote them. Many of them
are in fact private family photos with people in the frame. The legacy library
*knows* they have people; that knowledge is indexed but nothing consumes it.

`bp match-legacy-preview` already reports which `candidate_public` photos match
which legacy assets (read-only). This feature adds the write path: use the
matched legacy metadata to **re-run the existing privacy classifier** and move
the people/private photos out of the public-candidate pool into the same states
they would have landed in had they been Apple photos all along.

## Goal

For Flickr-only `candidate_public` photos that match a legacy asset, re-classify
their `privacy_state` using the **same `analyzer.privacy.classify()`** that runs
on Apple photos — no impedance mismatch, no separate policy. Photos with people
move to `needs_review`; home/geofenced photos move to `auto_private`; photos
with no people signal stay `candidate_public`.

## Non-goals (deferred)

- Propagating legacy `keywords`/`labels` into `proposed_tags` — **#168**.
- Wiring legacy steps into `bp all` — **#167**.
- Any write back to Flickr (title/description/visibility). This command only
  edits local `privacy_state`.

---

## Command surface

Consolidate the existing read-only `match-legacy-preview` subcommand into a
single `match-legacy` subcommand whose **default is preview** (no writes) and
whose `--apply` flag performs the reclassification.

```
bp match-legacy                         # preview (current behaviour, no writes)
bp match-legacy --apply                  # perform reclassification
bp match-legacy --library-uuid <uuid>    # pick library when >1 indexed
bp match-legacy --csv report.csv         # write the tiered report (either mode)
```

- `match-legacy-preview` is **removed** (it shipped only in 1.4.0; no stable
  consumers, no back-compat shim per project convention). Its handler
  `cmd_match_legacy_preview` is renamed/extended to `cmd_match_legacy`.
- `--apply` defaults to `False`. Without it the command behaves exactly as
  `match-legacy-preview` does today (prints tier counts; optional `--csv`).
- With `--apply`, after printing the preview counts the command performs the
  writes and prints an applied-counts summary.

## Scope of matches acted on (Approach 2 — errs private)

The matcher (`legacy_match.classify_match`) returns a tier per photo:

- **confident** — exactly one legacy asset matches (timestamp + dims, no title
  conflict).
- **ambiguous** — multiple candidate assets, or a single candidate with a
  dim/title conflict.
- **no-match** — no legacy asset at that wall-clock timestamp.

`--apply` acts on a photo when **either**:

1. the tier is **confident**, or
2. the tier is **ambiguous and every candidate asset is people-positive**
   (`named_face_count > 0` OR `unknown_face_count > 0` OR a non-empty named
   `persons` list OR a people label).

Rationale (errs private): when all ambiguous candidates agree "people present",
the *privacy outcome* is identical regardless of which asset is the true match,
so it is safe to demote to `needs_review`. When candidates disagree about people
presence we cannot tell whether demotion is warranted, so we **leave the photo
untouched** rather than risk leaving a people-photo sitting as a public
candidate. `no-match` photos are never touched.

This was chosen over "confident only" (leaves ~1,400 people-positive ambiguous
photos in the public pool) and over "confident + any-people ambiguous" (acts on
ambiguous photos whose candidates disagree, which is not identity-safe).

Rough estimates from the indexed library (`/tmp/estimate_apply.py`):

| Approach | candidate_public photos remaining |
|---|---|
| 1 — confident only | ~23,644 |
| **2 — confident + all-people ambiguous (this design)** | **~22,208** |
| 3 — confident + any-people ambiguous | ~22,114 |

## Classifier parity — shaping a legacy asset for `classify()`

`classify(photo, zones, self_name, person_policies)` already reads both record
shapes via `_get_persons` / `_get_labels`. The legacy record must be shaped to
match what Apple records provide, with one reconstruction:

- `latitude`, `longitude` → from the legacy asset (drives geofence rules 1–2).
- `persons` → the legacy named-`persons` JSON list **plus** `"_UNKNOWN_"`
  injected `unknown_face_count` times. The Apple path encodes unknown faces as
  `_UNKNOWN_` entries / `face_info`; legacy stores them only as the integer
  `unknown_face_count`, so we reconstruct the `_UNKNOWN_` sentinels the
  classifier already counts (`privacy.py:163`). Without this, unknown-face-only
  legacy photos would wrongly classify as `candidate_public`.
- `labels` → the legacy `labels` JSON list (drives the people-label rule).
- No `place_ishome` / `place` (legacy has no home flag) → rule 1 simply doesn't
  fire; geofence zones still apply via lat/lon.
- No `media_analysis` → the body-detection rule doesn't fire. Acceptable: named
  persons, unknown faces, and people labels already cover the legacy signals.

`zones`, `self_name`, and `person_policies` are sourced identically to the
scanner: `zones = db.active_zones()`, `person_policies = db.get_person_policies()`,
`self_name = config["photos_library"]["self_name"]`. This guarantees a legacy
photo of `self_name` alone is not demoted, matching Apple behaviour.

For an **ambiguous, all-people** match there are multiple candidate assets. We
classify a synthesised record that unions the people signals (any named person,
summed unknown faces, unioned labels) and uses the first candidate's lat/lon —
the outcome is `needs_review`/`auto_private` regardless, so the union is safe.

## Transition rule

For each acted-on photo:

1. Read its current `privacy_state`. **Only `candidate_public` photos are
   eligible** — human-reviewed states (`approved_public`, `keep_private`,
   `already_public`, `skipped`, `approved_friends`, `approved_family`,
   `approved_friends_family`) and any other state are skipped, mirroring the
   scanner's guard (`scanner.py:543`).
2. Run `classify()` on the shaped legacy record.
3. If the new state == `candidate_public`, **no-op** (no write, no log).
4. Otherwise call `db.set_privacy_state(photo_id, new_state, new_reason)` and
   `db.log_operation(...)`.

The candidate query gains `id` (needed for `set_privacy_state`):

```sql
SELECT id, flickr_id, date_taken, width, height, flickr_title
FROM photos WHERE uuid IS NULL AND privacy_state = 'candidate_public'
```

## Audit trail

Each reclassification appends one `operation_log` row:

```python
db.log_operation(
    photo_id,
    operation="match_legacy_apply",
    target="privacy_state",
    old_value="candidate_public",
    new_value=new_state,
    trigger=f"legacy:{asset_uuid} tier={tier}",
    actor="bp",
)
```

`log_operation` is fire-and-forget (swallows errors), so journaling never
interrupts the reclassification.

## Idempotency

Running `--apply` twice is safe. The second run re-queries `candidate_public`
photos; the ones demoted on the first run are no longer in that pool, so they
are not seen. Photos that stayed `candidate_public` re-evaluate to the same
state and no-op. No duplicate log spam beyond the genuine first transition.

## Output

Preview mode prints today's report unchanged. `--apply` additionally prints:

```
Applied legacy reclassification:
  reclassified : <n> photos moved out of candidate_public
    needs_review : <a>
    auto_private : <b>
  unchanged    : <c> (stayed candidate_public)
  skipped      : <d> (already human-reviewed / not eligible)
```

## Testing (TDD)

Unit tests in `tests/` (pure logic, no osxphotos / no NAS):

1. **Shaping** — `unknown_face_count=2`, empty named persons → shaped record has
   two `_UNKNOWN_` entries → `classify()` returns `needs_review`.
2. **Named person demotes** — legacy `persons=["Aunt May"]`, `self_name="Me"` →
   `needs_review`.
3. **Self-only does not demote** — legacy `persons=["Me"]`, `self_name="Me"`,
   no other signal → stays `candidate_public` → photo untouched.
4. **No people, no geo** → stays `candidate_public` → no write, no log.
5. **Geofenced home** — legacy lat/lon inside an `auto_private` zone →
   `auto_private`.
6. **Confident match is acted on**; **no-match is never acted on**.
7. **Ambiguous all-people** → acted on (demoted); **ambiguous mixed** (one
   candidate people-positive, one not) → **not** acted on (untouched).
8. **Eligibility guard** — a matched photo already in `keep_private` /
   `approved_public` is never modified.
9. **Idempotency** — two `--apply` passes produce one transition and one log
   row per photo.
10. **Audit** — a transition writes one `operation_log` row with
    `operation="match_legacy_apply"` and the expected old/new values.

Run: `python -m pytest tests/ -q` (all green before commit). `make lint`
(mypy + ruff) clean on touched files.

## Files

- **Modify** `bp` — rename `match-legacy-preview` → `match-legacy`; add
  `--apply`; extend `cmd_match_legacy_preview` → `cmd_match_legacy` with the
  apply path; `id` added to the candidate query; update dispatch + arg defaults.
- **Modify** `poller/legacy_match.py` — add a helper to shape a legacy asset (or
  unioned candidate set) into a `classify()`-ready dict, including `_UNKNOWN_`
  reconstruction, and a predicate for "people-positive".
- **Create** `tests/test_match_legacy_apply.py` — the tests above.
- **Modify** `README.md` — document `bp match-legacy [--apply]` (replacing the
  `match-legacy-preview` mention).
- **Modify** `docs/superpowers/specs/2026-05-30-legacy-library-indexer-162-design.md`
  — cross-reference this follow-up (optional).
