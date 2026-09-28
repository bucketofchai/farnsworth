#!/usr/bin/env python3
"""Focused tests: IMDb must not clobber season searches."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _stub_web_imports() -> None:
    if "fastapi" in sys.modules:
        return
    fastapi = types.ModuleType("fastapi")

    class FastAPI:
        def __init__(self, **_kw):
            pass

        def mount(self, *_a, **_k):
            pass

        def middleware(self, *_a, **_k):
            return lambda fn: fn

        def on_event(self, *_a, **_k):
            return lambda fn: fn

        def get(self, *_a, **_k):
            return lambda fn: fn

        def post(self, *_a, **_k):
            return lambda fn: fn

    class HTTPException(Exception):
        def __init__(self, status_code=0, detail=""):
            self.status_code = status_code
            self.detail = detail

    fastapi.FastAPI = FastAPI
    fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi

    responses = types.ModuleType("fastapi.responses")

    class FileResponse:
        def __init__(self, *_a, **_k):
            pass

    class Response:
        def __init__(self, *_a, **_k):
            pass

    responses.FileResponse = FileResponse
    responses.Response = Response
    sys.modules["fastapi.responses"] = responses

    staticfiles = types.ModuleType("fastapi.staticfiles")

    class StaticFiles:
        def __init__(self, *_a, **_k):
            pass

    staticfiles.StaticFiles = StaticFiles
    sys.modules["fastapi.staticfiles"] = staticfiles

    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        pass

    def Field(*_a, **_k):
        return None

    pydantic.BaseModel = BaseModel
    pydantic.Field = Field
    sys.modules["pydantic"] = pydantic


_stub_web_imports()
import app

MEMO = "Criminal Minds: Season 10 - Memo from the Acting Director"
MEMO_ID = "tt5178864"

EXTRAS_ONLY = [
    {
        "title": MEMO,
        "year": 2015,
        "id": MEMO_ID,
        "qid": "video",
        "rank": 100,
    },
    {
        "title": "Criminal Minds: Season 13 - The Evolution of TV's Most Popular Show",
        "year": 2018,
        "id": "tt1234567",
        "qid": "short",
        "rank": 200,
    },
]

SERIES_ROWS = [
    {
        "title": "Criminal Minds",
        "year": 2005,
        "id": "tt0452046",
        "qid": "tvSeries",
        "rank": 1,
    },
    {
        "title": "Criminal Minds: Evolution",
        "year": 2022,
        "id": "tt18459620",
        "qid": "tvSeries",
        "rank": 50,
    },
    *EXTRAS_ONLY,
]


def _plan(user: str, llm: str | None = None, category: str = "tv", fetch=None):
    app._IMDB_CACHE.clear()
    seen: list[str] = []
    original = app._imdb_fetch

    def fake(title: str):
        seen.append(title)
        return list(fetch if fetch is not None else SERIES_ROWS)

    app._imdb_fetch = fake
    try:
        return app.plan_search(user, llm or user, category), seen
    finally:
        app._imdb_fetch = original


def test_season_17_extras_only_does_not_clobber():
    plan, seen = _plan(
        "criminal minds season 17 complete",
        "criminal minds season 17",
        fetch=EXTRAS_ONLY,
    )
    assert seen == ["criminal minds"], seen
    assert app._imdb_slug(seen[0]) == "criminal_minds"
    imdb = plan["imdb"]
    assert imdb is None or imdb.get("id") != MEMO_ID
    blob = f"{plan['display']} {plan['title']} {plan['show']}".lower()
    assert "memo" not in blob
    assert MEMO_ID not in blob
    assert plan["year"] is None
    joined = " ".join(plan["patterns"])
    assert "17" in joined or "S17" in joined
    assert any("Evolution S02" in p for p in plan["patterns"])


def test_season_17_series_hit_keeps_season_not_year():
    plan, seen = _plan(
        "criminal minds season 17 complete",
        "criminal minds season 17",
        fetch=SERIES_ROWS,
    )
    assert seen == ["criminal minds"]
    assert plan["imdb"]["id"] == "tt0452046"
    assert plan["imdb"]["title"] == "Criminal Minds"
    assert plan["show"] == "Criminal Minds"
    assert plan["year"] is None
    assert "2005" not in plan["display"]
    assert "season 17" in plan["display"].lower()
    joined = " ".join(plan["patterns"]).lower()
    assert "17" in joined
    assert "s17" in joined
    assert any("Evolution S02" in p for p in plan["patterns"])


def test_season_14_same_extra_bug():
    plan, _ = _plan(
        "criminal minds season 14 complete",
        "criminal minds season 14",
        fetch=EXTRAS_ONLY,
    )
    blob = f"{plan['display']} {plan['title']}".lower()
    assert "memo" not in blob
    assert plan["year"] is None
    joined = " ".join(plan["patterns"])
    assert "14" in joined or "S14" in joined
    assert not any("Evolution" in p for p in plan["patterns"])


def test_title_matches_rejects_wrong_season():
    extra = MEMO
    assert app.title_matches(extra, "criminal minds season 17") is False
    assert app.title_matches("Criminal Minds S10 Complete", "Criminal Minds", season=17) is False
    assert app.title_matches("Criminal Minds S17 Complete", "Criminal Minds", season=17) is True
    assert app.title_matches("Criminal Minds Evolution S02 1080p", "Criminal Minds season 17") is True


def test_s16_plus_adds_evolution():
    assert any("Evolution S01" in p for p in app.search_patterns("Criminal Minds", 16))
    assert any("Evolution S02" in p for p in app.search_patterns("Criminal Minds", 17))
    assert not any("Evolution" in p for p in app.search_patterns("Criminal Minds", 14))
    assert not any("Evolution" in p for p in app.search_patterns("Other Show", 17))


def test_parse_strips_complete_and_season():
    show, season, year = app.parse_search_query(
        "criminal minds season 17 complete", "criminal minds season 17"
    )
    assert show == "criminal minds"
    assert season == 17
    assert year is None


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"ok  {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    if failed:
        sys.exit(1)
    print(f"{len(tests)} passed")
