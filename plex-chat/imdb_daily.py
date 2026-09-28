#!/usr/bin/env python3
"""Once a day, pick 3 IMDb Top 250 movies that are not already on disk and queue them.

The chart page (https://www.imdb.com/chart/top/) is tried first. IMDb answers
bots with an AWS WAF challenge, so the fallback builds the same list from the
public datasets: feature films with at least 25,000 votes, ranked by IMDb's
weighted rating.
"""
from __future__ import annotations

import gzip
import json
import os
import random
import re
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CHART_URL = os.environ.get(
    "IMDB_CHART_URL", "https://www.imdb.com/chart/top/?ref_=hm_nv_menu"
)
BASICS_URL = "https://datasets.imdbws.com/title.basics.tsv.gz"
RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
MIN_VOTES = 25_000
MEAN_VOTE_FLOOR = 1_000
MIN_RUNTIME = 40
STATE_PATH = Path(os.environ.get("IMDB_DAILY_STATE", "/var/lib/imdb-daily/state.json"))
CACHE_DIR = Path(os.environ.get("IMDB_DAILY_CACHE", str(STATE_PATH.parent)))
COUNT = int(os.environ.get("IMDB_DAILY_COUNT", "3"))
TZ_NAME = os.environ.get("TZ", "America/New_York")

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
_NODE_RE = re.compile(
    r'"id":"(tt\d+)","titleText":\{"text":"((?:\\.|[^"\\])*)"\}'
    r',"releaseYear":\{"year":(\d{4})\}'
)
_HREF_RE = re.compile(
    r'href="/title/(tt\d+)/[^"]*"[^>]*>([^<]+)</a>(?:(?!</a>).){0,400}?((?:18|19|20)\d{2})',
    re.S,
)
_YEAR_RE = re.compile(r"(?:^|\D)((?:18|19|20)\d{2})(?:\D|$)")


def _unescape(text: str) -> str:
    return (
        text.replace("\\u0026", "&")
        .replace("\\/", "/")
        .replace('\\"', '"')
        .replace("\\n", " ")
        .strip()
    )


def parse_chart_html(html: str) -> list[dict]:
    rows = []
    seen = set()
    for match in _NODE_RE.finditer(html or ""):
        imdb_id, title, year = match.group(1), _unescape(match.group(2)), int(match.group(3))
        if imdb_id in seen or not title:
            continue
        seen.add(imdb_id)
        rows.append({"id": imdb_id, "title": title, "year": year, "rank": len(rows) + 1})
    if len(rows) >= 50:
        return rows[:250]
    for match in _HREF_RE.finditer(html or ""):
        imdb_id, title, year = match.group(1), _unescape(match.group(2)), int(match.group(3))
        if imdb_id in seen or not title:
            continue
        seen.add(imdb_id)
        rows.append({"id": imdb_id, "title": title, "year": year, "rank": len(rows) + 1})
    return rows[:250]


def weighted_rating(rating: float, votes: int, mean: float, minimum: int = MIN_VOTES) -> float:
    return (votes / (votes + minimum)) * rating + (minimum / (votes + minimum)) * mean


def top250_from_tables(basics: list[dict], ratings: list[dict]) -> list[dict]:
    movies = {}
    for row in basics:
        if row.get("titleType") != "movie" or str(row.get("isAdult")) != "0":
            continue
        genres = str(row.get("genres") or "")
        if "Documentary" in genres.split(","):
            continue
        runtime = str(row.get("runtimeMinutes") or "")
        if runtime.isdigit() and int(runtime) < MIN_RUNTIME:
            continue
        year = str(row.get("startYear") or "")
        if not year.isdigit():
            continue
        title = str(row.get("primaryTitle") or "").strip()
        imdb_id = str(row.get("tconst") or "")
        if title and imdb_id:
            movies[imdb_id] = (title, int(year))
    scored = []
    rating_sum = 0.0
    rating_n = 0
    pending = []
    for row in ratings:
        imdb_id = str(row.get("tconst") or "")
        if imdb_id not in movies:
            continue
        try:
            rating = float(row["averageRating"])
            votes = int(row["numVotes"])
        except (KeyError, TypeError, ValueError):
            continue
        if votes >= MEAN_VOTE_FLOOR:
            rating_sum += rating
            rating_n += 1
        if votes >= MIN_VOTES:
            pending.append((imdb_id, rating, votes))
    mean = (rating_sum / rating_n) if rating_n else 7.0
    for imdb_id, rating, votes in pending:
        title, year = movies[imdb_id]
        scored.append((weighted_rating(rating, votes, mean), imdb_id, title, year, rating, votes))
    scored.sort(key=lambda item: (-item[0], -item[5], item[2].lower()))
    return [
        {"id": imdb_id, "title": title, "year": year, "rank": index}
        for index, (_score, imdb_id, title, year, _rating, _votes) in enumerate(scored[:250], start=1)
    ]


