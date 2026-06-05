"""Date precision helpers for approximate / fuzzy dates (#157).

format_date_precision(date_taken, precision, approximate) → str

Used as a Jinja filter (date_display) in the reviewer UI and directly in tests.
"""

from __future__ import annotations

from datetime import datetime

PRECISION_VALUES = ("exact", "day", "month", "year", "decade", "unknown")

# When precision is 'month', 'year', or 'decade', the stored date_taken value
# contains a synthetic day/time component that is a placeholder, not historical
# truth (e.g. 1975-01-01 means "sometime in 1975", not "January 1st, 1975").
# Only the year (and month, for 'month' precision) should be displayed.


def format_date_precision(
    date_taken: str | None,
    precision: str | None,
    approximate: bool = False,
) -> str:
    """Return a human-readable date string respecting precision and approximate flag.

    Returns '' when date_taken is None (except for 'unknown' which also returns '').
    """
    p = precision if precision in PRECISION_VALUES else "exact"

    if p == "unknown":
        return ""

    if date_taken is None:
        return ""

    try:
        dt = datetime.fromisoformat(date_taken)
    except (ValueError, TypeError):
        return date_taken

    prefix = "c. " if approximate and p not in ("exact",) else ""

    if p == "exact":
        return dt.strftime("%Y-%m-%d %H:%M")
    if p == "day":
        return prefix + dt.strftime("%Y-%m-%d")
    if p == "month":
        return prefix + dt.strftime("%B %Y")
    if p == "year":
        return prefix + str(dt.year)
    if p == "decade":
        decade = (dt.year // 10) * 10
        return prefix + f"{decade}s"
    # Unreachable (PRECISION_VALUES guard above), but keeps mypy happy
    return dt.strftime("%Y-%m-%d %H:%M")
