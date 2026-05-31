"""Pure normalization helpers for the legacy library indexer (#162).

No external dependencies; safe to import from indexer, matcher, and tests.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

HEAD_HASH_BYTES = 16 * 1024 * 1024  # 16 MiB of raw file bytes
_DUP_SLASH = re.compile(r"/{2,}")


def normalize_json_list(values: list[str] | None) -> str:
    """JSON array, unique + alphabetically sorted, blanks stripped.

    Deterministic: reordered or duplicated input yields identical output, so
    re-indexing never produces a noisy row update.
    """
    if not values:
        return "[]"
    cleaned = sorted({str(v).strip() for v in values if str(v).strip()})
    return json.dumps(cleaned, ensure_ascii=False)


def canonical_rel_path(path: str | None) -> str | None:
    """Canonical bundle-relative path. Fixed transform order (spec review pt 5):
    POSIX separators -> collapse duplicate slashes -> strip leading './' ->
    strip trailing '/' -> preserve case -> NFC.
    """
    if path is None:
        return None
    s = path.replace("\\", "/")  # 1. separators -> POSIX
    s = _DUP_SLASH.sub("/", s)  # 2. collapse dup slashes (after step 1)
    if s.startswith("./"):  # 3. strip leading ./
        s = s[2:]
    if len(s) > 1 and s.endswith("/"):  # 4. strip trailing slash
        s = s[:-1]
    s = unicodedata.normalize("NFC", s)  # 6. NFC (case preserved throughout)
    return s


def normalize_title(title: str | None) -> str | None:
    """Trim, casefold, NFC. Empty-after-trim counts as missing -> None."""
    if title is None:
        return None
    s = unicodedata.normalize("NFC", title).strip()
    if not s:
        return None
    return s.casefold()


def thumbnail_cache_key(library_uuid: str, asset_uuid: str) -> str:
    """Stable, path-independent 32-char hex key of the asset identity."""
    digest = hashlib.sha256(f"{library_uuid}:{asset_uuid}".encode()).hexdigest()
    return digest[:32]


def thumbnail_path(thumb_root: Path | str, library_uuid: str, cache_key: str) -> Path:
    """Absolute thumbnail path, resolved at read time against the cache root."""
    return Path(thumb_root) / "legacy" / library_uuid / f"{cache_key}.jpg"


def head_hash(path: str, n: int = HEAD_HASH_BYTES) -> str:
    """SHA256 of the first n raw file bytes (not SQLite pages)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(n))
    return h.hexdigest()