def choose_daily(
    chart: list[dict],
    owned: set[str],
    queued: set[str],
    day: str,
    count: int = 3,
) -> list[dict]:
    pool = []
    for row in chart:
        if row["id"] in queued:
            continue
        if _title_key(row["title"]) in owned:
            continue
        pool.append(row)
    rng = random.Random(day)
    rng.shuffle(pool)
    return pool[: max(0, count)]


def _title_key(title: str) -> str:
    text = re.sub(r"\s*\(((?:18|19|20)\d{2})\)\s*$", "", title or "")
    text = re.sub(r"[._]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def owned_title_keys(movies: list[dict]) -> set[str]:
    return {_title_key(str(row.get("title") or "")) for row in movies if row.get("title")}


def filename_has_year(name: str, year: int) -> bool:
    return str(year) in _YEAR_RE.findall(name or "")


def _now() -> datetime:
    return datetime.now(ZoneInfo(TZ_NAME))


def _today() -> str:
    return _now().date().isoformat()


def in_run_window(now: datetime) -> bool:
    """Local midnight through 6am, not including 6:00."""
    return 0 <= now.hour < 6


def seconds_until_window(now: datetime) -> int:
    if in_run_window(now):
        return 0
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt = nxt + timedelta(days=1)
    return max(1, int((nxt - now).total_seconds()))


def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("queued_ids", [])
    data.setdefault("days", {})
    return data


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE_PATH)


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    tmp.replace(dest)


