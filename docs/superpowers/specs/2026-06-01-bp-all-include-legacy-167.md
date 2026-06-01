# `bp all --include-legacy` — Spec (#167)

**Issue:** https://github.com/cdevers/Blue-Pearmain/issues/167
**Status:** Ready for implementation

---

## Goal

Add `--include-legacy` to `bp all` so the full legacy refresh + reclassification pipeline can be run as part of the nightly maintenance sequence without changing the default `bp all` behavior.

---

## Scope

**In scope:**
- `--include-legacy` flag on `bp all`
- Two new steps appended after `pipeline`: `index-legacy` (authoritative full refresh) then `match-legacy --apply`
- Correct dry-run and failure-sequencing behavior

**Out of scope:**
- The indexer logic itself (#162)
- The match-apply logic itself (#166, #168)
- Daemon/scheduling wiring (#13)

---

## Behavior

### Normal run (`bp all --include-legacy`)

Two steps are inserted into the maintenance sequence **after `pipeline` and before `reconcile`**:

```
scan --all
poll
thumbs
sync-names-from-flickr
pipeline
  → index-legacy          ← new
  → match-legacy --apply  ← new
reconcile --fix
sync-albums
sync-album-collections
checkpoint
```

**Rationale for placement:** `pipeline` runs `classify` and auto-applies proposals, so the `candidate_public` pool is fully refreshed before the match step runs. `reconcile` runs after, so it can handle any DB/Flickr mismatches produced by the reclassifications.

Both new steps use the same independent-step contract as every other `bp all` step: a failure is logged, the step is added to `errors[]`, and the run continues.

### `--include-legacy` absent

`bp all` behaves exactly as before — no legacy steps, no change to existing output or error behavior.

---

## Step arguments

| Step | Arg | Value | Rationale |
|------|-----|-------|-----------|
| `index-legacy` | `library` | `None` | Use `legacy_library.path` from config |
| | `no_thumbnails` | `False` | Full index including thumb cache |
| | `limit` | `None` | Authoritative run (enables reconciliation/deletion of stale rows) |
| | `no_cache` | `False` | Use the local DB cache for speed |
| | `refresh_cache` | `False` | Don't force a full cache rebuild nightly |
| `match-legacy --apply` | `apply` | `True` | Full reclassify + metadata propagation |
| | `library_uuid` | `None` | Use the most recently indexed library |
| | `csv` | `None` | No report file |

---

## Dry-run behavior

When `bp all --dry-run --include-legacy` is run, **both legacy steps remain in the sequence but are skipped with a logged notice**, preserving the visible step order:

```
all: -> index-legacy
all: index-legacy SKIPPED (no dry-run support) -- continuing
all: -> match-legacy --apply
all: match-legacy --apply SKIPPED (no dry-run support) -- continuing
```

**Rationale:** Silently omitting the steps would make `--dry-run --include-legacy` structurally diverge from the real run, hiding what would happen. Keeping them visible as `SKIPPED` lets the user verify sequencing.

**No config/path validation in dry-run:** the `_dry_run_skip` wrapper returns before the underlying function is called, so config reading, path resolution, and NAS availability are never checked. Dry-run describes intent without requiring the NAS to be mounted or `legacy_library.path` to be configured.

**Step-result accounting for skipped steps:** SKIPPED steps are **not** added to `errors[]`, do not affect the error count, and do not appear in the final error summary line. They are emitted only as log lines. A run with only SKIPPED legacy steps exits 0 (same as a run where `--include-legacy` was absent). This means dashboards and automation that check the exit code or error summary will not interpret dry-run skips as failures. The log output is the only place a dry-run run is distinguishable from a non-legacy run.

**Implementation:** introduce a `_dry_run_skip(name)` wrapper in `cmd_all` that logs the skip and returns without invoking the underlying function. Steps that have no read-only mode (index-legacy, match-legacy) use this wrapper; steps that have an existing dry-run mechanism (all current steps, which already receive `dry_run=True` via `_step_args`) continue as before.

---

## NAS unreachability and failure sequencing

`cmd_index_legacy` exits `sys.exit(1)` if the library path is not mounted or not configured. The existing `bp all` error loop catches this as `SystemExit(non-zero)`, logs it as a step failure, and continues. No special-casing is needed.

`match-legacy --apply` **always runs**, even if `index-legacy` failed. It operates against whatever is currently in `legacy_assets` (the last successful full index). This is safe because:
- A failed mid-run `index-legacy` provides a **no-destructive-partial-failure guarantee**: rows are upserted incrementally during iteration, so an interruption may leave a partially-refreshed index (some assets updated, none yet deleted), but the authoritative reconciliation/deletion pass in `index_library` only executes after a successful full iteration (see `legacy_indexer.py:205–206`). No existing `legacy_assets` rows are lost on failure.
- `match-legacy --apply` is read-only with respect to `legacy_assets`; it only writes to `photos` and `operation_log`.
- **Freshness caveat:** match results may reflect the most recently successfully indexed state, not necessarily current NAS contents. If the library has changed since the last successful index, the match step operates on stale data until the next successful `index-legacy` run.
- **Operator signal for stale runs:** when `index-legacy` is in `errors[]` at the point `match-legacy --apply` is about to run, `cmd_all` emits an explicit warning **before** dispatching the match step: `all: WARNING index-legacy failed — match-legacy --apply will use the last successfully indexed state`. This prevents a successful match step from being mistaken for a fully fresh legacy refresh. Implementation: after building the steps list, `cmd_all` checks `"index-legacy" in errors` inside the loop, just before calling `cmd_match_legacy`. This is the only place in the sequence where a step inspects prior step outcomes.

If `legacy_library.path` is absent from `config.yml`, both steps fail: `index-legacy` exits code 2 (caught), then `match-legacy --apply` exits because no indexed library exists (caught). Both appear in the errors summary; other steps continue.

---

## Idempotence and rerun safety

`bp all --include-legacy` is safe to run nightly without oscillation or metadata churn:

- **`index-legacy` (authoritative):** `upsert_legacy_asset` is INSERT-OR-REPLACE; re-indexing the same library produces the same rows. The reconciliation pass only deletes assets that have left the library since the last run.
- **`match-legacy --apply`:** `db.apply_legacy_metadata` returns `False` and writes nothing when the photo's staging fields already match (idempotent, verified in tests). `db.reclassify_legacy_match` only writes when `privacy_state` actually changes. Photos reclassified on a prior run remain reclassified and are not touched again.
- **Observability:** reruns may repeat scanning and matching work, but once convergence is reached (all matchable photos reclassified and tagged), subsequent runs produce **zero additional DB mutations, zero reclassification churn, zero metadata rewrites, and no count inflation** in `metadata_applied` or `reclassified`. This can be verified by running `bp all --include-legacy` twice on an unchanged library and comparing `operation_log` rows and match-legacy counts.

---

## Error summary

The final `bp all` error summary already lists every failed step by name. No change needed — `index-legacy` and `match-legacy --apply` appear there if they fail.

---

## Changes required

**`bp` (two changes):**

1. **`cmd_all`**: add `include_legacy = getattr(args, "include_legacy", False)`; introduce `_dry_run_skip` wrapper; splice legacy steps into the list when `include_legacy` is set.
2. **`p_all` arg parser**: add `--include-legacy` flag.

No changes to `cmd_index_legacy`, `cmd_match_legacy`, `index_library`, or any DB/poller module.

---

## Tests

File: `tests/test_cli_all.py` (new or existing — check for the file)

1. **Ordering — `--include-legacy` absent**: legacy steps not in the sequence; existing step order unchanged.
2. **Ordering — `--include-legacy` present**: `index-legacy` and `match-legacy --apply` appear after `pipeline` and before `reconcile`.
3. **Dry-run with `--include-legacy`**: both legacy steps are in the sequence but are skipped (no step function called; log contains "SKIPPED").
4. **Failure sequencing**: `cmd_index_legacy` raises `SystemExit(1)` (monkeypatched) → error recorded in `errors[]` → stale-index warning logged → `cmd_match_legacy` is still called → subsequent steps (`reconcile`, etc.) still run. This is the primary behavioral contract of the feature.
5. **Interruption + convergence (integration test)**: seed a DB with a handful of legacy assets and candidate_public photos; run `index_library` but raise an exception partway through (simulating a partial index); then run `bp all --include-legacy` end-to-end against the partially-indexed DB; assert the final DB state matches what a full uninterrupted run would have produced (same reclassified photos, same proposed_tags). This proves the no-destructive-partial-failure guarantee and the idempotent convergence in combination. Lives in the same test file as the other `bp all` tests; uses monkeypatching to inject the mid-index exception.

---

## Self-review

- **Placeholder scan:** No TBD/TODO. All step args specified. Test cases fully described.
- **Internal consistency:** Dry-run section and failure-sequencing section both name `_dry_run_skip`; both new steps use the same error-continue contract.
- **Scope:** Two edits to `bp`, zero changes to any other module. Focused.
- **Ambiguity:** "Authoritative full refresh" = `limit=None`, no `--refresh-cache`. Explicit in the args table.
