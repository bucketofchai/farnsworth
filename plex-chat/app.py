#!/usr/bin/env python3
"""LAN chat UI: local LLM → qBittorrent search → pick → download status."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
import json
import os
import random
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import imdb_catalog

QBIT_URL = os.environ.get("QBIT_URL", "http://127.0.0.1:18081").rstrip("/")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400").rstrip("/")
GLUETUN_CTRL = os.environ.get("GLUETUN_CTRL", "http://127.0.0.1:8000").rstrip("/")
PLEX_QBIT_STATE = Path(os.environ.get("PLEX_QBIT_STATE", "/var/lib/plex-qbit/state.json"))
ORGANIZE_STATE = Path(os.environ.get("QBIT_ORGANIZE_STATE", "/var/lib/qbit-organize/state.json"))
HOST_PROC = Path(os.environ.get("HOST_PROC", "/host/proc"))
HOST_ROOT = Path(os.environ.get("HOST_ROOT", "/hostfs"))
HOST_USB = Path(os.environ.get("HOST_USB", str(HOST_ROOT / "mnt/usbdrive")))
ORGANIZE_STALE_SEC = int(os.environ.get("ORGANIZE_STALE_SEC", "90"))
SEARCH_TIMEOUT = int(os.environ.get("SEARCH_TIMEOUT", "25"))
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "12"))
IMDB_TIMEOUT = int(os.environ.get("IMDB_TIMEOUT", "8"))
TV_CATEGORY = os.environ.get("QBIT_TV_CATEGORY", "TV")
MOVIE_CATEGORY = os.environ.get("QBIT_MOVIE_CATEGORY", "Movies")
TV_DIR = Path(os.environ.get("TV_DIR", "/tv"))
MOVIE_DIR = Path(os.environ.get("MOVIE_DIR", "/movies"))
LIBRARY_CACHE_SEC = int(os.environ.get("LIBRARY_CACHE_SEC", "60"))
IMDB_CACHE_SEC = int(os.environ.get("IMDB_CACHE_SEC", "21600"))
# Home Plex default: keep typical 1080p rips, drop huge 4K remuxes (~40–80 GiB).
_MAX_TORRENT_GIB_DEFAULT = 20
STATIC = Path(__file__).resolve().parent / "static"
_LIBRARY_CACHE: tuple[float, dict] | None = None
_IMDB_CACHE: dict[str, tuple[float, dict | None]] = {}
IMDB_MOVIE = {"movie", "tvMovie", "short", "video"}
IMDB_TV = {"tvSeries", "tvMiniSeries", "tvSpecial", "tvShort"}
IMDB_TV_SERIES = {"tvSeries", "tvMiniSeries"}
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts"}
ACTIVE_DL = {
    "downloading",
    "stalledDL",
    "queuedDL",
    "pausedDL",
    "stoppedDL",
    "metaDL",
    "allocating",
    "checkingDL",
    "forcedDL",
    "moving",
}
SEASON_RE = re.compile(
    r"(?i)[.\-_ ]*(?:complete[.\-_ ]*)?(?:season[.\-_ ]*(\d{1,2})|\bS(\d{1,2})\b).*$"
)
PACK_MOD_RE = re.compile(r"(?i)\b(?:complete|pack)\b")
IMDB_EXTRA_RE = re.compile(
    r"(?i)^(?P<show>.+?):\s*(?:complete\s+)?(?:season\s*(?P<season>\d{1,2})|"
    r"s(?P<scode>\d{1,2}))\b(?:\s*[-–—:]\s*(?P<extra>.+))?$"
)
JUNK_RE = re.compile(
    r"(?i)[.\-_\[( ]+(?:720p|1080p|2160p|4k|uhd|hdr|bluray|blu-ray|web-?dl|"
    r"webrip|hdtv|x264|x265|hevc|aac|ac3|dts|amzn|dsnp|complete|repack|"
    r"retail|uncut|remastered|yts|etrg|tgx|eztv|nosub).*$"
)
TITLE_STOP = {"the", "a", "an", "of", "and", "in"}
YEAR_TOKEN_RE = re.compile(r"(?:\(|\b)((?:19|20)\d{2})(?:\)|\b)")
SIZE_LABEL_RE = re.compile(
    r"(?i)^\s*([\d]+(?:[.,]\d+)?)\s*(bytes?|b|kib|kb|mib|mb|gib|gb|tib|tb)\s*$"
)
# Dry one-liners after a finished qBit search (hits and zero-hits). Not used on errors.
SEARCH_QUIPS = (
    "qBit rummaged the trackers so you don't have to.",
    "That's the swarm's current opinion. I just sorted it.",
    "Indexers have spoken. Quietly, as usual.",
    "I ranked by seeders, not vibes.",
    "Search done. Magnets either showed up or they ghosted.",
    "Plex can wait; the swarm already voted.",
    "If it isn't here, it isn't seeding. Harsh, but tidy.",
    "I asked the indexers nicely. This is what they handed back.",
    "Done looking. Download, or pretend you never asked.",
    "The usual: title, year, then whatever still has peers.",
    "qBit finished the rummage. I kept the usable part.",
    "Trackers checked. Peers optional. Taste not included.",
)
_quip_lock = threading.Lock()
_last_quip = -1

LLM_PROMPT = """Convert the user's request into a torrent search.
Reply with JSON only, no markdown:
{"query":"short search string","category":"tv"|"movies"|"all"}
query should be the title only, plus season if they asked for one.
Keep a year only if the user named one. Do not invent a year or site names.
"""

# "Find me a comedy" is a genre browse, not a title search.
GENRE_ALIASES = (
    ("science fiction", "Sci-Fi"),
    ("sci-fi", "Sci-Fi"),
    ("scifi", "Sci-Fi"),
    ("romantic comedy", "Comedy"),
    ("rom-com", "Comedy"),
    ("romcom", "Comedy"),
    ("comedies", "Comedy"),
    ("comedy", "Comedy"),
    ("funny", "Comedy"),
    ("dramas", "Drama"),
    ("drama", "Drama"),
    ("thrillers", "Thriller"),
    ("thriller", "Thriller"),
    ("horrors", "Horror"),
    ("horror", "Horror"),
    ("scary", "Horror"),
    ("action", "Action"),
    ("adventures", "Adventure"),
    ("adventure", "Adventure"),
    ("animation", "Animation"),
    ("animated", "Animation"),
    ("crime", "Crime"),
    ("fantasy", "Fantasy"),
    ("mysteries", "Mystery"),
    ("mystery", "Mystery"),
    ("romance", "Romance"),
    ("romantic", "Romance"),
    ("family", "Family"),
    ("westerns", "Western"),
    ("western", "Western"),
    ("war", "War"),
    ("musicals", "Musical"),
    ("musical", "Musical"),
    ("biography", "Biography"),
    ("biopic", "Biography"),
    ("history", "History"),
    ("sports", "Sport"),
    ("sport", "Sport"),
    ("documentaries", "Documentary"),
    ("documentary", "Documentary"),
)
GENRE_FILLER = {
    "a", "an", "some", "me", "i", "you", "to", "for", "of", "the", "and",
    "find", "suggest", "recommend", "recommendation", "recommendations",
    "looking", "look", "want", "wanted", "need", "gimme", "get", "give",
    "show", "something", "anything", "please", "good", "great", "movie",
    "movies", "film", "films", "series", "tv", "watch", "newer", "new",
    "recent", "rated", "over", "stars", "star", "with", "like", "from",
}
GENRE_CUE_RE = re.compile(
    r"\b(find|suggest|recommend|looking|want|gimme|something|anything|need)\b",
    re.I,
)
GENRE_MIN_RATING = 7.0
GENRE_MIN_VOTES = 25000
IMDB_DATA_DIR = Path(os.environ.get("IMDB_DATA_DIR", "/var/lib/imdb-daily"))
_GENRE_LOCK = threading.Lock()
_GENRE_INDEX: list[dict] | None = None
_GENRE_MTIME: tuple[float, float] | None = None

RESULTS: dict[str, dict] = {}
PICK_TTL_SEC = 30 * 60
PICK_MAX = 48
_RESULTS_LOCK = threading.Lock()
CHAT_USER = os.environ.get("PLEX_CHAT_USER", "").strip()
CHAT_PASS = os.environ.get("PLEX_CHAT_PASSWORD", "")

app = FastAPI(title="Plex chat")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _basic_ok(header: str) -> bool:
    if not CHAT_USER or not CHAT_PASS or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, sep, password = raw.partition(":")
    if not sep:
        return False
    user_ok = secrets.compare_digest(user, CHAT_USER) if len(user) == len(CHAT_USER) else False
    pass_ok = secrets.compare_digest(password, CHAT_PASS) if len(password) == len(CHAT_PASS) else False
    return user_ok and pass_ok


@app.middleware("http")
async def require_basic_auth(request, call_next):
    if _basic_ok(request.headers.get("authorization") or ""):
        return await call_next(request)
    return Response(
        "Authentication required\n",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="plex-chat"', "Cache-Control": "no-store"},
        media_type="text/plain",
    )


@app.on_event("startup")
def _require_chat_password() -> None:
    if not CHAT_USER or not CHAT_PASS:
        raise RuntimeError("PLEX_CHAT_USER and PLEX_CHAT_PASSWORD must be set")
    threading.Thread(target=_warm_genre_index, daemon=True, name="imdb-genre-index").start()


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class DownloadIn(BaseModel):
    id: str = Field(min_length=8, max_length=64)


def _call_with_timeout(fn, timeout: float, *args, **kwargs):
    """Run fn with a hard wall-clock limit. urllib timeouts only cover idle sockets."""
    box: dict = {}

    def run() -> None:
        try:
            box["ok"] = fn(*args, **kwargs)
        except Exception as exc:
            box["err"] = exc

    thread = threading.Thread(
        target=run, daemon=True, name=getattr(fn, "__name__", "timeout-call")
    )
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"{getattr(fn, '__name__', 'call')} timed out after {timeout}s")
    if "err" in box:
        raise box["err"]
    return box["ok"]


def qbit(method: str, path: str, data: dict | None = None, timeout: int = 30) -> tuple[int, str]:
    body = None if data is None else urllib.parse.urlencode(data).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
    req = urllib.request.Request(f"{QBIT_URL}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def ollama_chat(user_text: str) -> dict:
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "keep_alive": "30m",
        "options": {"temperature": 0.1, "num_ctx": 1024},
        "messages": [
            {"role": "system", "content": LLM_PROMPT},
            {"role": "user", "content": user_text},
        ],
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    content = ((data.get("message") or {}).get("content") or "").strip()
    parsed = json.loads(content)
    query = str(parsed.get("query") or user_text).strip()
    category = str(parsed.get("category") or "all").strip().lower()
    if category not in {"tv", "movies", "all"}:
        category = "all"
    if not query:
        query = user_text.strip()
    return {"query": query, "category": category}


def parse_genre_request(text: str) -> dict | None:
    """Return {genre, kind} when the user asked for a kind of movie, not a title."""
    raw = (text or "").strip()
    if not raw or not GENRE_CUE_RE.search(raw):
        return None
    lower = raw.lower()
    found = None
    stripped = lower
    for alias, name in GENRE_ALIASES:
        match = re.search(rf"\b{re.escape(alias)}\b", lower)
        if not match:
            continue
        found = name
        stripped = lower[: match.start()] + " " + lower[match.end() :]
        break
    if not found:
        return None
    leftover = re.sub(r"[^a-z0-9\s]", " ", stripped)
    tokens = [token for token in leftover.split() if token not in GENRE_FILLER and not token.isdigit()]
    if tokens:
        return None
    kind = "tvSeries" if re.search(r"\b(show|series|tv)\b", lower) else "movie"
    return {"genre": found, "kind": kind}


def _title_key(title: str) -> str:
    text = re.sub(r"\s*\(((?:18|19|20)\d{2})\)\s*$", "", title or "")
    return norm_name(text)


def rank_genre_rows(
    rows: list[dict],
    genre: str,
    kind: str,
    owned: set[str],
    limit: int = 3,
) -> list[dict]:
    matched = []
    for row in rows:
        if row.get("kind") != kind or genre not in (row.get("genres") or ()):
            continue
        rating = float(row.get("rating") or 0)
        votes = int(row.get("votes") or 0)
        if rating <= GENRE_MIN_RATING or votes < GENRE_MIN_VOTES:
            continue
        if _title_key(str(row.get("title") or "")) in owned:
            continue
        matched.append(row)
    def _rank_key(row: dict) -> tuple:
        genres = row.get("genres") or ()
        primary = 0 if genres and genres[0] == genre else 1
        return (
            primary,
            -int(row["year"]),
            -float(row["rating"]),
            -int(row["votes"]),
            str(row["title"]).lower(),
        )

    matched.sort(key=_rank_key)
    return matched[: max(0, limit)]


def _iter_imdb_tsv(path: Path):
    import gzip

    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        header = handle.readline().rstrip("\n").split("\t")
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(header):
                continue
            yield dict(zip(header, parts))


def _build_genre_index(basics_path: Path, ratings_path: Path) -> list[dict]:
    titles: dict[str, tuple[str, int, tuple[str, ...], str]] = {}
    for row in _iter_imdb_tsv(basics_path):
        kind = row.get("titleType") or ""
        if kind not in {"movie", "tvSeries"} or row.get("isAdult") != "0":
            continue
        year = row.get("startYear") or ""
        title = (row.get("primaryTitle") or "").strip()
        imdb_id = row.get("tconst") or ""
        if not year.isdigit() or not title or not imdb_id:
            continue
        genres = tuple(part for part in (row.get("genres") or "").split(",") if part and part != "\\N")
        if not genres:
            continue
        titles[imdb_id] = (title, int(year), genres, kind)
    index = []
    for row in _iter_imdb_tsv(ratings_path):
        imdb_id = row.get("tconst") or ""
        meta = titles.get(imdb_id)
        if meta is None:
            continue
        try:
            rating = float(row.get("averageRating") or 0)
            votes = int(row.get("numVotes") or 0)
        except ValueError:
            continue
        if rating <= GENRE_MIN_RATING or votes < GENRE_MIN_VOTES:
            continue
        title, year, genres, kind = meta
        index.append(
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
    print(f"imdb genre index: {len(index)} titles over {GENRE_MIN_RATING}", flush=True)
    return index


def load_genre_index() -> list[dict]:
    global _GENRE_INDEX, _GENRE_MTIME
    basics = IMDB_DATA_DIR / "title.basics.tsv.gz"
    ratings = IMDB_DATA_DIR / "title.ratings.tsv.gz"
    if not basics.is_file() or not ratings.is_file():
        raise FileNotFoundError(f"IMDb datasets missing in {IMDB_DATA_DIR}")
    catalog_file = imdb_catalog.catalog_path(IMDB_DATA_DIR)
    catalog = imdb_catalog.read_catalog(catalog_file)
    use_catalog = catalog is not None and imdb_catalog.matches_dumps(catalog, basics, ratings)
    if use_catalog:
        stamp: tuple = ("catalog", catalog_file.stat().st_mtime_ns)
    else:
        stamp = ("tsv", basics.stat().st_mtime_ns, ratings.stat().st_mtime_ns)
    with _GENRE_LOCK:
        if _GENRE_INDEX is not None and _GENRE_MTIME == stamp:
            return _GENRE_INDEX
        if use_catalog and catalog is not None:
            index = imdb_catalog.genre_rows(catalog, GENRE_MIN_RATING)
            print(f"imdb genre index: {len(index)} titles from catalog", flush=True)
        else:
            index = _build_genre_index(basics, ratings)
        _GENRE_INDEX = index
        _GENRE_MTIME = stamp
        return index


def _warm_genre_index() -> None:
    try:
        load_genre_index()
    except Exception as exc:
        print(f"imdb genre index not ready: {type(exc).__name__}: {exc}", flush=True)


def suggest_genre(genre: str, kind: str, owned: set[str], limit: int = 3) -> list[dict]:
    return rank_genre_rows(load_genre_index(), genre, kind, owned, limit)


def fallback_parse(user_text: str) -> dict:
    text = user_text.strip()
    lower = text.lower()
    category = "all"
    if re.search(r"\b(s\d{1,2}e\d{1,2}|season|episode|series|show|tv)\b", lower):
        category = "tv"
    elif re.search(r"\b(movie|film|cinema)\b", lower):
        category = "movies"
    return {"query": text, "category": category}


def qbit_search_category(category: str) -> str:
    if category == "tv":
        return "tv"
    if category == "movies":
        return "movies"
    return "all"


def add_category(category: str) -> str:
    if category == "tv":
        return TV_CATEGORY
    if category == "movies":
        return MOVIE_CATEGORY
    return ""


def human_size(num: int) -> str:
    if num is None or num < 0:
        return "?"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


def max_torrent_bytes() -> int:
    raw_bytes = (os.environ.get("MAX_TORRENT_BYTES") or "").strip()
    if raw_bytes:
        try:
            return max(0, int(raw_bytes))
        except ValueError:
            pass
    raw_gib = (os.environ.get("MAX_TORRENT_GIB") or "").strip() or str(_MAX_TORRENT_GIB_DEFAULT)
    try:
        return max(0, int(float(raw_gib) * (1024 ** 3)))
    except ValueError:
        return _MAX_TORRENT_GIB_DEFAULT * (1024 ** 3)


def parse_size_bytes(value) -> int:
    """Return size in bytes, or -1 if unknown."""
    if value is None or value == "" or isinstance(value, bool):
        return -1
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else -1
    text = str(value).strip()
    if not text or text == "?":
        return -1
    try:
        n = int(float(text.replace(",", "")))
        return n if n >= 0 else -1
    except ValueError:
        pass
    match = SIZE_LABEL_RE.match(text)
    if not match:
        return -1
    num = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    # 1024-based so a "50 GB" remux is treated as large even if the label is SI.
    multipliers = {
        "b": 1,
        "byte": 1,
        "bytes": 1,
        "kb": 1024,
        "kib": 1024,
        "mb": 1024 ** 2,
        "mib": 1024 ** 2,
        "gb": 1024 ** 3,
        "gib": 1024 ** 3,
        "tb": 1024 ** 4,
        "tib": 1024 ** 4,
    }
    factor = multipliers.get(unit)
    if factor is None:
        return -1
    return int(num * factor)


def result_size_bytes(item: dict) -> int:
    size = parse_size_bytes(item.get("fileSize"))
    if size >= 0:
        return size
    return parse_size_bytes(item.get("size_label") or item.get("size"))


def torrent_too_large(size_n: int, limit: int) -> bool:
    if limit <= 0 or size_n < 0:
        return False
    return size_n > limit


def human_eta(seconds: int) -> str:
    if seconds is None or seconds < 0 or seconds > 86400 * 30:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def norm_name(name: str) -> str:
    cleaned = re.sub(r"[._]+", " ", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


YEAR_MIN = 1900
YEAR_MAX = 2035


def _year_ok(year: int) -> bool:
    return YEAR_MIN <= year <= YEAR_MAX


def extract_year(text: str) -> int | None:
    for match in YEAR_TOKEN_RE.finditer(text or ""):
        year = int(match.group(1))
        if _year_ok(year):
            return year
    return None


def years_in(text: str) -> list[int]:
    out = []
    for match in YEAR_TOKEN_RE.finditer(text or ""):
        year = int(match.group(1))
        if _year_ok(year):
            out.append(year)
    return out


def strip_years(text: str) -> str:
    cleaned = YEAR_TOKEN_RE.sub(" ", text or "")
    cleaned = re.sub(r"[\[\](){}]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


def parse_search_query(user_text: str, llm_query: str) -> tuple[str, int | None, int | None]:
    raw = strip_years(llm_query or user_text)
    if not raw:
        raw = strip_years(user_text) or (user_text or "").strip()
    season = _season_num(raw) or _season_num(user_text or "")
    show = SEASON_RE.sub("", raw)
    show = PACK_MOD_RE.sub(" ", show)
    show = re.sub(r"\s+", " ", show).strip(" -.:")
    if not show:
        show = PACK_MOD_RE.sub(" ", raw)
        show = re.sub(r"\s+", " ", show).strip(" -.:") or raw
    return show, season, extract_year(user_text)


def _imdb_slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return slug[:20]


def _imdb_fetch(title: str) -> list[dict]:
    slug = _imdb_slug(title)
    if not slug:
        return []
    url = f"https://v2.sg.media-imdb.com/suggestion/{slug[0]}/{slug}.json"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=IMDB_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    rows = []
    for item in data.get("d") or []:
        name = str(item.get("l") or "").strip()
        year = item.get("y")
        qid = str(item.get("qid") or "")
        if not name or not isinstance(year, int):
            continue
        rows.append(
            {
                "title": name,
                "year": year,
                "id": str(item.get("id") or ""),
                "qid": qid,
                "rank": int(item.get("rank") or 10_000_000),
            }
        )
    return rows


def _imdb_kind(category: str) -> set[str] | None:
    if category == "tv":
        return IMDB_TV
    if category == "movies":
        return IMDB_MOVIE
    return None


def _imdb_series_core(title: str) -> str:
    match = IMDB_EXTRA_RE.match((title or "").strip())
    if match:
        return (match.group("show") or title).strip()
    return title


def _imdb_is_series_extra(row_title: str, query_title: str) -> bool:
    match = IMDB_EXTRA_RE.match((row_title or "").strip())
    if not match:
        return False
    extra = (match.group("extra") or "").strip()
    if not extra:
        return True
    extra_tokens = set(title_tokens(extra))
    query_tokens = set(title_tokens(query_title))
    if extra_tokens and extra_tokens <= query_tokens:
        return False
    return True


def _canonical_show_ok(imdb_title: str, show: str) -> bool:
    if _imdb_is_series_extra(imdb_title, show):
        return False
    core = _imdb_series_core(imdb_title)
    return title_matches(core, show) and title_matches(show, core)


def _imdb_score(row: dict, title: str, prefer_year: int | None) -> tuple:
    want = title_tokens(title)
    core = _imdb_series_core(row["title"])
    got = title_tokens(core)
    want_n = norm_name(title)
    core_n = norm_name(core)
    exact = core_n == want_n or norm_name(row["title"]) == want_n
    prefix = exact or core_n.startswith(want_n + " ") or want_n.startswith(core_n + " ")
    token_hit = sum(1 for token in want if token in set(got)) if want else 0
    year_hit = 0 if prefer_year and row["year"] == prefer_year else 1
    return (
        0 if exact else 1,
        0 if prefix else 1,
        year_hit,
        -token_hit,
        row["rank"],
        abs((prefer_year or row["year"]) - row["year"]),
    )


def imdb_lookup(title: str, category: str, prefer_year: int | None = None) -> dict | None:
    key = f"{norm_name(title)}|{category}|{prefer_year or ''}"
    now = time.time()
    cached = _IMDB_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    hit = None
    try:
        rows = _call_with_timeout(_imdb_fetch, IMDB_TIMEOUT + 1, title)
        kind = _imdb_kind(category)
        if category == "tv":
            rows = [row for row in rows if row["qid"] in IMDB_TV_SERIES]
        elif kind:
            typed = [row for row in rows if row["qid"] in kind]
            rows = typed or rows
        plausible = []
        for row in rows:
            if _imdb_is_series_extra(row["title"], title):
                continue
            if title_matches(row["title"], title) or title_matches(title, row["title"]):
                plausible.append(row)
        if plausible:
            plausible.sort(key=lambda row: _imdb_score(row, title, prefer_year))
            hit = plausible[0]
            print(
                f"imdb {title!r} -> {hit['title']} ({hit['year']}) {hit['id']}",
                flush=True,
            )
    except Exception as exc:
        print(f"imdb lookup failed: {type(exc).__name__}: {exc}", flush=True)
    _IMDB_CACHE[key] = (now + IMDB_CACHE_SEC, hit)
    return hit


def title_tokens(title: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", title.lower())
    kept = [t for t in tokens if t not in TITLE_STOP and len(t) > 1]
    return kept or tokens


def _strip_season_mods(title: str) -> str:
    text = SEASON_RE.sub(" ", title or "")
    text = PACK_MOD_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -.:")


def _is_criminal_minds(show: str) -> bool:
    return norm_name(show) == "criminal minds"


def _season_equiv(found: int, want: int, name: str, title: str) -> bool:
    if found == want:
        return True
    blob = f"{norm_name(name)} {norm_name(title)}"
    if "evolution" not in blob:
        return False
    if want >= 16 and found == want - 15:
        return True
    if found >= 16 and want == found - 15:
        return True
    return False


def title_matches(name: str, title: str, season: int | None = None) -> bool:
    check_season = season if season is not None else _season_num(title)
    if check_season is not None:
        found = _season_num(name)
        if found is not None and not _season_equiv(found, check_season, name, title):
            return False
    tokens = title_tokens(_strip_season_mods(title) or title)
    if not tokens:
        return True
    blob = norm_name(name)
    hits = sum(1 for token in tokens if token in blob)
    need = len(tokens) if len(tokens) <= 3 else len(tokens) - 1
    return hits >= need


def search_patterns(show: str, season: int | None) -> list[str]:
    if season is None:
        return [show]
    patterns = [f"{show} season {season}", f"{show} S{season:02d}"]
    if _is_criminal_minds(show) and season >= 16:
        patterns.append(f"{show} Evolution S{season - 15:02d}")
    return patterns


def format_search_display(
    show: str,
    season: int | None,
    year: int | None,
    year_from: str,
    used_imdb_name: bool = False,
) -> str:
    if season is not None:
        if used_imdb_name:
            return f"{show} (IMDb) · season {season}"
        return f"{show} season {season}"
    if year:
        return f"{show} ({year}, IMDb)" if year_from == "imdb" else f"{show} ({year})"
    return show


def plan_search(user_text: str, llm_query: str, category: str) -> dict:
    show, season, user_year = parse_search_query(user_text, llm_query)
    if season is not None and category == "all":
        category = "tv"
    prefer_year = None if season is not None else user_year
    imdb = imdb_lookup(show, category, prefer_year=prefer_year)
    year = user_year
    year_from = "you"
    used_imdb_name = False
    if imdb:
        imdb_title = str(imdb.get("title") or "").strip()
        if imdb_title and _canonical_show_ok(imdb_title, show):
            show = imdb_title
            used_imdb_name = True
        if season is None and imdb.get("year"):
            year = int(imdb["year"])
            year_from = "imdb"
    if season is not None:
        year = None
        year_from = "you"
    patterns = search_patterns(show, season)
    if year:
        year_pat = f"{show} {year}"
        if year_pat not in patterns:
            patterns.append(year_pat)
    return {
        "show": show,
        "season": season,
        "year": year,
        "user_year": user_year,
        "year_from": year_from,
        "category": category,
        "patterns": patterns,
        "display": format_search_display(show, season, year, year_from, used_imdb_name),
        "imdb": imdb,
        "title": show if season is None else f"{show} season {season}",
        "used_imdb_name": used_imdb_name,
    }


def year_delta(name: str, want: int | None) -> int:
    if want is None:
        return 0
    found = years_in(name)
    if want in found:
        return 0
    if not found:
        return 40
    return min(abs(year - want) for year in found)


def run_search(
    query: str,
    category: str,
    year: int | None = None,
    extra_patterns: list[str] | None = None,
) -> list[dict]:
    qcat = qbit_search_category(category)
    title = strip_years(query) or query
    merged: list[dict] = []
    errors: list[str] = []
    patterns = [title]
    if year:
        patterns.append(f"{title} {year}")
    for extra in extra_patterns or []:
        cleaned = strip_years(extra) or extra
        if cleaned and cleaned not in patterns:
            patterns.append(cleaned)
    with ThreadPoolExecutor(max_workers=max(1, len(patterns))) as pool:
        futs = {pool.submit(_nova_search, pattern, qcat): pattern for pattern in patterns if pattern}
        for fut in as_completed(futs):
            pattern = futs[fut]
            rows, err = fut.result()
            merged.extend(rows)
            errors.extend(err)
            print(f"search {pattern!r}: {len(rows)} hits", flush=True)
    if errors:
        print(f"search indexer errors: {'; '.join(errors)}", flush=True)
    return merged


def _nova_search(query: str, qcat: str) -> tuple[list[dict], list[str]]:
    qs = urllib.parse.urlencode({"pattern": query, "category": qcat})
    try:
        code, text = _call_with_timeout(
            qbit, SEARCH_TIMEOUT + 12, "GET", f"/nova-search?{qs}", None, SEARCH_TIMEOUT + 10
        )
    except Exception as exc:
        print(f"search failed: {type(exc).__name__}: {exc}", flush=True)
        return [], [f"{type(exc).__name__}: {exc}"]
    if code != 200:
        print(f"search failed ({code}): {text[:200]}", flush=True)
        return [], [f"search failed ({code})"]
    payload = json.loads(text) or {}
    return payload.get("results") or [], list(payload.get("errors") or [])


def top_unique(
    raw: list[dict],
    category: str,
    title: str = "",
    year: int | None = None,
    season: int | None = None,
) -> list[dict]:
    best: dict[str, dict] = {}
    limit = max_torrent_bytes()
    for item in raw:
        name = str(item.get("fileName") or "").strip()
        url = str(item.get("fileUrl") or "").strip()
        if not name or not url:
            continue
        if title and not title_matches(name, title, season=season):
            continue
        size_n = result_size_bytes(item)
        if torrent_too_large(size_n, limit):
            continue
        seeds = item.get("nbSeeders")
        try:
            seeds = int(seeds)
        except (TypeError, ValueError):
            seeds = -1
        if seeds < 0:
            seeds = 0
        key = norm_name(name)
        current = best.get(key)
        if current is None or seeds > current["seeds"]:
            rid = uuid.uuid4().hex
            best[key] = {
                "id": rid,
                "name": name,
                "seeds": seeds,
                "leechers": int(item.get("nbLeechers") or 0),
                "size": size_n,
                "size_label": human_size(size_n),
                "site": str(item.get("siteUrl") or item.get("engineName") or ""),
                "category": category,
                "year_delta": year_delta(name, year),
                "url": url,
            }
    ranked = sorted(
        best.values(),
        key=lambda row: (row["year_delta"], -row["seeds"], row["name"].lower()),
    )
    chosen = ranked[:3]
    _remember_picks(chosen)
    hidden = {"url", "saved_at"}
    return [{k: v for k, v in row.items() if k not in hidden} for row in chosen]


def _prune_results(now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired = [
        key
        for key, row in RESULTS.items()
        if now - float(row.get("saved_at") or 0) > PICK_TTL_SEC
    ]
    for key in expired:
        RESULTS.pop(key, None)
    overflow = len(RESULTS) - PICK_MAX
    if overflow <= 0:
        return
    oldest = sorted(RESULTS.items(), key=lambda item: float(item[1].get("saved_at") or 0))
    for key, _row in oldest[:overflow]:
        RESULTS.pop(key, None)


def _remember_picks(chosen: list[dict]) -> None:
    now = time.time()
    with _RESULTS_LOCK:
        _prune_results(now)
        for row in chosen:
            row["saved_at"] = now
            RESULTS[row["id"]] = row


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


def _http_up(url: str, timeout: float = 1.5) -> bool:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as exc:
        return 400 <= exc.code < 500
    except Exception:
        return False


def _plex_up() -> bool:
    # Host-networked like Plex (127.0.0.1:32400). The state file is a fast
    # fallback when plex-qbit has polled recently.
    try:
        age = time.time() - PLEX_QBIT_STATE.stat().st_mtime
        if age <= 25:
            return True
    except OSError:
        pass
    return _http_up(f"{PLEX_URL}/identity", timeout=0.4)


def _organize_status() -> dict:
    try:
        data = json.loads(ORGANIZE_STATE.read_text())
        age = time.time() - ORGANIZE_STATE.stat().st_mtime
        heartbeat = float(data.get("heartbeat") or 0)
        if heartbeat:
            age = time.time() - heartbeat
        pending = data.get("pending") or {}
        jobs = []
        for job in pending.values():
            if not isinstance(job, dict):
                continue
            name = str(job.get("name") or Path(str(job.get("dest") or "")).name)
            if name:
                jobs.append({"name": name, "kind": str(job.get("kind") or "")})
        error = str(data.get("last_error") or "").strip()
        ok = age <= ORGANIZE_STALE_SEC and not error
        return {
            "ok": ok,
            "busy": bool(jobs) and ok,
            "pending": jobs,
            "error": error,
            "age": round(age),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"ok": False, "busy": False, "pending": [], "error": "no state", "age": None}


_HOST_LOCK = threading.Lock()
_HOST_PREV: dict | None = None


def _proc_path(name: str) -> Path:
    candidate = HOST_PROC / name
    return candidate if candidate.is_file() else Path("/proc") / name


def _read_cpu() -> tuple[int, int, int] | None:
    try:
        line = _proc_path("stat").read_text().splitlines()[0]
    except OSError:
        return None
    parts = line.split()
    if parts[0] != "cpu" or len(parts) < 5:
        return None
    nums = [int(x) for x in parts[1:11]]
    total = sum(nums)
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    iowait = nums[4] if len(nums) > 4 else 0
    return total, idle, iowait


def _read_mem() -> dict | None:
    vals: dict[str, int] = {}
    try:
        for line in _proc_path("meminfo").read_text().splitlines():
            key, rest = line.split(":", 1)
            vals[key] = int(rest.strip().split()[0]) * 1024
    except (OSError, ValueError):
        return None
    total = vals.get("MemTotal")
    avail = vals.get("MemAvailable")
    if not total or avail is None:
        return None
    return {
        "total": total,
        "available": avail,
        "used": total - avail,
        "pct": round(100 * (total - avail) / total, 1),
    }


def _read_load() -> float | None:
    try:
        return float(_proc_path("loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _diskstats_named() -> dict[str, tuple[int, int, int]]:
    named: dict[str, tuple[int, int, int]] = {}
    try:
        for line in _proc_path("diskstats").read_text().splitlines():
            p = line.split()
            if len(p) < 14:
                continue
            name = p[2]
            if name.startswith(("loop", "ram", "dm-")):
                continue
            named[name] = (int(p[5]), int(p[9]), int(p[12]))
    except (OSError, ValueError, IndexError):
        return {}
    return named


def _disk_for_path(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)
    try:
        for line in _proc_path("diskstats").read_text().splitlines():
            p = line.split()
            if len(p) < 3:
                continue
            if int(p[0]) == major and int(p[1]) == minor:
                name = p[2]
                parent = name.rstrip("0123456789")
                return parent or name
    except (OSError, ValueError):
        return None
    return None


def _mount_usage(path: Path) -> dict | None:
    try:
        s = os.statvfs(path)
    except OSError:
        return None
    total = s.f_frsize * s.f_blocks
    if not total:
        return None
    free = s.f_frsize * s.f_bavail
    used = total - free
    return {
        "total": total,
        "used": used,
        "free": free,
        "pct": round(100 * used / total, 1),
    }


def _streams() -> int | None:
    try:
        data = json.loads(PLEX_QBIT_STATE.read_text())
        return int(data.get("streams") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _host_metrics() -> dict:
    now = time.monotonic()
    cpu_raw = _read_cpu()
    disks = _diskstats_named()
    mem = _read_mem()
    load = _read_load()
    root_path = HOST_ROOT if HOST_ROOT.is_dir() else Path("/")
    usb_path = HOST_USB if HOST_USB.is_dir() else TV_DIR
    root = _mount_usage(root_path)
    usb = _mount_usage(usb_path)
    root_disk = _disk_for_path(root_path)
    usb_disk = _disk_for_path(usb_path)
    cpu_pct = None
    iowait_pct = None
    usb_io: dict | None = None
    global _HOST_PREV
    with _HOST_LOCK:
        prev = _HOST_PREV
        _HOST_PREV = {"t": now, "cpu": cpu_raw, "disks": disks}
    if prev and cpu_raw and prev.get("cpu"):
        dt = now - prev["t"]
        pt, pi, pw = prev["cpu"]
        t, i, w = cpu_raw
        total_d = t - pt
        if dt > 0.2 and total_d > 0:
            cpu_pct = round(100 * (1 - (i - pi) / total_d), 1)
            iowait_pct = round(100 * (w - pw) / total_d, 1)
        prev_disks = prev.get("disks") or {}
        if usb_disk and usb_disk in disks and usb_disk in prev_disks and dt > 0.2:
            r0, w0, u0 = prev_disks[usb_disk]
            r1, w1, u1 = disks[usb_disk]
            usb_io = {
                "disk": usb_disk,
                "read_kbs": round((r1 - r0) / 2 / dt, 1),
                "write_kbs": round((w1 - w0) / 2 / dt, 1),
                "util": round(min(100.0, (u1 - u0) / (dt * 10)), 1),
            }
    return {
        "cpu_pct": cpu_pct,
        "iowait_pct": iowait_pct,
        "load1": load,
        "mem": mem,
        "root": root,
        "usb": usb,
        "root_disk": root_disk,
        "usb_disk": usb_disk,
        "usb_io": usb_io,
        "streams": _streams(),
    }


def _llm_status() -> tuple[bool, bool]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2) as resp:
            tags = json.loads(resp.read().decode("utf-8", "replace"))
        names = [str((m or {}).get("name") or "") for m in tags.get("models") or []]
        return True, any(OLLAMA_MODEL in name for name in names)
    except Exception:
        return False, False


@app.get("/api/health")
def health() -> dict:
    ollama_ok = False
    model_ok = False
    plex_ok = False
    vpn_ok = False
    qbit_ok = False
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {
            pool.submit(_plex_up): "plex",
            pool.submit(_http_up, f"{GLUETUN_CTRL}/v1/publicip/ip"): "vpn",
            pool.submit(qbit, "GET", "/api/v2/app/version", None, 2): "qbit",
            pool.submit(_llm_status): "llm",
        }
        for fut in as_completed(futs):
            kind = futs[fut]
            try:
                result = fut.result()
            except Exception:
                continue
            if kind == "plex":
                plex_ok = bool(result)
            elif kind == "vpn":
                vpn_ok = bool(result)
            elif kind == "qbit":
                qbit_ok = result[0] == 200
            elif kind == "llm":
                ollama_ok, model_ok = result
    organize = _organize_status()
    host = _host_metrics()
    return {
        "ok": plex_ok and vpn_ok and qbit_ok and ollama_ok and organize["ok"],
        "plex": plex_ok,
        "vpn": vpn_ok,
        "qbit": qbit_ok,
        "ollama": ollama_ok,
        "model": OLLAMA_MODEL,
        "model_ready": model_ok,
        "organize": organize["ok"],
        "organize_busy": organize["busy"],
        "organize_pending": organize["pending"],
        "organize_error": organize["error"],
        "host": host,
        "stack": [
            {"id": "plex", "label": "Plex", "ok": plex_ok},
            {"id": "vpn", "label": "VPN", "ok": vpn_ok},
            {"id": "qbit", "label": "qBit", "ok": qbit_ok},
            {"id": "llm", "label": "LLM", "ok": ollama_ok and model_ok},
            {
                "id": "organize",
                "label": "File",
                "ok": organize["ok"],
                "busy": organize["busy"],
            },
        ],
    }


def _search_quip() -> str:
    """Rotate-ish: random line, never the same as the previous search."""
    global _last_quip
    n = len(SEARCH_QUIPS)
    with _quip_lock:
        if n <= 1:
            return SEARCH_QUIPS[0]
        if _last_quip < 0:
            idx = random.randrange(n)
        else:
            idx = random.randrange(n - 1)
            if idx >= _last_quip:
                idx += 1
        _last_quip = idx
        return SEARCH_QUIPS[idx]


def _genre_reply(genre: dict) -> dict:
    library = collect_library()
    source = library["shows"] if genre["kind"] == "tvSeries" else library["movies"]
    owned = {_title_key(str(row.get("title") or "")) for row in source}
    try:
        rows = suggest_genre(genre["genre"], genre["kind"], owned, limit=3)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="IMDb ratings are not on disk yet.") from exc
    label = genre["genre"].lower()
    noun = "shows" if genre["kind"] == "tvSeries" else "movies"
    category = "tv" if genre["kind"] == "tvSeries" else "movies"
    if not rows:
        summary = f"No {label} {noun} on IMDb rated over 7 that you don't already have."
        return {
            "reply": summary,
            "quip": "",
            "query": genre["genre"],
            "category": category,
            "kind": "suggest",
            "results": [],
        }
    summary = f"IMDb {label} {noun} rated over 7, newest first. Pick one to search."
    return {
        "reply": summary,
        "quip": "",
        "query": genre["genre"],
        "category": category,
        "kind": "suggest",
        "results": [
            {
                "title": row["title"],
                "year": row["year"],
                "rating": row["rating"],
                "votes": row["votes"],
            }
            for row in rows
        ],
    }


@app.post("/api/chat")
def chat(body: ChatIn) -> dict:
    message = body.message.strip()
    genre = parse_genre_request(message)
    if genre:
        return _genre_reply(genre)
    parsed = fallback_parse(message)
    llm_error = ""
    try:
        parsed = _call_with_timeout(ollama_chat, LLM_TIMEOUT, message)
    except Exception as exc:
        llm_error = f"{type(exc).__name__}: {exc}"
        print(f"llm fallback: {llm_error}", flush=True)
    try:
        plan = plan_search(message, parsed["query"], parsed["category"])
        parsed["category"] = plan["category"]
        title = plan["title"]
        year = plan["year"]
        extra = plan["patterns"][1:]
        raw = run_search(plan["patterns"][0], plan["category"], year, extra_patterns=extra)
        picks = top_unique(
            raw, plan["category"], title=title, year=year, season=plan["season"]
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Search failed: {exc}") from exc
    note = ""
    if llm_error:
        note = " Local model was busy, so I searched your text as-is."
    shown = plan["display"]
    quip = _search_quip()
    print(f"search quip: {quip}", flush=True)
    if not picks:
        summary = f'Searched qBittorrent for “{shown}” and found no usable torrents.{note}'
        return {
            "reply": f"{summary}\n{quip}",
            "quip": quip,
            "query": shown,
            "category": parsed["category"],
            "results": [],
        }
    year_note = ""
    if year:
        year_note = " Matches for that year are listed first."
        if plan["year_from"] == "imdb" and plan["user_year"] and plan["user_year"] != year:
            year_note = f" IMDb lists this as {year}, so that year is listed first."
    summary = (
        f'Searched qBittorrent for “{shown}” '
        f'({parsed["category"]}). Pick one of the top seeded matches.{year_note}{note}'
    )
    return {
        "reply": f"{summary}\n{quip}",
        "quip": quip,
        "query": shown,
        "category": parsed["category"],
        "results": picks,
    }


@app.post("/api/download")
def download(body: DownloadIn) -> dict:
    with _RESULTS_LOCK:
        _prune_results()
        item = RESULTS.get(body.id)
    if not item:
        raise HTTPException(status_code=404, detail="That pick expired. Search again.")
    existing = _find_torrent(item["name"], item["url"])
    if existing:
        payload = _status_payload(existing)
        payload["already"] = True
        return payload
    mapped = add_category(str(item.get("category") or ""))
    data = {"urls": item["url"]}
    if mapped:
        data["category"] = mapped
    code, text = qbit("POST", "/api/v2/torrents/add", data, timeout=30)
    if code == 409:
        info = _find_torrent(item["name"], item["url"])
        if not info:
            raise HTTPException(
                status_code=409,
                detail="That torrent is already in qBittorrent.",
            )
        payload = _status_payload(info)
        payload["already"] = True
        return payload
    if code != 200:
        raise HTTPException(status_code=502, detail=f"qBit add failed ({code}): {text[:200]}")
    info = _await_torrent(item["name"], item["url"])
    if not info:
        return {
            "hash": "",
            "name": item["name"],
            "progress": 0,
            "progress_pct": "0%",
            "eta": "starting",
            "save_path": "",
            "state": "queued",
        }
    return _status_payload(info)


@app.get("/api/downloads")
def downloads() -> dict:
    code, text = qbit("GET", "/api/v2/torrents/info", timeout=15)
    if code != 200:
        raise HTTPException(status_code=502, detail=f"qBit list failed ({code})")
    rows = json.loads(text) or []
    active = [_status_payload(row) for row in rows if _is_downloading(row)]
    active.sort(key=lambda row: (-row.get("dlspeed", 0), row.get("name", "").lower()))
    organize = _organize_status()
    return {
        "ok": True,
        "count": len(active),
        "items": active,
        "filing": organize["pending"],
        "organize_ok": organize["ok"],
    }


@app.get("/api/library")
def library() -> dict:
    global _LIBRARY_CACHE
    now = time.time()
    if _LIBRARY_CACHE and _LIBRARY_CACHE[0] > now:
        return _LIBRARY_CACHE[1]
    payload = collect_library()
    _LIBRARY_CACHE = (now + LIBRARY_CACHE_SEC, payload)
    return payload


@app.get("/api/status")
def status(hash: str = "") -> dict:
    if not hash:
        raise HTTPException(status_code=400, detail="Missing hash")
    code, text = qbit("GET", f"/api/v2/torrents/info?hashes={urllib.parse.quote(hash)}", timeout=15)
    if code != 200:
        raise HTTPException(status_code=502, detail=f"qBit status failed ({code})")
    rows = json.loads(text) or []
    if not rows:
        raise HTTPException(status_code=404, detail="Torrent not found")
    return _status_payload(rows[0])


def _await_torrent(name: str, url: str, tries: int = 4, pause: float = 0.4) -> dict | None:
    for attempt in range(max(1, tries)):
        if attempt:
            time.sleep(pause)
        info = _find_torrent(name, url)
        if info:
            return info
    return None


def _find_torrent(name: str, url: str) -> dict | None:
    code, text = qbit("GET", "/api/v2/torrents/info", timeout=15)
    if code != 200:
        return None
    rows = json.loads(text) or []
    magnet_hash = ""
    match = re.search(r"btih:([a-fA-F0-9]{32,40})", url)
    if match:
        magnet_hash = match.group(1).lower()
    want = norm_name(name)
    for row in reversed(rows):
        row_hash = str(row.get("hash") or "").lower()
        if magnet_hash and row_hash.startswith(magnet_hash):
            return row
        if want and norm_name(str(row.get("name") or "")) == want:
            return row
    return None


def _status_payload(info: dict) -> dict:
    progress = float(info.get("progress") or 0)
    eta = int(info.get("eta") or -1)
    state = str(info.get("state") or "")
    save_path = str(info.get("save_path") or info.get("content_path") or "")
    amount_left = int(info.get("amount_left") or 0)
    if progress >= 0.999 or amount_left == 0:
        eta_label = "done"
    else:
        eta_label = human_eta(eta)
    return {
        "hash": str(info.get("hash") or ""),
        "name": str(info.get("name") or ""),
        "progress": progress,
        "progress_pct": f"{progress * 100:.1f}%",
        "eta": eta_label,
        "save_path": save_path,
        "state": state,
        "dlspeed": int(info.get("dlspeed") or 0),
        "speed_label": human_speed(int(info.get("dlspeed") or 0)),
        "size_label": human_size(int(info.get("size") or 0)),
        "category": str(info.get("category") or ""),
    }


def human_speed(bps: int) -> str:
    if bps is None or bps <= 0:
        return "0 B/s"
    return f"{human_size(bps)}/s"


def _is_downloading(row: dict) -> bool:
    state = str(row.get("state") or "")
    if state in ACTIVE_DL:
        return True
    progress = float(row.get("progress") or 0)
    left = int(row.get("amount_left") or 0)
    if state in {"error", "missingFiles"}:
        return True
    if progress >= 0.999 or left == 0:
        return False
    return not state.endswith("UP")


def _clean_title(name: str) -> str:
    text = name.replace(".", " ").replace("_", " ")
    text = SEASON_RE.sub("", text)
    text = JUNK_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -[]")
    text = re.sub(r"\s*\(\d{4}$", "", text)
    text = re.sub(r"\s*\(\s*$", "", text)
    return text.strip(" -") or name


def _season_num(name: str) -> int | None:
    match = re.search(r"(?i)(?:season[.\-_ ]*(\d{1,2})|\bS(\d{1,2})\b)", name)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _year_of(name: str) -> int | None:
    return extract_year(name)


def _iter_entries(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if path.name.startswith("."):
            continue
        if path.is_dir() or path.suffix.lower() in VIDEO_EXT:
            out.append(path)
    return out


def _season_total(nested: int, season_nums: set[int]) -> int:
    count = nested
    if season_nums:
        count = max(count, max(season_nums), len(season_nums))
    return count or 1


def collect_library() -> dict:
    movies: dict[str, dict] = {}
    for path in _iter_entries(MOVIE_DIR):
        raw_name = path.stem if path.is_file() else path.name
        title = _clean_title(raw_name)
        key = title.lower()
        year = _year_of(path.name)
        current = movies.get(key)
        if current is None or len(raw_name) < len(current["raw"]):
            movies[key] = {
                "title": title,
                "year": year or (current or {}).get("year"),
                "raw": raw_name,
            }
        elif year and not current.get("year"):
            current["year"] = year
    shows: dict[str, dict] = {}
    for path in _iter_entries(TV_DIR):
        title = _clean_title(path.name)
        key = title.lower()
        season = _season_num(path.name)
        nested = 0
        if path.is_dir():
            nested = sum(1 for child in path.iterdir() if child.is_dir() and _season_num(child.name))
        current = shows.get(key)
        if current is None:
            shows[key] = {
                "title": title,
                "year": _year_of(path.name),
                "nested": nested,
                "season_nums": {season} if season else set(),
            }
        else:
            current["nested"] = max(current["nested"], nested)
            if season:
                current["season_nums"].add(season)
            if not current.get("year"):
                current["year"] = _year_of(path.name)
    movie_rows = sorted(movies.values(), key=lambda row: row["title"].lower())
    show_rows = []
    for row in shows.values():
        show_rows.append({
            "title": row["title"],
            "year": row.get("year"),
            "seasons": _season_total(row["nested"], row["season_nums"]),
        })
    show_rows.sort(key=lambda row: row["title"].lower())
    return {
        "ok": True,
        "source": "disk",
        "movies": movie_rows,
        "shows": show_rows,
        "movie_count": len(movie_rows),
        "show_count": len(show_rows),
    }


def latest_daily_report() -> dict | None:
    path = IMDB_DATA_DIR / "state.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    days = data.get("days") if isinstance(data, dict) else None
    if not isinstance(days, dict) or not days:
        return None
    day = max(str(key) for key in days)
    rows = days.get(day) or []
    if not isinstance(rows, list) or not rows:
        return None
    picks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        picks.append(
            {
                "title": title,
                "year": row.get("year"),
                "status": str(row.get("status") or ""),
            }
        )
    if not picks:
        return None
    return {"day": day, "picks": picks}


@app.get("/api/imdb-daily")
def imdb_daily_report() -> dict:
    report = latest_daily_report()
    if not report:
        return {"ok": True, "day": "", "picks": []}
    return {"ok": True, "day": report["day"], "picks": report["picks"]}


def _serve() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "7680"))
    lan = os.environ.get("PLEX_CHAT_BIND", "127.0.0.1").strip() or "127.0.0.1"

    def run(host: str) -> None:
        uvicorn.run(app, host=host, port=port, log_level="info")

    if lan != "127.0.0.1":
        threading.Thread(
            target=run, args=("127.0.0.1",), daemon=True, name="chat-loopback"
        ).start()
    run(lan)


if __name__ == "__main__":
    _serve()