def _fetch_chart_page() -> list[dict]:
    req = urllib.request.Request(
        CHART_URL,
        headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        waf = resp.headers.get("x-amzn-waf-action") or ""
        html = resp.read().decode("utf-8", "replace")
    if waf:
        print(f"imdb chart blocked ({waf})", flush=True)
        return []
    rows = parse_chart_html(html)
    print(f"imdb chart page titles={len(rows)}", flush=True)
    return rows if len(rows) >= 200 else []


def _iter_tsv(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        header = handle.readline().rstrip("\n").split("\t")
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(header):
                continue
            yield dict(zip(header, parts))


def _chart_from_datasets() -> list[dict]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    basics_path = CACHE_DIR / "title.basics.tsv.gz"
    ratings_path = CACHE_DIR / "title.ratings.tsv.gz"
    print("downloading IMDb title datasets", flush=True)
    _download(BASICS_URL, basics_path)
    _download(RATINGS_URL, ratings_path)
    print("ranking feature films with at least 25000 votes", flush=True)
    basics = []
    for row in _iter_tsv(basics_path):
        if row.get("titleType") != "movie":
            continue
        basics.append(
            {
                "tconst": row.get("tconst"),
                "titleType": "movie",
                "primaryTitle": row.get("primaryTitle"),
                "startYear": row.get("startYear"),
                "runtimeMinutes": row.get("runtimeMinutes"),
                "genres": row.get("genres"),
                "isAdult": row.get("isAdult"),
            }
        )
    ratings = [
        {
            "tconst": row.get("tconst"),
            "averageRating": row.get("averageRating"),
            "numVotes": row.get("numVotes"),
        }
        for row in _iter_tsv(ratings_path)
    ]
    chart = top250_from_tables(basics, ratings)
    if len(chart) < 50:
        raise RuntimeError(f"IMDb top list too short ({len(chart)})")
    print(
        "chart "
        + ", ".join(f"#{row['rank']} {row['title']} ({row['year']})" for row in chart[:5]),
        flush=True,
    )
    return chart


def load_chart(state: dict, day: str) -> list[dict]:
    cached = state.get("chart") or []
    if state.get("chart_date") == day and len(cached) >= 200:
        return cached
    try:
        chart = _fetch_chart_page()
    except Exception as exc:
        print(f"imdb chart fetch failed: {type(exc).__name__}: {exc}", flush=True)
        chart = []
    if len(chart) < 200:
        chart = _chart_from_datasets()
    state["chart"] = chart
    state["chart_date"] = day
    _save_state(state)
    return chart


def _queue(title: str, year: int) -> dict:
    import app

    raw = app.run_search(title, "movies", year, extra_patterns=[f"{title} {year}"])
    rank_year = year if year >= 1950 else None
    picks = app.top_unique(raw, "movies", title=title, year=rank_year)
    chosen = None
    for pick in picks:
        item = app.RESULTS.get(pick["id"]) or {}
        if item and filename_has_year(item.get("name") or "", year) and int(item.get("seeds") or 0) > 0:
            chosen = item
            break
    if not chosen:
        return {"status": "miss", "detail": "no seeded torrent with that year"}
    existing = app._find_torrent(chosen["name"], chosen["url"], fallback_latest=False)
    if existing:
        return {"status": "already", "name": chosen["name"], "seeds": chosen["seeds"]}
    data = {"urls": chosen["url"], "category": app.MOVIE_CATEGORY}
    code, text = app.qbit("POST", "/api/v2/torrents/add", data, timeout=30)
    if code not in (200, 409):
        return {"status": "error", "detail": f"qBit add failed ({code}): {text[:160]}", "name": chosen["name"]}
    print(
        f"queued {title} ({year}) -> {chosen['name']} seeds={chosen['seeds']} size={chosen['size_label']}",
        flush=True,
    )
    return {
        "status": "queued" if code == 200 else "already",
        "name": chosen["name"],
        "seeds": chosen["seeds"],
        "size": chosen["size_label"],
    }


def run_once(day: str | None = None) -> dict:
    import app

    day = day or _today()
    state = _load_state()
    if state.get("ran_on") == day:
        print(f"already picked for {day}", flush=True)
        return state["days"].get(day) or {}
    library = app.collect_library()
    if library.get("movie_count", 0) == 0:
        raise RuntimeError("movie library is empty; not picking")
    owned = owned_title_keys(library["movies"])
    chart = load_chart(state, day)
    queued = set(state.get("queued_ids") or [])
    picks = choose_daily(chart, owned, queued, day, COUNT)
    report = []
    for row in picks:
        outcome = {"id": row["id"], "title": row["title"], "year": row["year"], "rank": row["rank"]}
        try:
            outcome.update(_queue(row["title"], int(row["year"])))
        except Exception as exc:
            outcome.update({"status": "error", "detail": f"{type(exc).__name__}: {exc}"})
            print(f"pick failed {row['title']}: {outcome['detail']}", flush=True)
        if outcome.get("status") in {"queued", "already"}:
            queued.add(row["id"])
        report.append(outcome)
        state["queued_ids"] = sorted(queued)
        state["days"][day] = report
        _save_state(state)
    state["ran_on"] = day
    state["days"][day] = report
    _save_state(state)
    print(
        f"day {day}: " + ", ".join(f"{row['title']} ({row['status']})" for row in report),
        flush=True,
    )
    return {"day": day, "picks": report}


def seconds_until_next_midnight(now: datetime) -> int:
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt = nxt + timedelta(days=1)
    return max(1, int((nxt - now).total_seconds()))


def main() -> None:
    while True:
        now = _now()
        if not in_run_window(now):
            wait = seconds_until_next_midnight(now)
            print(f"outside midnight–6am; sleeping {wait}s", flush=True)
            time.sleep(wait)
            continue
        try:
            run_once()
        except Exception as exc:
            print(f"daily run failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(900)
            continue
        wait = seconds_until_next_midnight(_now())
        print(f"sleeping {wait}s until the next pick", flush=True)
        time.sleep(wait)


if __name__ == "__main__":
    main()
