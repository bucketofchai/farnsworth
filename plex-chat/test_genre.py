#!/usr/bin/env python3
"""Genre asks return newer IMDb titles rated over 7, not a title search."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_imdb_season  # noqa: F401  (stubs fastapi, imports app)
import app


def test_parse_genre_request():
    assert app.parse_genre_request("Find me a comedy") == {"genre": "Comedy", "kind": "movie"}
    assert app.parse_genre_request("recommend a sci-fi show") == {"genre": "Sci-Fi", "kind": "tvSeries"}
    assert app.parse_genre_request("I want a horror movie") == {"genre": "Horror", "kind": "movie"}
    assert app.parse_genre_request("download inception") is None
    assert app.parse_genre_request("Find me Anchorman") is None


def test_rank_prefers_newer_over_7():
    rows = [
        {"title": "Old Hit", "year": 1999, "rating": 8.8, "votes": 50000, "genres": ("Comedy",), "kind": "movie"},
        {"title": "New Good", "year": 2024, "rating": 7.2, "votes": 30000, "genres": ("Comedy", "Drama"), "kind": "movie"},
        {"title": "Newer Better", "year": 2025, "rating": 7.4, "votes": 40000, "genres": ("Comedy",), "kind": "movie"},
        {"title": "Barely", "year": 2026, "rating": 7.0, "votes": 9000, "genres": ("Comedy",), "kind": "movie"},
        {"title": "Few Votes", "year": 2026, "rating": 8.0, "votes": 50, "genres": ("Comedy",), "kind": "movie"},
        {"title": "Owned", "year": 2026, "rating": 8.1, "votes": 9000, "genres": ("Comedy",), "kind": "movie"},
        {"title": "Drama Only", "year": 2026, "rating": 8.5, "votes": 9000, "genres": ("Drama",), "kind": "movie"},
    ]
    picked = app.rank_genre_rows(rows, "Comedy", "movie", {app._title_key("Owned")}, 3)
    assert [row["title"] for row in picked] == ["Newer Better", "New Good", "Old Hit"]


if __name__ == "__main__":
    test_parse_genre_request()
    test_rank_prefers_newer_over_7()
    print("ok")
