# `bp migrate` Command Design — Issue #122

**Date:** 2026-05-22
**Issue:** https://github.com/cdevers/Blue-Pearmain/issues/122

---

## Goal

Add `bp migrate` — a command that auto-discovers and applies all pending DB migrations in order, using the `schema_migrations` table to skip already-applied ones. Update `bp doctor` to hint at `bp migrate` instead of listing individual `python db/migrations/…` run commands.

---

## Background

`bp doctor` already (section 5) checks whether all migration files have been applied. It scans `db/migrations/migrate_*.py` for `MIGRATION_NAME` constants, compares against the `schema_migrations` table, and warns about any gaps — printing a manual `python db/migrations/X.py --config Y` command for each. All migrations are already idempotent.

What's missing is a single command to fix the gap.

---

## Architecture

### Shared Helper: `_pending_migrations`

```python
def _pending_migrations(
    db_path: str,
    migrations_dir: Path,
) -> list[tuple[str, Path]]:
    """
    Return list of (migration_name, file_path) for migrations not yet applied,
    sorted by filename (which sorts numerically by the NNN prefix).
    """
```

- Opens the DB, reads `schema_migrations` for applied names.
- Scans `migrations_dir.glob("migrate_*.py")` for `MIGRATION_NAME` constants (same regex already used in `cmd_doctor`).
- Returns unapplied entries, sorted by `file_path.name`.
- Raises `sqlite3.OperationalError` if `schema_migrations` table doesn't exist (fresh DB, no migrations run yet) — callers should catch and treat as "all pending".

Both `cmd_doctor` and `cmd_migrate` call this helper.

### `cmd_migrate`

```
bp migrate [--dry-run]
```

1. Load config from `args.config`; resolve `db_path`.
2. Resolve `migrations_dir = ROOT / "db" / "migrations"`.
3. Call `_pending_migrations(db_path, migrations_dir)`.
4. If list is empty: print `"All migrations already applied."`, exit 0.
5. For each `(name, file_path)` in order:
   - Print `"Applying: <name>…"` (or `"[dry-run] Would apply: <name>…"` if dry-run).
   - Load the module via `importlib.util.spec_from_file_location`.
   - Call `module.run(db_path, dry_run=args.dry_run)`.
6. Print `"N migration(s) applied."` (or `"N migration(s) would be applied."` for dry-run).

Errors from individual migrations propagate — if one fails, the run stops (the migration's own idempotency guard means re-running is safe).

### `cmd_doctor` update (section 5)

Replace the per-file hint:

```
Run: python db/migrations/migrate_XXX.py --config config/config.yml
```

With a single unified hint:

```
Run: bp migrate
```

The helper is called the same way; only the output message changes.

---

## File Changes

| File | Change |
|------|--------|
| `bp` | Add `_pending_migrations()` helper; add `cmd_migrate()`; update `cmd_doctor` section 5 hint; register `migrate` subparser and dispatch entry |

No other files change — no new modules, no DB schema changes.

---

## CLI Interface

```
bp migrate              Apply all pending DB migrations in order
bp migrate --dry-run    Show what would be applied without changing the DB
bp doctor               (unchanged) Warns about unapplied migrations; now hints "Run: bp migrate"
```

---

## Argparse Entry

```python
p_mig = sub.add_parser(
    "migrate",
    help="Apply all pending DB migrations in order (safe to re-run; migrations are idempotent).",
)
p_mig.add_argument("--dry-run", action="store_true", help="Show what would run without applying")
```

Usage line added to the module docstring at the top of `bp`.

---

## Tests (`tests/test_migrate_cmd.py`)

All tests use a temporary SQLite DB (no config file needed — pass `db_path` directly).

- **`test_no_pending_migrations`**: Seed `schema_migrations` with all migration names found on disk → `_pending_migrations()` returns empty list.
- **`test_pending_migration_helper`**: Remove one name from `schema_migrations` → helper returns that one entry.
- **`test_cmd_migrate_applies_pending`**: Create a temp migration file with a `run()` stub; call `cmd_migrate` with it pending → stub is called, name recorded in `schema_migrations`.
- **`test_cmd_migrate_dry_run`**: Same setup; `--dry-run` → stub called with `dry_run=True`, `schema_migrations` unchanged.
- **`test_cmd_migrate_already_applied`**: All applied → exits 0, no stubs called.
- **`test_doctor_hint_says_bp_migrate`**: Run `cmd_doctor` with a pending migration and capture stdout → output contains `"bp migrate"`, not `"python db/migrations"`.
