# Deduplicator Weekly Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a weekly launchd plist that automatically runs `bp deduplicator --write` then `bp deduplicator --prune --apply` every Sunday at 3am, keeping duplicate groups self-healing without manual intervention.

**Architecture:** New `config/com.blue-pearmain.deduplicator.plist` (modelled on the existing `reconcile.plist` pattern); extend `bp deduplicator` CLI with `--prune` and `--apply` flags so the plist can drive both passes through the `bp` entry point; wire the new plist into `bp install-daemons`. No schema changes, no new Python logic in the deduplicator itself.

**Tech Stack:** Python, launchd plist XML, `argparse`, `unittest`

---

## File Map

| File | Action | What changes |
|---|---|---|
| `config/com.blue-pearmain.deduplicator.plist` | Create | New weekly launchd agent template |
| `bp` | Modify | Add `--prune`/`--apply` to `p_dedup` subparser + `cmd_deduplicator()`; add plist to `cmd_install_daemons()` list; add hasattr defaults |
| `tests/test_core.py` | Modify | 2 new tests in `TestInstallDaemons`; update plist count (4→5); update `TestUninstallDaemons._install_fake_plists` (4→5) |

---

## Task 1: Write failing tests

**Files:**
- Modify: `tests/test_core.py` (class `TestInstallDaemons` around line 7998; class `TestUninstallDaemons` around line 8115)

- [ ] **Step 1: Update the plist-count assertion in `TestInstallDaemons`**

In `tests/test_core.py`, find `test_tokens_substituted_in_installed_files` (around line 8031) and change the count:

```python
# was:
self.assertEqual(len(installed), 4)
# becomes:
self.assertEqual(len(installed), 5)
```

- [ ] **Step 2: Add two new tests to `TestInstallDaemons`**

Insert both methods before the closing of `TestInstallDaemons` (just before the `class TestUninstallDaemons` line, around line 8114):

```python
    def test_deduplicator_plist_runs_write_and_prune(self):
        """Installed deduplicator plist chains --write then --prune --apply."""
        import tempfile

        bp = self._import_bp()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_agents = fake_home / "Library" / "LaunchAgents"
            fake_agents.mkdir(parents=True)
            with (
                unittest.mock.patch("shutil.which", return_value="/fake/uv"),
                unittest.mock.patch.object(Path, "home", return_value=fake_home),
            ):
                bp.cmd_install_daemons(self._args())
            dedup = fake_agents / "com.blue-pearmain.deduplicator.plist"
            self.assertTrue(dedup.exists(), "deduplicator plist not installed")
            text = dedup.read_text()
            self.assertIn("deduplicator", text)
            self.assertIn("--write", text)
            self.assertIn("--prune", text)
            self.assertIn("--apply", text)

    def test_deduplicator_plist_has_sunday_3am_schedule(self):
        """Installed deduplicator plist runs on Sunday at 3am."""
        import tempfile

        bp = self._import_bp()
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_agents = fake_home / "Library" / "LaunchAgents"
            fake_agents.mkdir(parents=True)
            with (
                unittest.mock.patch("shutil.which", return_value="/fake/uv"),
                unittest.mock.patch.object(Path, "home", return_value=fake_home),
            ):
                bp.cmd_install_daemons(self._args())
            dedup = fake_agents / "com.blue-pearmain.deduplicator.plist"
            text = dedup.read_text()
            self.assertIn("StartCalendarInterval", text)
            self.assertIn("Weekday", text)
            # Hour 3 appears as <integer>3</integer>
            self.assertIn("<integer>3</integer>", text)
```

- [ ] **Step 3: Update `TestUninstallDaemons._install_fake_plists` to include the new plist**

Find `_install_fake_plists` in `TestUninstallDaemons` (around line 8137) and add the deduplicator entry:

```python
    def _install_fake_plists(self, fake_agents: Path):
        plists = [
            "com.blue-pearmain.poller.plist",
            "com.blue-pearmain.pipeline.plist",
            "com.blue-pearmain.reviewer.plist",
            "com.blue-pearmain.reconcile.plist",
            "com.blue-pearmain.deduplicator.plist",
        ]
        for name in plists:
            (fake_agents / name).write_text("<plist/>")
        return plists
```

- [ ] **Step 4: Run the new tests to confirm they fail for the right reason**

```bash
python -m pytest tests/test_core.py::TestInstallDaemons::test_deduplicator_plist_runs_write_and_prune tests/test_core.py::TestInstallDaemons::test_deduplicator_plist_has_sunday_3am_schedule tests/test_core.py::TestInstallDaemons::test_tokens_substituted_in_installed_files -v
```

Expected: all three FAIL.
- Count test: `AssertionError: 4 != 5`
- Content/schedule tests: `AssertionError: deduplicator plist not installed`

Do NOT commit yet.

---

## Task 2: Create plist + extend `bp` CLI

**Files:**
- Create: `config/com.blue-pearmain.deduplicator.plist`
- Modify: `bp`

### 2a: Create the plist template

- [ ] **Step 1: Create `config/com.blue-pearmain.deduplicator.plist`**

