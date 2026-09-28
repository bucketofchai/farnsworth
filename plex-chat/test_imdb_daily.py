#!/usr/bin/env python3
"""IMDb daily pick: chart parse, ranking, and one-pick-per-day selection."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import imdb_daily


def test_parse_chart_html():
    html = """
    {"id":"tt0111161","titleText":{"text":"The Shawshank Redemption"},"releaseYear":{"year":1994}}
    {"id":"tt0068646","titleText":{"text":"The Godfather"},"releaseYear":{"year":1972}}
    """
    rows = imdb_daily.parse_chart_html(html)
    assert [row["title"] for row in rows] == ["The Shawshank Redemption", "The Godfather"]
    assert rows[0]["rank"] == 1
    assert rows[1]["year"] == 1972


def test_top250_skips_docs_shorts_and_low_votes():
    basics = [
        {"tconst": "tt1", "titleType": "movie", "primaryTitle": "Classic", "startYear": "1994", "runtimeMinutes": "142", "genres": "Drama", "isAdult": "0"},
        {"tconst": "tt2", "titleType": "movie", "primaryTitle": "Doc", "startYear": "2010", "runtimeMinutes": "90", "genres": "Documentary", "isAdult": "0"},
        {"tconst": "tt3", "titleType": "short", "primaryTitle": "Short", "startYear": "1920", "runtimeMinutes": "10", "genres": "Comedy", "isAdult": "0"},
        {"tconst": "tt4", "titleType": "movie", "primaryTitle": "Other", "startYear": "1972", "runtimeMinutes": "175", "genres": "Crime", "isAdult": "0"},
        {"tconst": "tt5", "titleType": "movie", "primaryTitle": "Tiny", "startYear": "2000", "runtimeMinutes": "20", "genres": "Drama", "isAdult": "0"},
    ]
    ratings = [
        {"tconst": "tt1", "averageRating": "9.3", "numVotes": "2500000"},
        {"tconst": "tt2", "averageRating": "9.9", "numVotes": "900000"},
        {"tconst": "tt3", "averageRating": "9.9", "numVotes": "900000"},
        {"tconst": "tt4", "averageRating": "9.2", "numVotes": "1800000"},
        {"tconst": "tt5", "averageRating": "9.9", "numVotes": "900000"},
        {"tconst": "tt1", "averageRating": "9.3", "numVotes": "100"},
    ]
    # duplicate low-vote row must not displace the real candidate
    rows = imdb_daily.top250_from_tables(basics, ratings)
    assert [row["id"] for row in rows] == ["tt1", "tt4"]
    assert rows[0]["title"] == "Classic"


def test_choose_is_stable_and_skips_owned_and_queued():
    chart = [
        {"id": f"tt{i}", "title": f"Movie {i}", "year": 2000 + i, "rank": i}
        for i in range(1, 11)
    ]
    owned = {imdb_daily._title_key("Movie 1")}
    queued = {"tt2"}
    first = imdb_daily.choose_daily(chart, owned, queued, "2026-09-27", 3)
    second = imdb_daily.choose_daily(chart, owned, queued, "2026-09-27", 3)
    other = imdb_daily.choose_daily(chart, owned, queued, "2026-09-28", 3)
    assert first == second
    assert len(first) == 3
    assert {row["id"] for row in first}.isdisjoint({"tt1", "tt2"})
    assert first != other


def test_run_window_is_midnight_to_6am():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    midnight = datetime(2026, 9, 28, 0, 0, tzinfo=tz)
    just_before_six = datetime(2026, 9, 28, 5, 59, tzinfo=tz)
    six = datetime(2026, 9, 28, 6, 0, tzinfo=tz)
    evening = datetime(2026, 9, 27, 19, 59, tzinfo=tz)
    assert imdb_daily.in_run_window(midnight)
    assert imdb_daily.in_run_window(just_before_six)
    assert not imdb_daily.in_run_window(six)
    assert not imdb_daily.in_run_window(evening)
    assert imdb_daily.seconds_until_window(evening) == 4 * 3600 + 60
    assert imdb_daily.seconds_until_window(midnight) == 0
    assert imdb_daily.seconds_until_next_midnight(just_before_six) > 18 * 3600


def test_filename_year():
    assert imdb_daily.filename_has_year("Casablanca (1942) [1080p]", 1942)
    assert not imdb_daily.filename_has_year("Casablanca (1942) [1080p]", 1943)
    assert not imdb_daily.filename_has_year("Movie 1080p", 1080)


if __name__ == "__main__":
    test_parse_chart_html()
    test_top250_skips_docs_shorts_and_low_votes()
    test_choose_is_stable_and_skips_owned_and_queued()
    test_run_window_is_midnight_to_6am()
    test_filename_year()
    print("ok")
