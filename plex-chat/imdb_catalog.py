#!/usr/bin/env python3
"""One IMDb catalog shared by chat genre search and the daily Top 250 picker.

imdb-daily writes catalog.json. plex-chat only reads it. The Bayesian mean
covers qualifying movies with at least 1,000 votes and is not stored as rows.
Stored titles are movies and series with at least 25,000 votes.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

MIN_VOTES = 25_000
MEAN_VOTE_FLOOR = 1_000
MIN_RUNTIME = 40
CATALOG_NAME = "catalog.json"
_KINDS = {"movie", "tvSeries"}


def catalog_path(cache_dir: Path) -> Path:
    return cache_dir / CATALOG_NAME


def iter_tsv(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        header = handle.readline().rstrip("\n").split("\t")
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(header):
                continue
            yield dict(zip(header, parts))


def _counts_toward_mean(row: dict) -> bool:
    if row.get("titleType") != "movie" or str(row.get("isAdult")) != "0":
        return False
    genres = [part for part in str(row.get("genres") or "").split(",") if part and part != "\\N"]
    if "Documentary" in genres:
        return False
    runtime = str(row.get("runtimeMinutes") or "")
    if runtime.isdigit() and int(runtime) < MIN_RUNTIME:
        return False
    year = str(row.get("startYear") or "")
    title = str(row.get("primaryTitle") or "").strip()
    return year.isdigit() and bool(title) and bool(row.get("tconst"))


def _load_ratings(path: Path) -> dict[str, tuple[float, int]]:
    rated: dict[str, tuple[float, int]] = {}
    for row in iter_tsv(path):
        imdb_id = str(row.get("tconst") or "")
        if not imdb_id:
            continue
        try:
            rating = float(row.get("averageRating") or 0)
            votes = int(row.get("numVotes") or 0)
        except ValueError:
            continue
        if votes >= MEAN_VOTE_FLOOR:
            rated[imdb_id] = (rating, votes)
    return rated


def build_catalog(basics_path: Path, ratings_path: Path) -> dict:
    rated = _load_ratings(ratings_path)
    titles = []
    rating_sum = 0.0
    rating_n = 0
    for row in iter_tsv(basics_path):
        kind = row.get("titleType") or ""
        if kind not in _KINDS or str(row.get("isAdult")) != "0":
            continue
        imdb_id = str(row.get("tconst") or "")
        score = rated.get(imdb_id)
        if score is None:
            continue
        rating, votes = score
        if _counts_toward_mean(row):
            rating_sum += rating
            rating_n += 1
        if votes < MIN_VOTES:
            continue
        year = str(row.get("startYear") or "")
        title = str(row.get("primaryTitle") or "").strip()
        if not year.isdigit() or not title:
            continue
        genres = [part for part in str(row.get("genres") or "").split(",") if part and part != "\\N"]
        runtime_text = str(row.get("runtimeMinutes") or "")
        titles.append(
            {
                "id": imdb_id,
                "title": title,
                "year": int(year),
                "genres": genres,
                "kind": kind,
                "rating": rating,
                "votes": votes,
                "runtime": int(runtime_text) if runtime_text.isdigit() else None,
            }
        )
    mean = (rating_sum / rating_n) if rating_n else 7.0
    return {
        "basics_mtime_ns": basics_path.stat().st_mtime_ns,
        "ratings_mtime_ns": ratings_path.stat().st_mtime_ns,
        "mean": mean,
        "titles": titles,
    }


def write_catalog(dest: Path, catalog: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalog) + "\n")
    tmp.replace(dest)


def read_catalog(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("titles"), list):
        return None
    return data


def matches_dumps(catalog: dict, basics_path: Path, ratings_path: Path) -> bool:
    try:
        basics_ns = basics_path.stat().st_mtime_ns
        ratings_ns = ratings_path.stat().st_mtime_ns
    except OSError:
        return False
    return (
        int(catalog.get("basics_mtime_ns") or -1) == basics_ns
        and int(catalog.get("ratings_mtime_ns") or -1) == ratings_ns
    )


def ensure_catalog(cache_dir: Path) -> dict:
    basics_path = cache_dir / "title.basics.tsv.gz"
    ratings_path = cache_dir / "title.ratings.tsv.gz"
    if not basics_path.is_file() or not ratings_path.is_file():
        raise FileNotFoundError(f"IMDb datasets missing in {cache_dir}")
    dest = catalog_path(cache_dir)
    existing = read_catalog(dest)
    if existing is not None and matches_dumps(existing, basics_path, ratings_path):
        return existing
    catalog = build_catalog(basics_path, ratings_path)
    write_catalog(dest, catalog)
    return catalog


def genre_rows(catalog: dict, min_rating: float) -> list[dict]:
    rows = []
    for row in catalog.get("titles") or []:
        if not isinstance(row, dict):
            continue
        genres = tuple(row.get("genres") or ())
        if not genres:
            continue
        try:
            rating = float(row.get("rating") or 0)
            votes = int(row.get("votes") or 0)
            year = int(row.get("year") or 0)
        except (TypeError, ValueError):
            continue
        if rating <= min_rating or votes < MIN_VOTES or year <= 0:
            continue
        title = str(row.get("title") or "").strip()
        imdb_id = str(row.get("id") or "")
        kind = str(row.get("kind") or "")
        if not title or not imdb_id or kind not in _KINDS:
            continue
        rows.append(
            {
                "id": imdb_id,
                "title": title,
                "year": year,
                "genres": genres,
                "kind": kind,
                "rating": rating,
                "votes": votes,
            }
        )
    return rows
