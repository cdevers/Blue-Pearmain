"""
Temporal pattern filter — pure functions returning SQLite WHERE clause fragments.
No Flask or DB dependencies.

Usage:
    frag, params = parse_pattern("month:10", expand_days=0, years=[])
    frag, params = parse_pattern("holiday:thanksgiving", expand_days=2, years=[2022, 2023])

All generated fragments reference the column alias p.date_taken (photos p).
Unknown or empty patterns return ("1=1", []) — a safe no-op clause.
"""

from __future__ import annotations

import datetime
from typing import Optional

# Fuzzy, overlapping season ranges — intentionally human-oriented.
# A photo from March is plausibly "winter" or "spring" depending on the person's
# frame of reference, so both seasons include it.
SEASONS: dict[str, list[str]] = {
    "spring": ["03", "04", "05", "06"],  # Mar–Jun
    "summer": ["06", "07", "08", "09"],  # Jun–Sep
    "fall": ["09", "10", "11", "12"],  # Sep–Dec
    "winter": ["12", "01", "02", "03"],  # Dec–Mar
}

# Holiday definitions.
# ("fixed",       month, day)
# ("nth_weekday", month, weekday, n)
#   weekday: Mon=0 … Sun=6  (Python datetime.weekday() convention)
#   n: positive = nth from start, -1 = last
HOLIDAYS: dict[str, tuple] = {
    "new_years": ("fixed", 1, 1),
    "mlk_day": ("nth_weekday", 1, 0, 3),  # 3rd Mon Jan
    "presidents_day": ("nth_weekday", 2, 0, 3),  # 3rd Mon Feb
    "memorial_day": ("nth_weekday", 5, 0, -1),  # last Mon May
    "july_4th": ("fixed", 7, 4),
    "labor_day": ("nth_weekday", 9, 0, 1),  # 1st Mon Sep
    "columbus_day": ("nth_weekday", 10, 0, 2),  # 2nd Mon Oct
    "halloween": ("fixed", 10, 31),
    "thanksgiving": ("nth_weekday", 11, 3, 4),  # 4th Thu Nov
    "christmas": ("fixed", 12, 25),
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """
    Return the nth occurrence of weekday in (year, month).
    weekday: Mon=0 … Sun=6 (Python datetime.weekday() convention).
    n=1 → first occurrence, n=-1 → last occurrence.
    """
    if n > 0:
        first = datetime.date(year, month, 1)
        days_ahead = (weekday - first.weekday()) % 7
        first_occurrence = first + datetime.timedelta(days=days_ahead)
        return first_occurrence + datetime.timedelta(weeks=n - 1)
    else:
        # last occurrence: start from the last day of the month and walk back
        if month == 12:
            last = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
        else:
            last = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
        days_back = (last.weekday() - weekday) % 7
        return last - datetime.timedelta(days=days_back)


def holiday_date(year: int, key: str) -> Optional[datetime.date]:
    """Return the date of the named holiday in the given year, or None if unknown."""
    defn = HOLIDAYS.get(key)
    if defn is None:
        return None
    if defn[0] == "fixed":
        _, month, day = defn
        return datetime.date(year, month, day)
    if defn[0] == "nth_weekday":
        _, month, weekday, n = defn
        return _nth_weekday(year, month, weekday, n)
    return None


def parse_pattern(
    pattern: str,
    expand_days: int,
    years: list[int],
) -> tuple[str, list]:
    """
    Return (sql_fragment, params) to append to a WHERE clause.
    The fragment references column alias 'p.date_taken'.
    Unknown or empty pattern returns ("1=1", []) — a safe no-op.

    Args:
        pattern:     Colon-separated type:key, e.g. "month:10", "holiday:thanksgiving".
        expand_days: For holiday patterns, expand the window by ±this many calendar days.
                     0 means exact day only. Fixed at 2 in v1 UI.
        years:       List of calendar years to compute holiday dates for. Obtained by
                     querying DISTINCT strftime('%Y', date_taken) from the photos table.
                     Ignored for non-holiday patterns.
    """
    if not pattern:
        return "1=1", []

    if pattern.startswith("month:"):
        month = pattern[6:]
        return "strftime('%m', p.date_taken) = ?", [month]

    if pattern.startswith("season:"):
        key = pattern[7:]
        months = SEASONS.get(key)
        if not months:
            return "1=1", []
        placeholders = ",".join("?" * len(months))
        return f"strftime('%m', p.date_taken) IN ({placeholders})", list(months)

    if pattern.startswith("daytype:"):
        key = pattern[8:]
        # SQLite strftime('%w', ...) → '0'=Sunday, '6'=Saturday
        if key == "weekend":
            return "strftime('%w', p.date_taken) IN (?,?)", ["0", "6"]
        if key == "weekday":
            return "strftime('%w', p.date_taken) NOT IN (?,?)", ["0", "6"]
        return "1=1", []

    if pattern.startswith("holiday:"):
        key = pattern[8:]
        if expand_days == 0:
            # Exact day: use strftime to strip time component from date_taken
            dates: list[str] = []
            for year in years:
                d = holiday_date(year, key)
                if d is None:
                    continue
                dates.append(str(d))
            if not dates:
                return "1=1", []
            placeholders = ",".join("?" * len(dates))
            return f"(strftime('%Y-%m-%d', p.date_taken) IN ({placeholders}))", dates
        else:
            # Date range with expansion: use BETWEEN with time-aware upper bound
            clauses_list: list[str] = []
            params: list = []
            for year in years:
                d = holiday_date(year, key)
                if d is None:
                    continue
                lo = str(d - datetime.timedelta(days=expand_days))
                hi = str(d + datetime.timedelta(days=expand_days)) + "T23:59:59"
                clauses_list.append("(p.date_taken BETWEEN ? AND ?)")
                params.extend([lo, hi])
            if not clauses_list:
                return "1=1", []
            return f"({' OR '.join(clauses_list)})", params

    return "1=1", []
