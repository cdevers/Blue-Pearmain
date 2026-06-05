"""Tests for db.date_precision — format_date_precision helper."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.date_precision import PRECISION_VALUES, format_date_precision


class TestPrecisionValues:
    def test_all_six_values_present(self):
        assert set(PRECISION_VALUES) == {"exact", "day", "month", "year", "decade", "unknown"}


class TestFormatDatePrecision:
    def test_exact_shows_datetime(self):
        assert format_date_precision("2023-06-15T14:32:00", "exact") == "2023-06-15 14:32"

    def test_exact_is_default_when_precision_none(self):
        assert format_date_precision("2023-06-15T14:32:00", None) == "2023-06-15 14:32"

    def test_day_shows_date_only(self):
        assert format_date_precision("2023-06-15T14:32:00", "day") == "2023-06-15"

    def test_month_shows_month_year(self):
        assert format_date_precision("1975-06-01T00:00:00", "month") == "June 1975"

    def test_year_shows_year_only(self):
        assert format_date_precision("1975-01-01T00:00:00", "year") == "1975"

    def test_decade_shows_decade(self):
        assert format_date_precision("1975-01-01T00:00:00", "decade") == "1970s"

    def test_decade_uses_start_of_decade(self):
        assert format_date_precision("1979-12-31T00:00:00", "decade") == "1970s"

    def test_unknown_returns_empty_string(self):
        assert format_date_precision(None, "unknown") == ""

    def test_unknown_with_date_still_returns_empty(self):
        assert format_date_precision("1975-01-01T00:00:00", "unknown") == ""

    def test_approximate_prefix_for_day(self):
        assert (
            format_date_precision("2023-06-15T00:00:00", "day", approximate=True) == "c. 2023-06-15"
        )

    def test_approximate_prefix_for_month(self):
        assert (
            format_date_precision("1975-06-01T00:00:00", "month", approximate=True)
            == "c. June 1975"
        )

    def test_approximate_prefix_for_year(self):
        assert format_date_precision("1975-01-01T00:00:00", "year", approximate=True) == "c. 1975"

    def test_approximate_prefix_for_decade(self):
        assert (
            format_date_precision("1975-01-01T00:00:00", "decade", approximate=True) == "c. 1970s"
        )

    def test_approximate_ignored_for_exact(self):
        # "c." prefix doesn't make sense for exact timestamps
        result = format_date_precision("2023-06-15T14:32:00", "exact", approximate=True)
        assert result == "2023-06-15 14:32"

    def test_null_date_taken_with_exact_returns_empty(self):
        assert format_date_precision(None, "exact") == ""

    def test_null_date_taken_with_year_returns_empty(self):
        assert format_date_precision(None, "year") == ""

    def test_unknown_precision_value_falls_back_gracefully(self):
        # Any unrecognised precision string → fall back to exact display
        result = format_date_precision("2023-06-15T14:32:00", "quarterly")
        assert result == "2023-06-15 14:32"

    def test_unknown_round_trip_display_is_blank(self):
        # 'unknown' precision → blank display regardless of date_taken value
        assert format_date_precision("1975-01-01T00:00:00", "unknown") == ""
        assert format_date_precision(None, "unknown") == ""


class TestDateDisplayFilter:
    """Integration test: filter wired into Flask app."""

    def test_filter_registered(self):
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from reviewer.app import _date_display_filter

        result = _date_display_filter("1975-01-01T00:00:00", "year", 0)
        assert result == "1975"

    def test_filter_approximate_flag(self):
        from reviewer.app import _date_display_filter

        result = _date_display_filter("1975-01-01T00:00:00", "year", 1)
        assert result == "c. 1975"

    def test_filter_none_date(self):
        from reviewer.app import _date_display_filter

        result = _date_display_filter(None, "exact", 0)
        assert result == ""
