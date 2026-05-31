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

### Ambiguous (all-people) candidate aggregation

For an **ambiguous, all-people** match there are multiple candidate legacy
assets. Rather than synthesising a unioned record, we **classify each candidate
independently** and take the most-private outcome, deterministically:

1. **Deterministic ordering.** Sort the candidates by `asset_uuid` ascending
   before evaluation. (Candidates already share the same wall-clock timestamp,
   so `asset_uuid` is the stable, reproducible tiebreak.)
2. **Classify each** shaped candidate record with `classify()`.
3. **State precedence (most-private wins):**
   `auto_private` > `needs_review` > `candidate_public`.
   Pick the candidate whose state ranks highest.
4. **Reason tiebreak.** Among candidates that produced the winning state, the
   reason and `asset_uuid` come from the **first in sorted order**. This makes
   the stored reason string fully reproducible regardless of DB row order.

A confident match is the degenerate single-candidate case of the same rule.

## Transition rule

For each acted-on photo:

1. Read its current `privacy_state`. **Only `candidate_public` photos are
   eligible** — human-reviewed states (`approved_public`, `keep_private`,
   `already_public`, `skipped`, `approved_friends`, `approved_family`,
   `approved_friends_family`) and any other state are skipped, mirroring the
   scanner's guard (`scanner.py:543`).
2. Run `classify()` on the shaped legacy record.
3. If the new state == `candidate_public`, **no-op** (no write, no log).
4. Otherwise write the new state and the audit row (see below).

The candidate query gains `id` (needed for `set_privacy_state`):

```sql
SELECT id, flickr_id, date_taken, width, height, flickr_title
FROM photos WHERE uuid IS NULL AND privacy_state = 'candidate_public'
```

### Reason string schema (frozen)

The stored `privacy_reason` is structured provenance, parseable and stable:

```
legacy-match[tier=<tier>,asset=<asset_uuid>]: <classifier_reason>
```

- `<tier>` — `confident` or `ambiguous`.
- `<asset_uuid>` — the winning candidate's `asset_uuid` (the sole asset for
  confident; the first-in-sorted-order winner for ambiguous, per the
  aggregation rule above).
- `<classifier_reason>` — the verbatim reason returned by `classify()` (e.g.
  `named person(s): Aunt May`).

Example:
`legacy-match[tier=confident,asset=A1B2-...]: 2 unidentified face(s)`

## Audit trail and transaction semantics

Each reclassification writes **two changes that must commit atomically** — the
`photos.privacy_state` update and the `operation_log` row — so an interruption
leaves both or neither. We must never end up with a demoted photo and no audit
trail, or an audit row for a state change that didn't land.

The existing helpers each commit on their own (`set_privacy_state` commits;
`log_operation` commits and swallows errors), so they **cannot** be called
back-to-back here. Instead, perform both writes inside a single transaction with
one commit:

```python
with db.conn:                      # single transaction; commits or rolls back
    db.conn.execute(
        "UPDATE photos SET privacy_state=?, privacy_reason=?, "
        "date_synced=?, updated_at=? WHERE id=?",
        (new_state, reason_str, _now_iso(), _now_iso(), photo_id),
    )
    db.conn.execute(
        "INSERT INTO operation_log "
        "(occurred_at, photo_id, operation, target, old_value, new_value, "
        " trigger, actor) VALUES (?,?,?,?,?,?,?,?)",
        (_now_iso(), photo_id, "match_legacy_apply", "privacy_state",
         "candidate_public", new_state,
         f"legacy:{asset_uuid} tier={tier} clf={CLASSIFIER_VERSION}", "bp"),
    )
```

(Equivalently, a small `db` helper — e.g. `reclassify_with_audit(...)` — that
wraps the two statements in one transaction. Pick whichever keeps `bp` thin;
the atomicity requirement is the binding part.) Unlike the fire-and-forget
`log_operation`, an audit-write failure here must roll the whole reclassification
back, not be swallowed.

### Frozen audit-row shape

Every reclassification writes exactly one `operation_log` row with this **frozen
payload** (reports and tooling may depend on it, so the strings are part of the
contract, not ad hoc):

| column      | frozen value                                                              |
| ----------- | ------------------------------------------------------------------------- |
| `operation` | the literal `match_legacy_apply`                                          |
| `target`    | the literal `privacy_state`                                               |
| `old_value` | the literal `candidate_public` (the only eligible source state)           |
| `new_value` | the winning state — `needs_review` or `auto_private`                      |
| `trigger`   | `legacy:<asset_uuid> tier=<tier> clf=<classifier_version>` (exact format) |
| `actor`     | the literal `bp`                                                          |

