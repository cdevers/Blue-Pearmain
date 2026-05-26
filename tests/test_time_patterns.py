"""Unit tests for db/time_patterns.py — pure temporal pattern module."""

import datetime

from db.time_patterns import _nth_weekday, holiday_date, parse_pattern


# ---------------------------------------------------------------------------
# _nth_weekday
# ---------------------------------------------------------------------------


class TestNthWeekday:
    def test_first_monday_september_2023(self):
        # Labor Day 2023: first Monday of September = Sep 4
        assert _nth_weekday(2023, 9, 0, 1) == datetime.date(2023, 9, 4)

    def test_last_monday_may_2023(self):
        # Memorial Day 2023: last Monday of May = May 29
        # May 1 2023 is a Monday — edge case worth exercising
        assert _nth_weekday(2023, 5, 0, -1) == datetime.date(2023, 5, 29)

    def test_fourth_thursday_november_2023(self):
        # Thanksgiving 2023 = Nov 23
        assert _nth_weekday(2023, 11, 3, 4) == datetime.date(2023, 11, 23)

    def test_third_monday_january_2023(self):
        # MLK Day 2023 = Jan 16
        assert _nth_weekday(2023, 1, 0, 3) == datetime.date(2023, 1, 16)

    def test_second_monday_october_2023(self):
        # Columbus Day 2023 = Oct 9
        assert _nth_weekday(2023, 10, 0, 2) == datetime.date(2023, 10, 9)


# ---------------------------------------------------------------------------
# holiday_date
# ---------------------------------------------------------------------------


class TestHolidayDate:
    def test_thanksgiving_2023(self):
        assert holiday_date(2023, "thanksgiving") == datetime.date(2023, 11, 23)

    def test_labor_day_2023(self):
        assert holiday_date(2023, "labor_day") == datetime.date(2023, 9, 4)

    def test_memorial_day_2023(self):
        assert holiday_date(2023, "memorial_day") == datetime.date(2023, 5, 29)

    def test_mlk_day_2023(self):
        assert holiday_date(2023, "mlk_day") == datetime.date(2023, 1, 16)

    def test_christmas_fixed(self):
        assert holiday_date(2023, "christmas") == datetime.date(2023, 12, 25)

    def test_new_years_fixed(self):
        assert holiday_date(2024, "new_years") == datetime.date(2024, 1, 1)

    def test_unknown_key_returns_none(self):
        assert holiday_date(2023, "easter") is None
        assert holiday_date(2023, "") is None


# ---------------------------------------------------------------------------
# parse_pattern
# ---------------------------------------------------------------------------


class TestParsePattern:
    # Month
    def test_month_october(self):
        sql, params = parse_pattern("month:10", 0, [])
        assert "strftime('%m'" in sql
        assert params == ["10"]

    def test_month_january_zero_padded(self):
        sql, params = parse_pattern("month:01", 0, [])
        assert params == ["01"]

    # Season
    def test_season_fall(self):
        sql, params = parse_pattern("season:fall", 0, [])
        assert set(params) == {"09", "10", "11", "12"}
        assert len(params) == 4

    def test_season_winter_includes_march(self):
        sql, params = parse_pattern("season:winter", 0, [])
        assert "03" in params  # overlaps with spring — intentional
        assert "12" in params

    def test_season_spring_includes_june(self):
        sql, params = parse_pattern("season:spring", 0, [])
        assert "06" in params  # overlaps with summer — intentional
        assert "03" in params

    def test_season_summer(self):
        sql, params = parse_pattern("season:summer", 0, [])
        assert set(params) == {"06", "07", "08", "09"}

    def test_season_unknown_key(self):
        sql, params = parse_pattern("season:monsoon", 0, [])
        assert sql == "1=1"
        assert params == []

    # Day type
    def test_daytype_weekend(self):
        sql, params = parse_pattern("daytype:weekend", 0, [])
        assert "NOT IN" not in sql
        assert set(params) == {"0", "6"}

    def test_daytype_weekday(self):
        sql, params = parse_pattern("daytype:weekday", 0, [])
        assert "NOT IN" in sql
        assert set(params) == {"0", "6"}

    # Holidays — no expansion
    def test_holiday_exact_thanksgiving_2023(self):
        sql, params = parse_pattern("holiday:thanksgiving", 0, [2023])
        assert "strftime('%Y-%m-%d', p.date_taken)" in sql
        assert "2023-11-23" in params
        assert "BETWEEN" not in sql

    def test_holiday_exact_two_years(self):
        sql, params = parse_pattern("holiday:thanksgiving", 0, [2022, 2023])
        assert "strftime('%Y-%m-%d', p.date_taken)" in sql
        assert "2022-11-24" in params  # Thanksgiving 2022 = Nov 24
        assert "2023-11-23" in params

    # Holidays — with expansion
    def test_holiday_expand_thanksgiving_2023(self):
        sql, params = parse_pattern("holiday:thanksgiving", 2, [2023])
        # ±2 from Nov 23 → lo=Nov 21, hi=Nov 25 23:59:59
        assert "2023-11-21" in params
        assert any("2023-11-25" in p for p in params)
        assert "BETWEEN" in sql

    def test_holiday_expand_two_years(self):
        sql, params = parse_pattern("holiday:thanksgiving", 2, [2022, 2023])
        assert sql.count("BETWEEN") == 2

    def test_holiday_unknown_key_empty_years(self):
        sql, params = parse_pattern("holiday:unknown_key", 0, [2023])
        assert sql == "1=1"
        assert params == []

    def test_holiday_no_years(self):
        sql, params = parse_pattern("holiday:thanksgiving", 2, [])
        assert sql == "1=1"
        assert params == []

    # Fallbacks
    def test_empty_pattern(self):
        sql, params = parse_pattern("", 0, [])
        assert sql == "1=1"
        assert params == []

    def test_unknown_prefix(self):
        sql, params = parse_pattern("unknown:xyz", 0, [2023])
        assert sql == "1=1"
        assert params == []
