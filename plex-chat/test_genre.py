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
        {"title": "Side Comedy", "year": 2026, "rating": 8.4, "votes": 80000, "genres": ("Sci-Fi", "Comedy"), "kind": "movie"},
    ]
    picked = app.rank_genre_rows(rows, "Comedy", "movie", {app._title_key("Owned")}, 3)
    assert [row["title"] for row in picked] == ["Newer Better", "New Good", "Old Hit"]


def test_secondary_genre_fills_only_when_primaries_run_out():
    rows = [
        {"title": "Only Primary", "year": 1990, "rating": 7.1, "votes": 30000, "genres": ("Comedy",), "kind": "movie"},
        {"title": "Newer Side", "year": 2024, "rating": 8.0, "votes": 40000, "genres": ("Drama", "Comedy"), "kind": "movie"},
    ]
    picked = app.rank_genre_rows(rows, "Comedy", "movie", set(), 3)
    assert [row["title"] for row in picked] == ["Only Primary", "Newer Side"]


def test_extract_year_keeps_1942():
    assert app.extract_year("Casablanca (1942)") == 1942
    assert app.years_in("Casablanca 1942") == [1942]
    assert app.extract_year("Roundhay 1888") is None
    assert app.year_delta("Casablanca (1942)", 1942) == 0


def test_searches_keep_earlier_picks():
    app.RESULTS.clear()

    def hit(name: str, digest: str) -> dict:
        return {
            "fileName": name,
            "fileUrl": f"magnet:?xt=urn:btih:{digest}",
            "nbSeeders": 4,
            "nbLeechers": 1,
            "fileSize": 1000,
        }

    first = app.top_unique([hit("Alpha", "a" * 40)], "movies")
    second = app.top_unique([hit("Beta", "b" * 40)], "movies")
    assert first[0]["id"] in app.RESULTS
    assert second[0]["id"] in app.RESULTS
    assert "url" not in first[0]
    assert "saved_at" not in first[0]
    app.RESULTS[first[0]["id"]]["saved_at"] = 0
    app._prune_results(app.time.time())
    assert first[0]["id"] not in app.RESULTS
    assert second[0]["id"] in app.RESULTS


def test_find_torrent_ignores_latest_row():
    original = app.qbit

    def fake_qbit(*_a, **_k):
        rows = [
            {"hash": "1111111111111111111111111111111111111111", "name": "Other"},
            {"hash": "2222222222222222222222222222222222222222", "name": "Latest"},
        ]
        return 200, __import__("json").dumps(rows)

    app.qbit = fake_qbit
    try:
        found = app._find_torrent("Wanted", "magnet:?xt=urn:btih:" + "ab" * 20)
    finally:
        app.qbit = original
    assert found is None


def test_await_torrent_waits_for_the_added_hash():
    original = app.qbit
    sleeps = []
    calls = {"n": 0}
    wanted = "ab" * 20

    def fake_qbit(*_a, **_k):
        calls["n"] += 1
        rows = [{"hash": "ff" * 20, "name": "Latest"}]
        if calls["n"] >= 2:
            rows.append({"hash": wanted, "name": "Wanted"})
        return 200, __import__("json").dumps(rows)

    def fake_sleep(seconds):
        sleeps.append(seconds)

    app.qbit = fake_qbit
    original_sleep = app.time.sleep
    app.time.sleep = fake_sleep
    try:
        found = app._await_torrent("Wanted", f"magnet:?xt=urn:btih:{wanted}", tries=4, pause=0.4)
    finally:
        app.qbit = original
        app.time.sleep = original_sleep
    assert found["name"] == "Wanted"
    assert sleeps == [0.4]


def test_health_requires_password():
    import asyncio
    import base64

    app.CHAT_USER = "user"
    app.CHAT_PASS = "secret"

    class Req:
        def __init__(self, path, header=""):
            self.url = type("URL", (), {"path": path})()
            self.headers = {"authorization": header} if header else {}

    called = {"n": 0}

    async def call_next(_request):
        called["n"] += 1
        return "ok"

    denied = asyncio.run(app.require_basic_auth(Req("/api/health"), call_next))
    assert called["n"] == 0
    assert denied is not None and denied != "ok"
    token = base64.b64encode(b"user:secret").decode()
    allowed = asyncio.run(
        app.require_basic_auth(Req("/api/health", f"Basic {token}"), call_next)
    )
    assert called["n"] == 1
    assert allowed == "ok"


def test_latest_daily_report():
    import tempfile

    original = app.IMDB_DATA_DIR
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        app.IMDB_DATA_DIR = root
        try:
            assert app.latest_daily_report() is None
            (root / "state.json").write_text(
                '{"days":{"2026-09-27":[{"title":"Rocky","year":1976,"status":"queued"},'
                '{"title":"Missing","year":1942,"status":"miss"}],'
                '"2026-09-28":[{"title":"Newer","year":2024,"status":"already"}]}}'
            )
            report = app.latest_daily_report()
        finally:
            app.IMDB_DATA_DIR = original
    assert report["day"] == "2026-09-28"
    assert report["picks"] == [{"title": "Newer", "year": 2024, "status": "already"}]


if __name__ == "__main__":
    test_parse_genre_request()
    test_rank_prefers_newer_over_7()
    test_secondary_genre_fills_only_when_primaries_run_out()
    test_extract_year_keeps_1942()
    test_searches_keep_earlier_picks()
    test_find_torrent_ignores_latest_row()
    test_await_torrent_waits_for_the_added_hash()
    test_health_requires_password()
    test_latest_daily_report()
    print("ok")
