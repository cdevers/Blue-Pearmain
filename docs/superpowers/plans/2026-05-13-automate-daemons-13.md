# Plan: GH #13 — Automate bp-all maintenance as launchd daemons

## Context

GH #13 asked to wire `bp-all` maintenance operations into launchd schedules. Most of that work has already landed:

| Operation | Issue asked | Current state |
|---|---|---|
| `bp sync-metadata` | Add to pipeline | ✓ Done — `cmd_pipeline` inside `cmd_all` |
| `bp reconcile --fix` | Weekly plist | ✓ Runs every 6h via `cmd_all` in pipeline plist |
| `bp sync-albums` | Add to pipeline | ✓ Done — `cmd_all` step 7 |
| `bp checkpoint` | Add to pipeline | ✓ Done — `cmd_all` step 9 |
| `bp thumbs` | After each `bp poll` | ✗ **Gap** — only runs via `cmd_all` every 6h |
| `bp reconcile --fix` weekly plist | Explicit weekly plist | ✗ Not created (but 6h coverage makes it redundant) |
| Update `bp-all` | Simplify to manual-only ops | ✓ `cmd_all` is already the full automation runner |

**Two remaining items:** thumbs after poll (genuine gap), and an explicit weekly reconcile plist (redundant but was specified in the issue).

---

## The gap: thumbnails lag up to 6h after new photos are polled

The poller plist runs `bp poll` every hour. Newly polled photos don't get thumbnails until `bp all` runs (every 6h), so the review UI can show blank thumbnails for up to 6 hours after a photo arrives.

---

## Task 1 — Thumbs after poll (poller plist)

**File:** `config/com.blue-pearmain.poller.plist`

Change `ProgramArguments` from the bare poll invocation to a shell chain:

```xml
<key>ProgramArguments</key>
<array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>__UV__ run python __REPO__/bp poll --config __REPO__/config/config.yml &amp;&amp; __UV__ run python __REPO__/bp thumbs --config __REPO__/config/config.yml</string>
</array>
```

Note: `&amp;&amp;` is the XML-escaped form of `&&`. The `__UV__` / `__REPO__` tokens are still substituted by `cmd_install_daemons` via plain string replacement.

Update the comment header to document the new behavior.

**Why shell wrapper over internal change to `cmd_poll`:** Keeps `bp poll` semantically pure. Adding thumbs inside `cmd_poll` would run thumbs twice in `cmd_all` (once via poll, once via the explicit thumbs step). Shell chaining avoids that without adding a `--skip-thumbs` workaround.

---

## Task 2 — Weekly reconcile plist (new file)

**File (new):** `config/com.blue-pearmain.reconcile.plist`

Label: `com.blue-pearmain.reconcile`  
Schedule: `CalendarInterval` — Weekday=0 (Sunday), Hour=2, Minute=0  
Command: `bp reconcile --fix --config __REPO__/config/config.yml`  
Logs: `__HOME__/Library/Logs/BluePearmain/reconcile.log`

Note: the pipeline plist already runs reconcile every 6h via `bp all`. This plist is belt-and-suspenders for any drift that accumulated over the week, and makes the intent explicit.

---

## Task 3 — Update `cmd_install_daemons` in `bp`

Add the reconcile plist to the hardcoded list (line ~314):

```python
plists = [
    "com.blue-pearmain.poller.plist",
    "com.blue-pearmain.pipeline.plist",
    "com.blue-pearmain.reviewer.plist",
    "com.blue-pearmain.reconcile.plist",   # new
]
```

Also update the printed post-install instructions to include the reconcile plist's `launchctl bootstrap` command.

---

## Task 4 — Tests (TDD — write tests first)

**`tests/test_core.py`** — extend `TestInstallDaemons`:

- `test_installs_four_plists` — update existing `len == 3` assertion to `4`
- `test_poller_plist_runs_thumbs_after_poll` — installed poller plist contains `"thumbs"`
- `test_reconcile_plist_has_weekly_calendar_interval` — installed reconcile plist contains `"CalendarInterval"` and `"Weekday"`

Note: `test_tokens_substituted_in_installed_files` asserts `len(installed) == 3` — update to `4`.

---

## Files changed

| File | Change |
|---|---|
| `config/com.blue-pearmain.poller.plist` | Chain `bp thumbs` after `bp poll` via `/bin/sh -c` |
| `config/com.blue-pearmain.reconcile.plist` | **New** — weekly Sunday reconcile daemon |
| `bp` | Add reconcile plist to `cmd_install_daemons` list (~line 314) |
| `tests/test_core.py` | Update install-daemons tests; add thumbs + reconcile plist assertions |

---

## Verification

1. `python -m pytest tests/test_core.py::TestInstallDaemons -v` — all pass
2. `bp install-daemons --dry-run` — shows 4 plists
3. `bp install-daemons` — all 4 plists written to `~/Library/LaunchAgents/`
4. Inspect `~/Library/LaunchAgents/com.blue-pearmain.poller.plist` — contains `thumbs` in the shell command string
5. Inspect `~/Library/LaunchAgents/com.blue-pearmain.reconcile.plist` — contains `CalendarInterval`, no `__REPO__` tokens
6. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.blue-pearmain.reconcile.plist` loads successfully
7. Bounce the poller daemon; `tail -f ~/Library/Logs/BluePearmain/poller.log` shows both poll and thumbs lines