The `trigger` format is fixed: space-separated `key=value` tokens, leading
`legacy:<asset_uuid>` token, then `tier=` then `clf=`. The matching
`asset_uuid` and `tier` also appear in `privacy_reason` via the frozen reason
schema (`legacy-match[tier=<tier>,asset=<asset_uuid>]: <classifier_reason>`), so
both the human-facing reason and the machine-facing trigger carry provenance.
A test asserts each of these fields by value, not just row presence.

### Failure scope across the run

Atomicity is **per photo**, and the run continues past a failure. The two
guarantees compose like this:

- **Within one photo:** the `privacy_state` update and its `operation_log` row
  commit together or not at all (the transaction above).
- **Across the batch:** `apply_legacy_matches` evaluates and writes one photo at
  a time. If a single photo's transaction raises, that photo is rolled back,
  counted under `failed`, and processing **continues** with the next photo. One
  bad row never aborts the whole run.

This is the right default because the operation is idempotent and re-runnable: a
photo that failed (or was never reached) is still `candidate_public` on the next
`--apply` pass and gets another attempt, so an isolated failure costs nothing
but a retry. Aborting the entire batch on the first error would instead strand
all the already-correct work behind one transient fault. Per-photo failures are
surfaced in the `failed` count (and logged), so they are visible rather than
silent.

### Recording the classifier ruleset version

The classifier's rules evolve. To keep historical reclassifications
explainable, the audit row records which ruleset produced the decision. Add a
module-level `CLASSIFIER_VERSION` constant to `analyzer/privacy.py` (a small
integer, bumped by hand whenever the rules in `classify()` change) and embed it
in the `operation_log.trigger` string as `clf=<N>` (shown above). This way a
future rules change can be correlated against the version stamped on each
historical decision, rather than guessing which logic was in force. The
constant lives next to the rules it versions so the two stay in sync.

This value exists **purely for provenance** — nothing in BP reads it back to
make decisions. A stale version (the rules changed but the constant was not
bumped) is acceptable and at worst makes one batch of audit rows look like they
came from an earlier ruleset; it never produces a wrong privacy decision or a
crash. Treat bumping it as a courtesy to your future self, not a blocking
maintenance obligation. We deliberately avoid any machinery (hashing,
auto-derivation) that would turn a forgotten bump into a hard failure.
**Version mismatches never block execution and never affect classification
outcome** — the value is recorded and read by humans, never gated on. It must
not become a migration gate or compatibility check.

## Idempotency

Running `--apply` twice is safe. The second run re-queries `candidate_public`
photos; the ones demoted on the first run are no longer in that pool, so they
are not seen. Photos that stayed `candidate_public` re-evaluate to the same
state and no-op. No duplicate log spam beyond the genuine first transition.

## Output

Preview mode prints today's report unchanged. `--apply` additionally prints:

```
Applied legacy reclassification:
  eligible     : <e> candidate_public photos evaluated
  reclassified : <n> photos moved out of candidate_public
    needs_review : <a>
    auto_private : <b>
  unchanged    : <c> (matched but no people signal, or no/ambiguous-mixed match)
  failed       : <f> (per-photo write/audit errors, rolled back and skipped)
```

### Frozen counts contract

`apply_legacy_matches()` returns a `dict[str, int]` with **exactly** these keys
(the CLI summary above is just a render of them; freezing them now keeps CLI
output and any future automation from drifting):

| key            | meaning                                                                                |
| -------------- | -------------------------------------------------------------------------------------- |
| `eligible`     | photos selected for evaluation (`WHERE privacy_state='candidate_public'`)               |
| `reclassified` | photos demoted out of `candidate_public` (equals `needs_review + auto_private`)          |
| `needs_review` | subset of `reclassified` whose winning state was `needs_review`                          |
| `auto_private` | subset of `reclassified` whose winning state was `auto_private`                          |
| `unchanged`    | selected photos that stayed `candidate_public` (no people signal, or ambiguous-mixed)    |
| `failed`       | photos whose per-photo transaction raised; rolled back, counted, and skipped (see above) |

**Invariant:** `reclassified + unchanged + failed == eligible`, and
`needs_review + auto_private == reclassified`. A test asserts both.

