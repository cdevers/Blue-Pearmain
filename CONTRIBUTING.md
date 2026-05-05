# Contributing to Blue Pearmain

## Requirements

- macOS (the tool relies on Apple Photos via `photoscript`)
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management

## Development setup

```bash
git clone https://github.com/cdevers/Blue-Pearmain.git
cd Blue-Pearmain
uv sync --extra dev
cp config/config.example.yml config/config.yml
# Edit config/config.yml — fill in your Flickr credentials and paths
```

## Running tests

```bash
python -m pytest tests/ -q
```

The test suite does not touch the live database or Apple Photos. All tests run against in-memory or temporary SQLite databases with mocked external calls.

**Note:** A handful of tests that exercise `photoscript` directly require macOS Full Disk Access for the Terminal process. If you see permission errors, grant Full Disk Access in System Settings → Privacy & Security.

## Commit style

Match the style of recent commits in the repository: imperative mood, short subject line, reference any relevant GitHub issue (`Closes #N` or `Refs #N`). Add a co-author line if you used AI assistance:

```
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

## Before opening a pull request

1. Run `python -m pytest tests/ -q` and confirm all tests pass.
2. Update `README.md` if your change affects user-visible behaviour or the test count.
3. Run `ruff check .` (install via `uv sync --extra dev`) and fix any lint errors.

## Reporting issues

Open an issue on GitHub. Include the Blue Pearmain version (`bp --version` or `git log -1 --oneline`), macOS version, and steps to reproduce.
