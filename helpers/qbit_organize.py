#!/usr/bin/env python3
"""After a qBittorrent download finishes: stop seeding, file it, scan Plex.

Names everything the way Plex expects, using IMDb for the official title/year:

  Movies/Title (Year)/Title (Year).ext
  TV/Show (Year)/Season 01/Show (Year) - S01E01 - Episode Title.ext

Two roles share a state directory because the processes cannot share a network
namespace: qBit's API is localhost inside Gluetun, Plex is on the host.

  ROLE=organize  (network_mode: service:gluetun)
      Poll completed torrents, delete them from qBit (keep files), move
      leftover Downloads items into TV or Movies with canonical names, queue
      a Plex scan.
  ROLE=scan      (network_mode: host)
      Drain the scan queue against localhost Plex.
  ROLE=library   (or --library)
      Rename the existing TV/Movies library, then exit.

Skip a torrent by tagging it keep-seed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROLE = os.environ.get("ROLE", "organize").strip().lower()
INTERVAL = int(os.environ.get("INTERVAL_SEC", "20"))
GRACE_SEC = int(os.environ.get("COMPLETION_GRACE_SEC", "20"))
STATE_PATH = Path(os.environ.get("STATE_PATH", "/var/lib/qbit-organize/state.json"))
SCAN_DIR = Path(os.environ.get("SCAN_DIR", str(STATE_PATH.parent / "scans")))
QBIT_URL = os.environ.get("QBIT_URL", "http://127.0.0.1:8080").rstrip("/")
QBIT_USER = os.environ.get("QBIT_USER", "").strip()
QBIT_PASS = os.environ.get("QBIT_PASS", "")
PLEX_URL = os.environ.get("PLEX_URL", "http://192.168.1.8:32400").rstrip("/")
PLEX_PREFS = os.environ.get("PLEX_PREFS", "/prefs.xml")
TV_DIR = Path(os.environ.get("TV_DIR", "/mnt/usbdrive/TV"))
MOVIE_DIR = Path(os.environ.get("MOVIE_DIR", "/mnt/usbdrive/Movies"))
MOVE_UID = int(os.environ.get("MOVE_UID", "1000"))
MOVE_GID = int(os.environ.get("MOVE_GID", "1000"))
TV_CATEGORY = os.environ.get("QBIT_TV_CATEGORY", "TV").strip().lower()
MOVIE_CATEGORY = os.environ.get("QBIT_MOVIE_CATEGORY", "Movies").strip().lower()
SKIP_TAG = os.environ.get("SKIP_TAG", "keep-seed").strip().lower()
IMDB_CACHE_SEC = int(os.environ.get("IMDB_CACHE_SEC", "2592000"))
MIN_TV_BYTES = int(os.environ.get("MIN_TV_BYTES", str(8 * 1024 * 1024)))
MIN_MOVIE_BYTES = int(os.environ.get("MIN_MOVIE_BYTES", str(40 * 1024 * 1024)))

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".wmv"}
SIDECAR_EXT = {".srt", ".ass", ".ssa", ".idx", ".sub", ".smi"}
JUNK_EXT = {".nfo", ".txt", ".jpg", ".jpeg", ".png", ".url", ".sfv", ".md5", ".torrent"}
DONE_STATES = {
    "uploading",
    "stalledUP",
    "pausedUP",
    "queuedUP",
    "checkingUP",
    "forcedUP",
    "stoppedUP",
}
SKIP_STATES = {
    "downloading",
    "metaDL",
    "stalledDL",
    "queuedDL",
    "checkingDL",
    "allocating",
    "moving",
    "error",
}
IMDB_MOVIE = {"movie", "tvMovie", "short", "video"}
IMDB_TV = {"tvSeries", "tvMiniSeries", "tvSpecial", "tvShort"}
TITLE_STOP = {"the", "a", "an", "of", "and", "in"}

EPISODE_RE = re.compile(
    r"(?i)(?:\bS\d{1,2}[.\-_ ]?E\d{1,2}\b|\b\d{1,2}x\d{2}\b)"
)
SEASON_RE = re.compile(
    r"(?i)(?:\bSeason[.\-_ ]?\d{1,2}\b|\bComplete[.\-_ ]+Season\b|\bS\d{1,2}\b(?![.\-_ ]?E\d))"
)
YEAR_RE = re.compile(r"(?:^|[.\s_\-(])((?:19|20)\d{2})(?:[.\s_\-)]|$)")
GENERIC_FOLDER_RE = re.compile(r"(?i)^(?:season[.\-_ ]?\d{1,2}|s\d{1,2}|complete|specials|extras|subs?)$")
QUALITY_TAIL_RE = re.compile(
    r"(?i)[.\-_ ](?:720p|1080p|2160p|4k|uhd|hdr|bluray|web[-_.]?dl|webrip|hdtv|"
    r"x264|x265|hevc|aac|ac3|dts|etrg|yts|yify|rarbg|sparks|lol).*$"
)
JUNK_TAIL_RE = re.compile(
    r"(?i)[\s.\-_\[(]*\b(?:720p|1080p|2160p|4k|uhd|hdr10\+?|hdr|bluray|blu-ray|"
    r"web-?dl|webrip|hdtv|hdrip|bdrip|brrip|dvdrip|telesync|\bts\b|\bcam\b|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|10bit|8bit|aac|ac3|ddp?5?|dts|truehd|"
    r"atmos|5\.1|6ch|7\.1|amzn|dsnp|hmax|\bnf\b|atvp|"
    r"complete|repack|retail|uncut|remastered|extended|internal|proper|"
    r"limited|multi|subs?|dksubs|nosub|ntsc|pal|\bdvd\b|"
    r"\byts\b|\byify\b|\betrg\b|\btgx\b|\beztv\b|\brarbg\b|\bntb\b|"
    r"\bgalaxytv\b|\bpsa\b|\bbone\b|\bvppv\b|\bjoy\b|"
    r"re-?blurip).*$"
)
SITE_PREFIX_RE = re.compile(
    r"(?i)^(?:www\.[^\s/]+|uindex\.org)\s*[-–—:]+\s*"
)
SEASON_TAIL_RE = re.compile(
    r"(?i)[.\-_ ]*(?:complete[.\-_ ]*)?(?:season[.\-_ ]*\d{1,2}|\bS\d{1,2}\b).*$"
)
EPISODE_PARSE_RE = re.compile(
    r"""(?ix)
    (?:
        \bS(?P<s1>\d{1,2})[.\-_ ]?E(?P<e1>\d{1,3})
        (?:
            E(?P<e1b>\d{1,3})
          | -E(?P<e1c>\d{1,3})
          | -(?P<e1d>\d{1,3})(?!\d)
        )?
      |
        \bS(?P<s3>\d{1,2})[.\-_ ]?Ep(?:isode)?[.\-_ ]?(?P<e3>\d{1,3})
      |
        \b(?P<s2>\d{1,2})x(?P<e2>\d{2})
        (?:
            -x?(?P<e2b>\d{2})
        )?
    )
    """
)
SAMPLE_RE = re.compile(r"(?i)(?:\bsample\b|\btrailer\b|\bscreenshots?\b)")
GENERIC_VIDEO_RE = re.compile(r"(?i)^(?:etrg|rarbg|yts|yify|www)(?:\.\w+)?$")
BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
YEAR_TOKEN_RE = re.compile(r"(?:\(|\b)((?:19|20)\d{2})(?:\)|\b)")

COOKIES = CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIES))
LOGGED_IN = False
UNKNOWN = set()
_IMDB_CACHE: dict[str, tuple[float, dict | None]] = {}
_IMDB_LOADED = False


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def log(msg: str) -> None:
    print(msg, flush=True)


def qbit_request(method: str, path: str, data: dict | None = None) -> tuple[int, str]:
    body = None if data is None else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        f"{QBIT_URL}{path}",
        data=body,
        method=method,
        headers={
            "Referer": f"{QBIT_URL}/",
            "Origin": QBIT_URL,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with OPENER.open(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def qbit_login() -> None:
    global LOGGED_IN
    if LOGGED_IN or not QBIT_USER:
        return
    code, text = qbit_request(
        "POST",
        "/api/v2/auth/login",
        {"username": QBIT_USER, "password": QBIT_PASS},
    )
    if code != 200 or text.strip().lower() != "ok.":
        raise RuntimeError(f"qBit login failed ({code})")
    LOGGED_IN = True


def qbit(method: str, path: str, data: dict | None = None) -> tuple[int, str]:
    qbit_login()
    code, text = qbit_request(method, path, data)
    if code in {401, 403} and QBIT_USER:
        global LOGGED_IN
        LOGGED_IN = False
        qbit_login()
        code, text = qbit_request(method, path, data)
    return code, text


def plex_token() -> str:
    token = os.environ.get("PLEX_TOKEN", "").strip()
    if token and token != "change-me":
        return token
    prefs = Path(PLEX_PREFS)
    if prefs.is_file():
        match = re.search(r'PlexOnlineToken="([^"]+)"', prefs.read_text(errors="replace"))
        if match:
            return match.group(1)
    return ""


def plex_get(path: str, timeout: int = 12) -> dict | None:
    token = plex_token()
    if not token:
        return None
    url = f"{PLEX_URL}{path}"
    sep = "&" if "?" in path else "?"
    url = f"{url}{sep}X-Plex-Token={urllib.parse.quote(token)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        log(f"plex {path} failed: {type(exc).__name__}: {exc}")
        return None
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"pending": {}, "done": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def imdb_cache_path() -> Path:
    return STATE_PATH.parent / "imdb-cache.json"


def load_imdb_cache() -> None:
    global _IMDB_LOADED
    if _IMDB_LOADED:
        return
    _IMDB_LOADED = True
    try:
        data = json.loads(imdb_cache_path().read_text())
    except (OSError, json.JSONDecodeError):
        return
    now = time.time()
    for key, row in (data or {}).items():
        if not isinstance(row, list) or len(row) != 2:
            continue
        expires, hit = row
        if isinstance(expires, (int, float)) and expires > now:
            _IMDB_CACHE[key] = (expires, hit)


def save_imdb_cache() -> None:
    path = imdb_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {key: [expires, hit] for key, (expires, hit) in _IMDB_CACHE.items()}
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def is_video(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXT


def looks_tv(text: str) -> bool:
    return bool(EPISODE_RE.search(text) or SEASON_RE.search(text))


def looks_movie(text: str) -> bool:
    return bool(YEAR_RE.search(text)) and not looks_tv(text)


def classify(name: str, category: str, files: list[str]) -> str | None:
    blob = " ".join([name, *files[:80]])
    videos = [f for f in files if is_video(f)]
    episode_hits = sum(1 for f in videos if EPISODE_RE.search(Path(f).name))
    if looks_tv(name) or episode_hits >= 2 or (episode_hits == 1 and len(videos) == 1):
        return "tv"
    if looks_movie(name) or (len(videos) == 1 and looks_movie(Path(videos[0]).name)):
        return "movies"
    if looks_tv(blob):
        return "tv"
    cat = (category or "").strip().lower()
    if cat == TV_CATEGORY or cat in {"tv", "television", "sonarr"}:
        return "tv"
    if cat == MOVIE_CATEGORY or cat in {"movies", "movie", "radarr"}:
        return "movies"
    return None


def dest_name(torrent_name: str, content_path: Path) -> str:
    base = content_path.name.strip() or torrent_name
    if content_path.is_file() or GENERIC_FOLDER_RE.match(base):
        cleaned = QUALITY_TAIL_RE.sub("", torrent_name).strip(" ._")
        cleaned = re.sub(r"\s+", " ", cleaned) or torrent_name
        if content_path.is_file():
            suffix = content_path.suffix
            if suffix and not cleaned.lower().endswith(suffix.lower()):
                cleaned = f"{cleaned}{suffix}"
        return cleaned
    return base


def under_dir(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def in_incomplete(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return "incomplete" in parts


def already_filed(path: Path) -> str | None:
    if under_dir(path, TV_DIR):
        return "tv"
    if under_dir(path, MOVIE_DIR):
        return "movies"
    return None


def unique_dest(root: Path, name: str) -> Path:
    dest = root / name
    if not dest.exists():
        return dest
    raise FileExistsError(str(dest))


def chown_tree(path: Path) -> None:
    try:
        os.chown(path, MOVE_UID, MOVE_GID)
        if path.is_dir():
            for child in path.rglob("*"):
                os.chown(child, MOVE_UID, MOVE_GID)
    except OSError:
        pass


def move_content(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dest)
    except OSError:
        shutil.move(str(src), str(dest))
    chown_tree(dest)


def torrent_files(hash_: str) -> list[str]:
    code, text = qbit("GET", f"/api/v2/torrents/files?hash={urllib.parse.quote(hash_)}")
    if code != 200:
        return []
    try:
        rows = json.loads(text) or []
    except json.JSONDecodeError:
        return []
    return [str(row.get("name") or "") for row in rows if row.get("name")]


def delete_torrent(hash_: str) -> None:
    code, text = qbit(
        "POST",
        "/api/v2/torrents/delete",
        {"hashes": hash_, "deleteFiles": "false"},
    )
    if code != 200:
        raise RuntimeError(f"qBit delete {hash_[:8]} -> {code}: {text[:200]}")


def plex_scan(kind: str) -> bool:
    want = "show" if kind == "tv" else "movie"
    data = plex_get("/library/sections")
    if data is None:
        log("plex scan skipped (unreachable or no token)")
        return False
    dirs = as_list((data.get("MediaContainer") or {}).get("Directory"))
    scanned = 0
    for item in dirs:
        if str(item.get("type") or "") != want:
            continue
        key = item.get("key")
        if key is None:
            continue
        plex_get(f"/library/sections/{key}/refresh")
        scanned += 1
        log(f"plex scan {item.get('title') or want} ({key})")
    if not scanned:
        log(f"plex scan: no {want} library section")
        return False
    return True


def is_complete(row: dict) -> bool:
    state = str(row.get("state") or "")
    if state in SKIP_STATES:
        return False
    progress = float(row.get("progress") or 0)
    left = int(row.get("amount_left") or 0)
    if progress < 0.999 and left > 0:
        return False
    if state not in DONE_STATES and progress < 1:
        return False
    done_at = int(row.get("completion_on") or 0)
    if done_at and time.time() - done_at < GRACE_SEC:
        return False
    return True


def tags_of(row: dict) -> set[str]:
    raw = str(row.get("tags") or "")
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def queue_scan(kind: str, dest: Path) -> None:
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    path = SCAN_DIR / f"{int(time.time() * 1000)}-{kind}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"kind": kind, "dest": str(dest)}))
    tmp.replace(path)


def finish_job(state: dict, hash_: str, kind: str, dest: Path) -> None:
    state.get("pending", {}).pop(hash_, None)
    state.setdefault("done", {})[hash_] = {"kind": kind, "dest": str(dest), "ts": time.time()}
    save_state(state)
    queue_scan(kind, dest)


def dots_to_spaces(text: str) -> str:
    if text.count(".") >= 2 and " " not in text:
        return text.replace(".", " ")
    return text.replace(".", " ") if text.count(".") > text.count(" ") else text


def extract_year(text: str) -> int | None:
    years = [int(y) for y in YEAR_TOKEN_RE.findall(text or "")]
    return years[-1] if years else None


def title_tokens(title: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", title.lower())
    kept = [t for t in tokens if t not in TITLE_STOP and len(t) > 1]
    return kept or tokens


def norm_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def title_matches(name: str, title: str) -> bool:
    tokens = title_tokens(title)
    if not tokens:
        return True
    blob = norm_name(name)
    hits = sum(1 for token in tokens if token in blob)
    need = len(tokens) if len(tokens) <= 3 else len(tokens) - 1
    return hits >= need


def safe_name(text: str) -> str:
    text = (text or "").replace(":", " -")
    text = BAD_CHARS_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .")


def _strip_year_tokens(text: str) -> str:
    years = list(YEAR_TOKEN_RE.finditer(text or ""))
    if not years:
        return (text or "").strip(" -")
    without = re.sub(r"\s+", " ", YEAR_TOKEN_RE.sub(" ", text)).strip(" -")
    if without:
        return without
    last = years[-1]
    trimmed = re.sub(r"\s+", " ", f"{text[: last.start()]} {text[last.end() :]}").strip(" -")
    return trimmed or last.group(1)


def clean_title(text: str) -> str:
    text = SITE_PREFIX_RE.sub("", text or "")
    text = dots_to_spaces(text)
    text = text.replace("_", " ")
    text = re.sub(r"[\[\]{}]", " ", text)
    text = SEASON_TAIL_RE.sub("", text)
    text = JUNK_TAIL_RE.sub("", text)
    text = re.sub(r"\s*\((?:19|20)\d{2}\)\s*", " ", text)
    text = _strip_year_tokens(text)
    text = re.sub(r"\s+", " ", text).strip(" -")
    text = re.sub(r"\s*\(\s*$", "", text)
    return safe_name(text)


def parse_episode(name: str) -> dict | None:
    stem = Path(name).name
    while Path(stem).suffix.lower() in VIDEO_EXT | SIDECAR_EXT:
        stem = Path(stem).stem
    match = EPISODE_PARSE_RE.search(stem)
    if not match:
        return None
    season = int(match.group("s1") or match.group("s2") or match.group("s3"))
    episode = int(match.group("e1") or match.group("e2") or match.group("e3"))
    end_raw = match.group("e1b") or match.group("e1c") or match.group("e1d") or match.group("e2b")
    episode_end = int(end_raw) if end_raw else None
    if episode_end is not None and episode_end <= episode:
        episode_end = None
    before = clean_title(stem[: match.start()])
    after = stem[match.end() :]
    after = JUNK_TAIL_RE.sub("", after)
    after = re.sub(r"^[\s.\-_–—]+", "", after)
    after = after.replace(".", " ").replace("_", " ")
    after = re.sub(r"\s+", " ", after).strip(" -")
    if after.lower() in {"", "episode", "part"} or JUNK_TAIL_RE.match(after):
        after = ""
    return {
        "season": season,
        "episode": episode,
        "episode_end": episode_end,
        "show_hint": before,
        "episode_title": safe_name(after) if after else "",
    }


def season_tag(season: int, episode: int, episode_end: int | None) -> str:
    tag = f"S{season:02d}E{episode:02d}"
    if episode_end:
        tag += f"-E{episode_end:02d}"
    return tag


def is_sample(path: Path, kind: str) -> bool:
    name = path.name
    if name.startswith("._") or GENERIC_VIDEO_RE.match(Path(name).stem):
        return True
    if SAMPLE_RE.search(name):
        return True
    try:
        size = path.stat().st_size
    except OSError:
        return True
    minimum = MIN_TV_BYTES if kind == "tv" else MIN_MOVIE_BYTES
    return size < minimum


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
    with urllib.request.urlopen(req, timeout=8) as resp:
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


def _imdb_score(row: dict, title: str, prefer_year: int | None) -> tuple:
    want = title_tokens(title)
    got = title_tokens(row["title"])
    exact = norm_name(row["title"]) == norm_name(title)
    token_hit = sum(1 for token in want if token in got) if want else 0
    year_hit = 0 if prefer_year and row["year"] == prefer_year else 1
    return (
        0 if exact else 1,
        year_hit,
        -token_hit,
        row["rank"],
        abs((prefer_year or row["year"]) - row["year"]),
    )


def imdb_lookup(title: str, category: str, prefer_year: int | None = None) -> dict | None:
    load_imdb_cache()
    key = f"{norm_name(title)}|{category}|{prefer_year or ''}"
    now = time.time()
    cached = _IMDB_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    hit = None
    try:
        rows = _imdb_fetch(title)
        kind = _imdb_kind(category)
        if kind:
            typed = [row for row in rows if row["qid"] in kind]
            rows = typed or rows
        plausible = []
        for row in rows:
            # IMDb title must contain the query tokens. The reverse check would
            # accept TV's "Chuck" for the movie "Chuck Chuck Baby".
            if title_matches(row["title"], title):
                plausible.append(row)
        if plausible:
            plausible.sort(key=lambda row: _imdb_score(row, title, prefer_year))
            hit = plausible[0]
            log(f"imdb {title!r} -> {hit['title']} ({hit['year']}) {hit['id']}")
    except Exception as exc:
        log(f"imdb lookup failed for {title!r}: {type(exc).__name__}: {exc}")
    _IMDB_CACHE[key] = (now + IMDB_CACHE_SEC, hit)
    try:
        save_imdb_cache()
    except OSError:
        pass
    return hit


def resolve_identity(title: str, kind: str, year: int | None) -> tuple[str, int | None]:
    cleaned = clean_title(title) or title
    if not cleaned:
        return title, year
    hit = imdb_lookup(cleaned, kind, prefer_year=year)
    if not hit:
        return safe_name(cleaned), year
    if year and hit.get("year") != year:
        return safe_name(hit["title"] if title_matches(hit["title"], cleaned) and len(title_tokens(cleaned)) > 2 else cleaned), year
    return safe_name(hit["title"]), hit.get("year") or year


def parent_show_hints(path: Path, root: Path) -> list[str]:
    hints = []
    try:
        rel = path.resolve().relative_to(root.resolve())
        parts = list(rel.parts[:-1])
    except (OSError, ValueError):
        parts = [p.name for p in path.parents]
    for part in reversed(parts):
        if GENERIC_FOLDER_RE.match(part):
            continue
        cleaned = clean_title(part)
        if cleaned and cleaned.lower() not in {h.lower() for h in hints}:
            hints.append(cleaned)
    return hints


def best_imdb(hints: list[str], kind: str, year: int | None) -> dict | None:
    best = None
    best_key = None
    seen = set()
    for hint in hints:
        cleaned = clean_title(hint) if hint else ""
        if not cleaned:
            continue
        key_id = cleaned.lower()
        if key_id in seen:
            continue
        seen.add(key_id)
        hit = imdb_lookup(cleaned, kind, prefer_year=year)
        if not hit:
            continue
        score = _imdb_score(hit, cleaned, year) + (-len(title_tokens(cleaned)),)
        if best is None or score < best_key:
            best = hit
            best_key = score
    return best


def guess_show(path: Path) -> tuple[str, int | None]:
    parsed = parse_episode(path.name) or {}
    year = extract_year(parsed.get("show_hint") or "")
    hints = parent_show_hints(path, TV_DIR)
    if parsed.get("show_hint"):
        hints.append(parsed["show_hint"])
    for parent in path.parents:
        if parent in {TV_DIR, TV_DIR.parent, Path("/")}:
            break
        if parent.parent == TV_DIR and re.fullmatch(r".+ \((?:19|20)\d{2}\)", parent.name):
            continue
        year = year or extract_year(parent.name)
    hit = best_imdb(hints, "tv", year)
    if hit:
        if year and hit.get("year") != year and len(title_tokens(hit["title"])) <= 2:
            return safe_name(hit["title"]), year
        return safe_name(hit["title"]), hit.get("year") or year
    if hints:
        return safe_name(hints[0]), year
    return resolve_identity(path.parent.name, "tv", year)


def guess_movie(path: Path) -> tuple[str, int | None]:
    if path.parent != MOVIE_DIR and path.parent.is_dir():
        source = path.parent.name
    else:
        source = path.stem
        while Path(source).suffix.lower() in VIDEO_EXT:
            source = Path(source).stem
    year = extract_year(source) or extract_year(path.name)
    return resolve_identity(source, "movies", year)


def episode_filename(show: str, year: int | None, parsed: dict, suffix: str) -> str:
    tagged = season_tag(parsed["season"], parsed["episode"], parsed.get("episode_end"))
    base = f"{show} ({year}) - {tagged}" if year else f"{show} - {tagged}"
    if parsed.get("episode_title"):
        base = f"{base} - {parsed['episode_title']}"
    return f"{base}{suffix}"


def movie_filename(title: str, year: int | None, suffix: str) -> str:
    base = f"{title} ({year})" if year else title
    return f"{base}{suffix}"


def canonical_video_dest(path: Path, kind: str) -> Path | None:
    suffix = path.suffix.lower()
    if kind == "tv":
        parsed = parse_episode(path.name)
        if not parsed:
            return None
        show, year = guess_show(path)
        season_dir = f"Season {parsed['season']:02d}"
        return TV_DIR / f"{show} ({year})" / season_dir / episode_filename(show, year, parsed, suffix) if year else TV_DIR / show / season_dir / episode_filename(show, year, parsed, suffix)
    title, year = guess_movie(path)
    folder = f"{title} ({year})" if year else title
    return MOVIE_DIR / folder / movie_filename(title, year, suffix)


def sidecar_dest(video: Path, sidecar: Path, dest: Path) -> Path:
    stem = video.stem
    name = sidecar.name
    if name.lower().startswith(stem.lower()):
        tail = name[len(stem) :]
        return dest.with_name(dest.stem + tail)
    extra = sidecar.stem.strip()
    if extra and extra.lower() != dest.stem.lower():
        return dest.with_name(f"{dest.stem}.{extra}{sidecar.suffix.lower()}")
    return dest.with_suffix(sidecar.suffix)


def list_sidecars(video: Path) -> list[Path]:
    found = []
    seen: set[str] = set()
    stem = video.stem.lower()
    parsed = parse_episode(video.name)
    tag = ""
    if parsed:
        tag = season_tag(parsed["season"], parsed["episode"], parsed.get("episode_end")).lower()
    try:
        sibling_videos = [
            p
            for p in video.parent.iterdir()
            if p.is_file() and is_video(p.name) and p != video and not p.name.startswith("._")
        ]
    except OSError:
        sibling_videos = []
    only_video = not sibling_videos
    parents = [video.parent]
    subs = video.parent / "Subs"
    if subs.is_dir():
        parents.append(subs)
    subtitles = video.parent / "Subtitles"
    if subtitles.is_dir():
        parents.append(subtitles)
    for folder in parents:
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        try:
            folder_is_parent = folder.resolve() == video.parent.resolve()
        except OSError:
            folder_is_parent = folder == video.parent
        for item in entries:
            if not item.is_file() or item.suffix.lower() not in SIDECAR_EXT:
                continue
            if item.name.startswith("._"):
                continue
            low = item.name.lower()
            take = False
            if item.stem.lower() == stem or low.startswith(stem):
                take = True
            elif tag and tag in low.replace(" ", ""):
                take = True
            elif only_video and not folder_is_parent:
                take = True
            if not take:
                continue
            key = str(item)
            if key in seen:
                continue
            seen.add(key)
            found.append(item)
    return found


def same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def real_videos(root: Path, kind: str) -> list[Path]:
    return [p for p in iter_videos(root) if not is_sample(p, kind)]


def uncanonical_videos(root: Path, kind: str) -> list[Path]:
    if not root.exists():
        return []
    leftover = []
    for video in real_videos(root, kind):
        dest = canonical_video_dest(video, kind)
        if dest is None:
            continue
        if not same_file(video, dest):
            leftover.append(video)
    return leftover


def can_finish_job(src: Path, kind: str, moved: int, conflicts: int) -> bool:
    if conflicts:
        return False
    leftover = uncanonical_videos(src, kind) if src.exists() else []
    if leftover:
        return False
    if not src.exists():
        return True
    if real_videos(src, kind):
        return True
    # Folder is still there with no videos. That is a success only after a
    # move; an empty pre-video directory must stay pending (qBit race).
    return moved > 0


def move_file(src: Path, dest: Path, do_chown: bool) -> str:
    if same_file(src, dest):
        return "ok"
    if dest.exists():
        try:
            if src.stat().st_size == dest.stat().st_size:
                return "exists"
        except OSError:
            pass
        return "conflict"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dest)
    except OSError:
        shutil.move(str(src), str(dest))
    if do_chown:
        chown_tree(dest)
        chown_tree(dest.parent)
    return "moved"


def prune_empties(start: Path, stop: Path) -> None:
    try:
        current = start if start.is_dir() else start.parent
        stop_res = stop.resolve()
    except OSError:
        return
    while True:
        try:
            resolved = current.resolve()
            resolved.relative_to(stop_res)
        except (OSError, ValueError):
            return
        if resolved == stop_res:
            return
        try:
            children = list(current.iterdir())
        except OSError:
            return
        leftover = []
        for child in children:
            if child.name.startswith("._"):
                child.unlink(missing_ok=True)
                continue
            if child.is_file() and child.suffix.lower() in JUNK_EXT:
                child.unlink(missing_ok=True)
                continue
            leftover.append(child)
        if leftover:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def iter_videos(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if is_video(root.name) else []
    out = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and is_video(path.name) and not path.name.startswith("._"):
            out.append(path)
    return out


def qbit_protected_paths() -> set[Path]:
    paths: set[Path] = set()
    try:
        code, text = qbit("GET", "/api/v2/torrents/info")
        if code != 200:
            return paths
        rows = json.loads(text) or []
    except Exception:
        return paths
    for row in rows:
        for key in ("content_path", "save_path"):
            raw = str(row.get(key) or "")
            if not raw:
                continue
            try:
                paths.add(Path(raw).resolve())
            except OSError:
                paths.add(Path(raw))
    return paths


def is_protected(path: Path, protected: set[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for locked in protected:
        try:
            resolved.relative_to(locked)
            return True
        except ValueError:
            pass
        try:
            locked.relative_to(resolved)
            return True
        except ValueError:
            pass
    return False


def file_tree(src: Path, kind: str, do_chown: bool, dry_run: bool = False) -> tuple[Path, int, int, int]:
    videos = [p for p in iter_videos(src) if not is_sample(p, kind)]
    moved = skipped = conflicts = 0
    last_dest = src
    for video in videos:
        dest = canonical_video_dest(video, kind)
        if dest is None:
            skipped += 1
            log(f"skip unparsed {video}")
            continue
        last_dest = dest.parent if kind == "movies" else dest.parent.parent
        if dry_run:
            mark = "OK" if same_file(video, dest) else "MOVE"
            log(f"{mark} {video} -> {dest}")
            if mark == "MOVE":
                moved += 1
            continue
        try:
            status = move_file(video, dest, do_chown)
        except OSError as exc:
            skipped += 1
            log(f"failed {video}: {type(exc).__name__}: {exc}")
            continue
        if status == "conflict":
            conflicts += 1
            log(f"conflict {video} -> {dest}")
            continue
        if status == "moved":
            moved += 1
            log(f"renamed {dest.relative_to(TV_DIR if kind == 'tv' else MOVIE_DIR)}")
            for sidecar in list_sidecars(video):
                side_dest = sidecar_dest(video, sidecar, dest)
                side_status = move_file(sidecar, side_dest, do_chown)
                if side_status == "moved":
                    log(f"sidecar {side_dest.name}")
            prune_empties(video.parent, TV_DIR if kind == "tv" else MOVIE_DIR)
        elif status == "exists" and not same_file(video, dest):
            skipped += 1
            log(f"already have {dest.name}, left {video}")
    return last_dest, moved, skipped, conflicts


def _cleanup_src(src: Path, kind: str) -> None:
    if not src.exists():
        return
    leftover_videos = real_videos(src, kind)
    if leftover_videos:
        return
    root = TV_DIR if kind == "tv" else MOVIE_DIR
    prune_empties(src if src.is_dir() else src.parent, root)
    if not src.exists() or not src.is_dir():
        return
    try:
        resolved = src.resolve()
        if resolved in {root.resolve(), TV_DIR.resolve(), MOVIE_DIR.resolve()}:
            return
    except OSError:
        return
    try:
        shutil.rmtree(src, ignore_errors=True)
    except OSError:
        pass


def process_pending(state: dict) -> None:
    pending = state.get("pending") or {}
    for hash_, job in list(pending.items()):
        src = Path(job["src"])
        dest = Path(job["dest"])
        kind = job["kind"]
        if dest.exists() and not src.exists() and not job.get("canonical"):
            if not uncanonical_videos(dest, kind):
                log(f"filed {job.get('name') or dest.name} at {dest}")
                finish_job(state, hash_, kind, dest)
                continue
            src = dest
        if not src.exists():
            log(f"pending {hash_[:8]} source missing: {src}")
            continue
        filed = already_filed(src)
        last_dest, moved, skipped, conflicts = file_tree(
            src, kind, do_chown=not bool(filed)
        )
        if can_finish_job(src, kind, moved, conflicts):
            _cleanup_src(src, kind)
            log(f"filed {job.get('name')} -> {last_dest} moved={moved} skip={skipped} conflict={conflicts}")
            finish_job(state, hash_, kind, last_dest)
            continue
        if not filed:
            if dest.exists() and not same_file(src, dest):
                log(f"pending {hash_[:8]} dest exists, left at {src}")
                continue
            if not same_file(src, dest):
                move_content(src, dest)
                src = dest
            last_dest, moved, skipped, conflicts = file_tree(src, kind, do_chown=True)
            log(f"moved {job.get('name')} -> {last_dest} renamed={moved}")
            if can_finish_job(src, kind, moved, conflicts):
                _cleanup_src(src, kind)
                finish_job(state, hash_, kind, last_dest)
                continue
        leftover = uncanonical_videos(src, kind) if src.exists() else []
        if leftover:
            log(f"pending {hash_[:8]} still release-named {leftover[0].name}")
        elif src.exists() and not real_videos(src, kind):
            log(f"pending {hash_[:8]} waiting for video files in {src}")
        else:
            log(f"pending {hash_[:8]} nothing renamed for {src}")


def reap_done_torrents(state: dict, rows: list[dict]) -> None:
    done = state.get("done") or {}
    for row in rows:
        hash_ = str(row.get("hash") or "")
        if not hash_ or hash_ not in done:
            continue
        if SKIP_TAG and SKIP_TAG in tags_of(row):
            continue
        if not is_complete(row):
            continue
        try:
            delete_torrent(hash_)
            log(f"removed leftover from qBit: {row.get('name')}")
        except Exception as exc:
            log(f"leftover delete failed {row.get('name')}: {exc}")


def process_torrent(row: dict, state: dict) -> None:
    hash_ = str(row.get("hash") or "")
    name = str(row.get("name") or "")
    if not hash_ or hash_ in (state.get("skipped") or {}):
        return
    if hash_ in (state.get("pending") or {}):
        return
    if SKIP_TAG and SKIP_TAG in tags_of(row):
        return
    if hash_ in (state.get("done") or {}):
        return
    if not is_complete(row):
        return

    content = Path(str(row.get("content_path") or row.get("save_path") or ""))
    if not str(content):
        return
    if in_incomplete(content):
        return
    if not content.exists():
        log(f"skip {name}: missing {content}")
        return

    files = torrent_files(hash_)
    kind = classify(name, str(row.get("category") or ""), files) or already_filed(content)
    if not kind:
        if hash_ not in UNKNOWN:
            log(f"skip {name}: could not tell TV vs movie")
            UNKNOWN.add(hash_)
        return

    dest = content
    if already_filed(content) != kind:
        dest = (TV_DIR if kind == "tv" else MOVIE_DIR) / dest_name(name, content)

    job = {"src": str(content), "dest": str(dest), "kind": kind, "name": name, "canonical": True}
    state.setdefault("pending", {})[hash_] = job
    save_state(state)
    delete_torrent(hash_)
    log(f"removed from qBit: {name} ({kind})")
    process_pending(state)


def requeue_uncanonical(state: dict) -> None:
    """Jobs marked done keep their release names if organize raced or skipped rename."""
    done = state.get("done") or {}
    pending = state.setdefault("pending", {})
    changed = False
    for hash_, job in list(done.items()):
        if hash_ in pending:
            continue
        kind = str(job.get("kind") or "")
        dest = Path(str(job.get("dest") or ""))
        if kind not in {"tv", "movies"} or not dest.exists():
            continue
        names = [p.name for p in iter_videos(dest)] or [dest.name]
        guessed = classify(dest.name, kind, names)
        if guessed:
            kind = guessed
        if not uncanonical_videos(dest, kind):
            continue
        log(f"retry uncanonical {dest}")
        done.pop(hash_, None)
        pending[hash_] = {
            "src": str(dest),
            "dest": str(dest),
            "kind": kind,
            "name": job.get("name") or dest.name,
            "canonical": True,
        }
        changed = True
    if changed:
        save_state(state)


def tick(state: dict) -> None:
    requeue_uncanonical(state)
    process_pending(state)
    code, text = qbit("GET", "/api/v2/torrents/info")
    if code != 200:
        raise RuntimeError(f"qBit torrents/info -> {code}: {text[:200]}")
    rows = json.loads(text) or []
    reap_done_torrents(state, rows)
    for row in rows:
        try:
            process_torrent(row, state)
        except FileExistsError as exc:
            hash_ = str(row.get("hash") or "")
            log(f"skip {row.get('name')}: destination exists ({exc})")
            state.get("pending", {}).pop(hash_, None)
            if hash_:
                state.setdefault("skipped", {})[hash_] = {"reason": "dest exists", "dest": str(exc)}
            save_state(state)
        except Exception as exc:
            log(f"failed {row.get('name')}: {type(exc).__name__}: {exc}")


def mark_tick(ok: bool, error: str = "") -> None:
    state = load_state()
    state["heartbeat"] = time.time()
    state["last_error"] = error
    save_state(state)


def run_organize() -> None:
    log(
        f"qbit-organize started qbit={QBIT_URL} tv={TV_DIR} movies={MOVIE_DIR} "
        f"interval={INTERVAL}s grace={GRACE_SEC}s skip_tag={SKIP_TAG or '-'}"
    )
    while True:
        try:
            mark_tick(True)
            tick(load_state())
            mark_tick(True)
        except Exception as exc:
            global LOGGED_IN
            LOGGED_IN = False
            log(f"tick failed: {type(exc).__name__}: {exc}")
            try:
                mark_tick(False, f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        time.sleep(INTERVAL)


def drain_scans() -> None:
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(SCAN_DIR.glob("*.json")):
        try:
            job = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log(f"bad scan job {path.name}: {exc}")
            path.unlink(missing_ok=True)
            continue
        kind = str(job.get("kind") or "")
        if kind not in {"tv", "movies"}:
            log(f"ignore scan job {path.name}: kind={kind!r}")
            path.unlink(missing_ok=True)
            continue
        if plex_scan(kind):
            path.unlink(missing_ok=True)


def run_scan() -> None:
    log(f"qbit-organize scan started plex={PLEX_URL} queue={SCAN_DIR}")
    if not plex_token():
        log("no Plex token yet; will retry")
    while True:
        try:
            drain_scans()
        except Exception as exc:
            log(f"scan failed: {type(exc).__name__}: {exc}")
        time.sleep(INTERVAL)


def run_library(dry_run: bool = False) -> None:
    log(f"library rename {'DRY-RUN' if dry_run else 'APPLY'} tv={TV_DIR} movies={MOVIE_DIR}")
    protected = set()
    try:
        protected = qbit_protected_paths()
        if protected:
            log(f"skipping {len(protected)} qBit path(s) still in the client")
    except Exception as exc:
        log(f"qBit skip list unavailable: {exc}")
    totals = {"tv": [0, 0, 0], "movies": [0, 0, 0]}
    for kind, root in (("movies", MOVIE_DIR), ("tv", TV_DIR)):
        if not root.is_dir():
            log(f"missing {root}")
            continue
        items = []
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            if is_protected(child, protected):
                log(f"skip qBit-owned {child}")
                continue
            items.append(child)
        for item in items:
            last, moved, skipped, conflicts = file_tree(item, kind, do_chown=False, dry_run=dry_run)
            totals[kind][0] += moved
            totals[kind][1] += skipped
            totals[kind][2] += conflicts
            if dry_run:
                continue
            prune_empties(item if item.is_dir() else item.parent, root)
            if item.exists() and item.is_dir() and item.resolve() != root.resolve():
                remain = [p for p in iter_videos(item) if not is_sample(p, kind)]
                if not remain:
                    shutil.rmtree(item, ignore_errors=True)
                    log(f"removed leftover {item.name}")
        if not dry_run:
            try:
                queue_scan(kind, root)
            except OSError as exc:
                log(f"scan queue failed for {kind}: {exc}")
    log(
        "library done "
        f"movies moved={totals['movies'][0]} skip={totals['movies'][1]} conflict={totals['movies'][2]} "
        f"tv moved={totals['tv'][0]} skip={totals['tv'][1]} conflict={totals['tv'][2]}"
    )


def run_rename_paths(paths: list[str], dry_run: bool = False) -> None:
    for raw in paths:
        src = Path(raw)
        if not src.exists():
            log(f"missing {src}")
            continue
        names = [p.name for p in iter_videos(src)] or [src.name]
        kind = classify(src.name, "", names) or already_filed(src)
        if not kind:
            log(f"skip {src}: could not tell TV vs movie")
            continue
        last, moved, skipped, conflicts = file_tree(src, kind, do_chown=True, dry_run=dry_run)
        log(f"rename {src.name} kind={kind} -> {last} moved={moved} skip={skipped} conflict={conflicts}")
        if dry_run:
            continue
        _cleanup_src(src, kind)
        try:
            queue_scan(kind, last)
        except OSError as exc:
            log(f"scan queue failed: {exc}")


def self_test() -> None:
    cases = {
        "Bluey.S01E01.720p.DSNP.WEBRip.x264-GalaxyTV.mkv": (1, 1, None, "Bluey", ""),
        "Bob's Burgers S01E01 Human Flesh.mkv": (1, 1, None, "Bob's Burgers", "Human Flesh"),
        "Heroes S01E01-E02 Orientation (1080p x265 Joy).mkv": (1, 1, 2, "Heroes", "Orientation"),
        "Star Trek Enterprise S01E01E02 Broken Bow (1080p x265 10bit Joy).mkv": (
            1,
            1,
            2,
            "Star Trek Enterprise",
            "Broken Bow",
        ),
        "The Outer Limits - 1x01-02 - The Sandkings.avi": (1, 1, 2, "The Outer Limits", "The Sandkings"),
        "StarTrek.TNG-s01e01.Encounter.at.Farpoint.mkv": (1, 1, None, "StarTrek TNG", "Encounter at Farpoint"),
        "Fallout - S01E01 - The End.mkv": (1, 1, None, "Fallout", "The End"),
        "S1.Ep.01.Standing.Up.in.the.Milky.Way.mp4": (1, 1, None, "", "Standing Up in the Milky Way"),
        "Yellowjackets (2021) - S01E01 - Pilot (1080p AMZN WEB-DL x265 Ghost).mkv": (
            1,
            1,
            None,
            "Yellowjackets",
            "Pilot",
        ),
    }
    failed = 0
    for name, expected in cases.items():
        parsed = parse_episode(name)
        got = (
            parsed["season"],
            parsed["episode"],
            parsed["episode_end"],
            parsed["show_hint"],
            parsed["episode_title"],
        ) if parsed else None
        if got != expected:
            print(f"FAIL {name}\n  got {got}\n  exp {expected}")
            failed += 1
    classify_cases = [
        (
            "tv",
            "Yellowjackets (2021) Season 1 S01 (1080p AMZN WEB-DL x265 HEVC 10bit EAC3 5.1 Ghost)",
            "Movies",
            ["Yellowjackets (2021) - S01E01 - Pilot (1080p AMZN WEB-DL x265 Ghost).mkv"],
        ),
        (
            "movies",
            "Muppet Treasure Island 1996 PROPER 1080p BluRay H264 AAC-SUBS",
            "TV",
            ["Muppet Treasure Island 1996 PROPER 1080p BluRay H264 AAC-RARBG.mkv"],
        ),
    ]
    for expected, name, category, files in classify_cases:
        got = classify(name, category, files)
        if got != expected:
            print(f"FAIL classify {name}\n  got {got}\n  exp {expected}")
            failed += 1
    title_cases = [
        (
            "www.Torrenting.com - Chuck Chuck Baby (2023) 720p WEBRip-LAMA",
            "Chuck Chuck Baby",
            2023,
        ),
        (
            "Chuck.Chuck.Baby.2023.720p.WEBRip.x264.AAC-LAMA.mp4",
            "Chuck Chuck Baby",
            2023,
        ),
        ("1917.2019.1080p.WEBRip.x264.AAC5.1-MP4", "1917", 2019),
        ("9.To.5.1980.1080p.BluRay.x265-RARBG", "9 To 5", 1980),
        ("Best.in.Show.2000.1080p.BluRay.x264-HD4U", "Best in Show", 2000),
    ]
    for raw, want_title, want_year in title_cases:
        got_title = clean_title(raw)
        got_year = extract_year(raw)
        if got_title != want_title or got_year != want_year:
            print(
                f"FAIL clean {raw!r}\n  got {got_title!r} {got_year}\n"
                f"  exp {want_title!r} {want_year}"
            )
            failed += 1
    if title_matches("Chuck", "Chuck Chuck Baby"):
        print("FAIL title_matches accepted TV Chuck for Chuck Chuck Baby")
        failed += 1
    if not title_matches("Chuck Chuck Baby", "Chuck Chuck Baby"):
        print("FAIL title_matches rejected exact Chuck Chuck Baby")
        failed += 1
    side = sidecar_dest(
        Path("Chuck.Chuck.Baby.2023.720p.WEBRip.x264.AAC-LAMA.mp4"),
        Path("Subs/swe.srt"),
        Path("/movies/Chuck Chuck Baby (2023)/Chuck Chuck Baby (2023).mp4"),
    )
    if side.name != "Chuck Chuck Baby (2023).swe.srt":
        print(f"FAIL sidecar dest {side.name}")
        failed += 1
    if failed:
        raise SystemExit(f"self-test failed ({failed})")
    print("self-test ok")


def main() -> None:
    argv = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv[1:]
    if "--self-test" in argv:
        self_test()
        return
    if "--rename" in argv:
        idx = argv.index("--rename")
        run_rename_paths(argv[idx + 1 :], dry_run=dry)
        return
    if "--library" in argv or ROLE == "library":
        run_library(dry_run=dry)
        return
    if ROLE == "scan":
        run_scan()
        return
    run_organize()


if __name__ == "__main__":
    main()