Create the file with this exact content:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  com.blue-pearmain.deduplicator.plist
  launchd agent: weekly deduplicator run (Sunday 3am)

  Runs 'bp deduplicator --write' then 'bp deduplicator --prune --apply' once
  a week to link orphaned duplicate siblings and prune zombie groups.
  Scheduled one hour after the reconcile job (Sunday 2am) to avoid overlap.

  Install with bp (substitutes __REPO__, __UV__, __HOME__ automatically):
    bp install-daemons

  Or install manually after substituting tokens:
    launchctl bootstrap gui/$(id -u) \
       ~/Library/LaunchAgents/com.blue-pearmain.deduplicator.plist

  Run now (without waiting for schedule):
    launchctl start com.blue-pearmain.deduplicator

  Uninstall:
    launchctl bootout gui/$(id -u) \
       ~/Library/LaunchAgents/com.blue-pearmain.deduplicator.plist
    rm ~/Library/LaunchAgents/com.blue-pearmain.deduplicator.plist

  Logs:
    tail -f __HOME__/Library/Logs/BluePearmain/deduplicator.log
-->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.blue-pearmain.deduplicator</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>__UV__ run python __REPO__/bp deduplicator --write --config __REPO__/config/config.yml &amp;&amp; __UV__ run python __REPO__/bp deduplicator --prune --apply --config __REPO__/config/config.yml</string>
    </array>

    <!-- Run every Sunday at 3:00am (one hour after reconcile at 2am) -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <!-- Stderr for uncaught exceptions; stdout is handled by RotatingFileHandler -->
    <key>StandardErrorPath</key>
    <string>__HOME__/Library/Logs/BluePearmain/deduplicator.log</string>

    <!-- Working directory -->
    <key>WorkingDirectory</key>
    <string>__REPO__</string>

    <!-- Don't thrash if it keeps crashing -->
    <key>ThrottleInterval</key>
    <integer>300</integer>
</dict>
</plist>
```

### 2b: Add `--prune` and `--apply` to `bp deduplicator` CLI

The plist uses `bp deduplicator --prune --apply`, but these flags don't exist in the `bp` subparser yet. Three locations need updating.

- [ ] **Step 2: Add `--prune` and `--apply` to the `p_dedup` subparser**

Find the `# deduplicator` block in `bp` (around line 1013). The current block ends with:

```python
    p_dedup.add_argument("--verbose", "-v", action="store_true", help="Show all uncertain pairs in report")
```

Add two lines immediately after:

```python
    p_dedup.add_argument("--prune", action="store_true",
                         help="Clean up zombie groups and stale photo_count values (dry-run by default)")
    p_dedup.add_argument("--apply", action="store_true",
                         help="Execute --prune changes (default is dry-run)")
```

- [ ] **Step 3: Handle `--prune` and `--apply` in `cmd_deduplicator()`**

Find `cmd_deduplicator` (around line 420). The current `pairs` block ends with:

```python
    if args.verbose:
        pairs.append(("--verbose", True))
```

Add two lines immediately after:

```python
    if args.prune:
        pairs.append(("--prune", True))
    if args.apply:
        pairs.append(("--apply", True))
```

- [ ] **Step 4: Add hasattr defaults for `prune` and `apply`**

Find the hasattr defaults block (around line 1067). The block currently has:

```python
    if not hasattr(args, "write"):          args.write = False
    if not hasattr(args, "confirm"):        args.confirm = False
    if not hasattr(args, "out"):           args.out = None
```

Add two lines after the `confirm` line:

```python
    if not hasattr(args, "prune"):          args.prune = False
    if not hasattr(args, "apply"):          args.apply = False
```

- [ ] **Step 5: Add the deduplicator plist to `cmd_install_daemons()`**

Find `cmd_install_daemons` (around line 316). The current `plists` list is:

```python
    plists = [
        "com.blue-pearmain.poller.plist",
        "com.blue-pearmain.pipeline.plist",
        "com.blue-pearmain.reviewer.plist",
        "com.blue-pearmain.reconcile.plist",
    ]
```

Change it to:

```python
    plists = [
        "com.blue-pearmain.poller.plist",
        "com.blue-pearmain.pipeline.plist",
        "com.blue-pearmain.reviewer.plist",
        "com.blue-pearmain.reconcile.plist",
        "com.blue-pearmain.deduplicator.plist",
    ]
```

- [ ] **Step 6: Run the tests to confirm they now pass**

```bash
python -m pytest tests/test_core.py::TestInstallDaemons -v
```

Expected: all tests in `TestInstallDaemons` PASS.

Then run the full suite:

```bash
python -m pytest tests/ -q
```

Expected: all tests pass, no regressions.

- [ ] **Step 7: Run lint**

```bash
make lint
```

Expected: no new type errors. The new `--prune`/`--apply` args in the subparser and `cmd_deduplicator` are `bool`, consistent with all other flag args in the function.

- [ ] **Step 8: Update the GH issue label and commit**

```bash
gh issue edit 147 --add-label "has-plan"
```

```bash
git add config/com.blue-pearmain.deduplicator.plist bp tests/test_core.py
git commit -m "feat(#147): weekly deduplicator launchd plist

- config/com.blue-pearmain.deduplicator.plist: runs deduplicator
  --write then --prune --apply every Sunday at 3am via launchd
- bp: adds --prune/--apply to deduplicator subcommand; wires new
  plist into install-daemons
- tests: 2 new TestInstallDaemons tests; plist count 4→5

Closes #147

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 9: Post retrospective on GH issue and push**

Close GH #147 with a retrospective comment:

```
Size estimate: S ✓

Files changed: 3 (config/com.blue-pearmain.deduplicator.plist, bp, tests/test_core.py)
Lines: ~70 added (plist: ~50, bp: ~10, tests: ~50, deltas net of context)
Plan tasks: 2

No scope changes. Straightforward — new plist + 3 small bp edits + 2 tests.
```

Then push:

```bash
git push origin main
```
