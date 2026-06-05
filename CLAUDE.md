# Blue Pearmain — Claude instructions

> **Note for contributors:** This file is a configuration file for [Claude Code](https://claude.ai/code) (Anthropic's AI coding assistant). It has no effect if you're not using Claude Code — you can safely ignore it.

These instructions apply to every session in this project. Follow them without being asked.

---

## Before starting any feature or bug fix

1. **Brainstorm** — invoke `/superpowers:brainstorming` before writing any code for a new feature or non-trivial change. Explore design options and agree on the approach with the user before touching files.
2. **File a GitHub issue** — once the approach is agreed, create or identify the issue. Do this before making any code changes.

---

## GitHub issue lifecycle

Every non-trivial piece of work must be tracked in a GitHub issue:

- **Before coding:** once we've agreed on the problem and approach, create or identify the issue. Do this before making any code changes.
- **After coding:** update the issue — either close it with a note summarising what was done, or leave a status comment if the work is only partially complete.

---

## After every round of changes

For each meaningful change (bug fix, feature, refactor):

1. **Tests (TDD)** — invoke `/superpowers:test-driven-development` before writing implementation code. Write the tests first, confirm they fail for the right reason, then implement. Run `python -m pytest tests/ -q` and confirm all tests pass before committing.
2. **README** — update `README.md` to reflect any user-visible change: new commands, changed behaviour. Do not update a specific test count — the README now has a general coverage statement instead.
3. **Docs** — if a `docs/` file describes work that was just completed, mark it done (e.g. `✓ done`) or update its status line.
4. **GitHub issue** — reference the issue in the commit message (e.g. `Closes #1`). After the commit, update the issue with a status comment or close it with a summary of what was done.
5. **Git commit** — create a commit that describes what changed and why, following the style of recent commits in this repo. Co-author line: `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`.

---

## Development environment

- **Database:** `data/curator.db` (SQLite).
- **Test runner:** `python -m pytest tests/ -q` from the repo root.
- **Dev server:** `python reviewer/app.py --config config/config.yml` or `bp ui`.
- **Python path:** scripts in `poller/` add both `Path(__file__).parent.parent` (project root) and `Path(__file__).parent` (the `poller/` directory itself) to `sys.path`. Sibling modules import as `from scanner import ...`, not `from poller.scanner import ...`.
- **Git commit email:** GitHub rejects pushes signed with `cdevers@pobox.com`. Use `1642218+cdevers@users.noreply.github.com` — verify with `git config user.email` before pushing.
- **Branch protection:** `main` requires a passing `test` CI check and a PR — no direct pushes, enforced for admins. All work goes on a feature branch; merge via PR only.

---

## macOS Full Disk Access bug

The macOS Terminal process sometimes loses Full Disk Access silently. Symptoms: permission errors reading files in the working directory, or `find` returning nothing. If this happens, stop and tell the user — they will fix it in System Settings and say "go" to resume.

---

## Known pre-existing issues

- `test_migration_002_idempotent` and `test_migration_table_exists_after_migrate_002` were removed (stale path bug; migration already applied to all installations).

---

## Project backlog

All open work is tracked in GitHub Issues: https://github.com/cdevers/Blue-Pearmain/issues

Design notes and specs live in `docs/`. Those files are implementation references, not task lists — the issues are the canonical source of what needs to be done.
