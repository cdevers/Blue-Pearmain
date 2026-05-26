"""Unit tests for db/photo_filters.py — pure photo filter module."""

from db.photo_filters import (
    build_text_clause,
    build_location_clause,
    build_person_clause,
    build_date_alias_clause,
)


class TestBuildTextClause:
    def test_seven_params_all_equal_to_term(self):
        sql, params = build_text_clause("sunset")
        assert len(params) == 7
        assert all(p == "%sunset%" for p in params)

    def test_sql_covers_all_seven_fields(self):
        sql, _ = build_text_clause("x")
        assert "photos_title" in sql
        assert "flickr_title" in sql
        assert "photos_description" in sql
        assert "flickr_description" in sql
        assert "apple_ai_caption" in sql
        assert "flickr_tags" in sql
        assert "photos_tags" in sql

    def test_tag_fields_use_json_each(self):
        sql, _ = build_text_clause("x")
        assert "json_each" in sql
        assert "EXISTS" in sql

    def test_term_wrapped_with_percent(self):
        _, params = build_text_clause("birthday")
        assert params[0] == "%birthday%"

    def test_empty_string_still_returns_fragment(self):
        sql, params = build_text_clause("")
        assert "%" in params[0]


class TestBuildLocationClause:
    def test_all_four_levels(self):
        sql, params = build_location_clause("United States", "MA", "Boston", "Back Bay")
        assert sql.count("= ?") == 4
        assert params == ["United States", "MA", "Boston", "Back Bay"]

    def test_three_levels_no_neighborhood(self):
        sql, params = build_location_clause("United States", "MA", "Springfield", None)
        assert "neighborhood" not in sql
        assert params == ["United States", "MA", "Springfield"]

    def test_country_only(self):
        sql, params = build_location_clause("France", None, None, None)
        assert sql == "p.place_country = ?"
        assert params == ["France"]

    def test_all_none_returns_noop(self):
        sql, params = build_location_clause(None, None, None, None)
        assert sql == "1=1"
        assert params == []

    def test_clauses_and_combined(self):
        sql, _ = build_location_clause("United States", "MA", None, None)
        assert " AND " in sql

    def test_neighborhood_without_city(self):
        sql, params = build_location_clause(None, None, None, "Union Square")
        assert "place_neighborhood" in sql
        assert params == ["Union Square"]


class TestBuildPersonClause:
    def test_json_each_exists_fragment(self):
        sql, params = build_person_clause("Alice")
        assert "json_each" in sql
        assert "EXISTS" in sql
        assert params == ["Alice"]

    def test_exact_match_not_like(self):
        sql, _ = build_person_clause("Alice")
        assert "LIKE" not in sql
        assert "value = ?" in sql

    def test_underscore_unknown_works_as_value(self):
        sql, params = build_person_clause("_UNKNOWN_")
        assert params == ["_UNKNOWN_"]


class TestBuildDateAliasClause:
    def test_date_function_exact_match(self):
        sql, params = build_date_alias_clause("2023-10-15")
        assert sql == "DATE(p.date_taken) = ?"
        assert params == ["2023-10-15"]
