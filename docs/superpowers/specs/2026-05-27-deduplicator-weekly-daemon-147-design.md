# Design: Deduplicator Weekly Daemon (#147)

**Status:** approved  
**Issue:** [#147](https://github.com/cdevers/Blue-Pearmain/issues/147)  
**Date:** 2026-05-27

---

## Summary

Add a weekly launchd plist that runs the deduplicator automatically — first the `--write` pass (link orphaned siblings, update `photo_count`), then the `--prune --apply` pass (delete zombie groups). Operator no longer needs to invoke `bp deduplicator` manually to keep the duplicate review queue accurate.

---

## Background

The deduplicator is currently invoked by hand. As the scanner adds new Photos records over time, new photos can silently accumulate as orphaned — sharing a hash key with an existing duplicate group but not linked to it. `photo_count` values on stale groups also drift. A weekly automated run self-heals both issues without operator attention.

The two passes **must** be separate invocations because `--prune` exits early before the dedup pass in `main()`.

---

## Design

### New file: `config/com.blue-pearmain.deduplicator.plist`

Weekly launchd agent, modelled on `com.blue-pearmain.reconcile.plist`.

- **Schedule:** Sunday 3am (`StartCalendarInterval`: Weekday=0, Hour=3, Minute=0) — one hour after the reconcile job at 2am, avoiding overlap.
- **Command:** `/bin/sh -c` chain:
  ```
  __UV__ run python __REPO__/bp deduplicator --write --config __REPO__/config/config.yml \
    && __UV__ run python __REPO__/bp deduplicator --prune --apply --config __REPO__/config/config.yml
  ```
  The `&&` means the prune pass only runs if the write pass exits cleanly.
- **Log:** `__HOME__/Library/Logs/BluePearmain/deduplicator.log`
- **Tokens:** `__REPO__`, `__UV__`, `__HOME__` — substituted at install time by `bp install-daemons`, identical to all other plists.
- **ThrottleInterval:** 300 (matches all other daemons).
- **RunAtLoad:** false (matches poller/reconcile).

### Change: `bp` script — `cmd_install_daemons()`

Add `"com.blue-pearmain.deduplicator.plist"` to the `plists` list. One line. The rest of the install machinery (token substitution, `LaunchAgents` directory, dry-run output) is already generic.

---

## Error handling

- If `--write` fails, the `&&` chain prevents `--prune --apply` from running on a potentially inconsistent state.
- launchd `ThrottleInterval: 300` prevents thrashing on repeated crashes.
- Uncaught exceptions go to `deduplicator.log` via `StandardErrorPath`.
- No new error paths introduced — both commands already have their own logging and exit codes.

---

## Testing

Three tests in a new `tests/test_deduplicator_daemon.py`:

1. **`test_deduplicator_plist_exists`** — `config/com.blue-pearmain.deduplicator.plist` exists and is valid XML.
2. **`test_deduplicator_plist_schedule_and_commands`** — plist contains Sunday 3am schedule, both `--write` and `--prune --apply` command strings, and the correct log path token.
3. **`test_install_daemons_includes_deduplicator`** — `bp install-daemons --dry-run` stdout lists the deduplicator plist (confirms it's wired into `cmd_install_daemons`).

---

## Scope

| Artifact | Change |
|---|---|
| `config/com.blue-pearmain.deduplicator.plist` | New file |
| `bp` (install-daemons plist list) | +1 line |
| `tests/test_deduplicator_daemon.py` | New file, 3 tests |

No schema changes. No new Python logic. No changes to the deduplicator itself.