The eligibility guard (only `candidate_public`, never human-reviewed states)
is the candidate query's `WHERE privacy_state = 'candidate_public'` clause —
ineligible photos are never selected, so there is no separate "skipped" count.
`unchanged` covers every selected photo that was not demoted, including the
ambiguous-mixed case (no separate `skipped_ambiguous` bucket — those photos are
genuinely left in `candidate_public`, which is what `unchanged` means).

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
8. **Ambiguous aggregation precedence** — candidates classify to
   `needs_review` and `auto_private` → winning state is `auto_private`
   (most-private wins).
9. **Ambiguous reason determinism** — given two candidates that both yield the
   winning state, the stored `privacy_reason` carries the lower `asset_uuid`'s
   reason regardless of input row order (feed the same pair reversed → identical
   reason string).
10. **Eligibility guard (human decision preserved)** — a matched photo whose
    legacy classifier says `auto_private` but whose current state is
    `approved_public` is **unchanged** (no write, no log). Repeat for
    `keep_private`.
11. **Idempotency** — two `--apply` passes produce one transition and one log
    row per photo.
12. **Audit payload (frozen shape)** — a transition writes exactly one
    `operation_log` row whose fields match the frozen audit-row shape by value:
    `operation="match_legacy_apply"`, `target="privacy_state"`,
    `old_value="candidate_public"`, `new_value=<state>`, `actor="bp"`, and
    `trigger == f"legacy:{asset_uuid} tier={tier} clf={CLASSIFIER_VERSION}"`
    (exact string, not just a `clf=` substring); the `privacy_reason` matches
    the frozen reason schema.
13. **Rollback on audit failure** — monkeypatch the `operation_log` INSERT to
    raise mid-transaction; assert the photo's `privacy_state` is still
    `candidate_public`, `privacy_reason` is unchanged, and no `operation_log`
    row was written. Proves the transactional invariant directly rather than
    inferring it.
14. **Counts contract** — over a mixed batch, `apply_legacy_matches()` returns a
    dict with exactly the frozen keys, and the invariants hold:
    `reclassified + unchanged + failed == eligible` and
    `needs_review + auto_private == reclassified`.
15. **Failure isolation** — with two eligible photos where the first photo's
    audit write is monkeypatched to raise, assert the second photo is still
    reclassified, `failed == 1`, and the run did not abort.
16. **No-op means truly no write** — for both no-op cases (a confident match
    whose classifier verdict is `candidate_public`, and an ambiguous-mixed
    match that is skipped), assert the photo stays `candidate_public`, its
    `privacy_reason` is byte-for-byte unchanged, and the `operation_log` row
    count is unchanged (zero rows written). Guards the invariant that a no-op
    touches neither `photos` nor `operation_log`.

Run: `python -m pytest tests/ -q` (all green before commit). `make lint`
(mypy + ruff) clean on touched files.

## Files

- **Modify** `bp` — rename `match-legacy-preview` → `match-legacy`; add
  `--apply`; extend `cmd_match_legacy_preview` → `cmd_match_legacy` with the
  apply path; `id` added to the candidate query; update dispatch + arg defaults.
- **Modify** `poller/legacy_match.py` — add pure helpers: shape a legacy asset
  into a `classify()`-ready dict (including `_UNKNOWN_` reconstruction), a
  predicate for "people-positive", and `resolve_apply_decision()` (tier gating +
  per-candidate classify + ordering + most-private precedence + frozen reason).
- **Create** `poller/legacy_apply.py` — `apply_legacy_matches(db, library_uuid,
  *, self_name, zones, person_policies, classifier_version)` orchestration:
  query eligible photos, build the wall-clock index, call
  `resolve_apply_decision`, write via the atomic db helper, return a counts dict.
  Keeps `bp` thin and the loop unit-testable against a temp DB.
- **Modify** `db/db.py` — add `reclassify_legacy_match(...)` performing the
  atomic `privacy_state` + `operation_log` write in one transaction.
- **Modify** `analyzer/privacy.py` — add the `CLASSIFIER_VERSION` constant
  stamped into each audit row.
- **Create** `tests/test_match_legacy_apply.py` — the tests above.
- **Modify** `README.md` — document `bp match-legacy [--apply]` (replacing the
  `match-legacy-preview` mention).
- **Modify** `docs/superpowers/specs/2026-05-30-legacy-library-indexer-162-design.md`
  — cross-reference this follow-up (optional).
