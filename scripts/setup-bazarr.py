#!/usr/bin/env python3
"""Connect Bazarr to Sonarr/Radarr, import existing libraries, search English subs."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def xml_key(path: Path, tag: str) -> str:
    text = path.read_text()
    match = re.search(rf"<{tag}>([^<]+)</{tag}>", text)
    if not match:
        raise SystemExit(f"{tag} not found in {path}")
    return match.group(1)


def http(method: str, url: str, headers: dict, body: dict | list | None = None) -> object:
    data = None if body is None else json.dumps(body).encode()
    hdrs = dict(headers)
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if not raw:
                return {}
            if resp.headers.get_content_type() == "application/json" or raw[:1] in (b"{", b"["):
                return json.loads(raw)
            return raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")
        raise SystemExit(f"{method} {url} -> {e.code}: {err[:800]}") from e


def wait_json(url: str, headers: dict, tries: int = 40) -> object:
    last = ""
    for _ in range(tries):
        try:
            return http("GET", url, headers)
        except SystemExit as e:
            last = str(e)
            time.sleep(3)
    raise SystemExit(f"Timeout waiting for {url}: {last}")


LOOKUP_ALIASES = (
    (re.compile(r"deep\s*space\s*nine|\bds9\b", re.I), "Star Trek Deep Space Nine"),
)


def folder_query(name: str) -> str:
    for pat, term in LOOKUP_ALIASES:
        if pat.search(name):
            return term
    cleaned = re.sub(r"[._]+", " ", name)
    cleaned = re.sub(
        r"\b(complete|season\s*\d+|s\d+|720p|1080p|2160p|webrip|web-dl|bluray|hdtv|x264|x265|hevc|dvdrip|amzn|dsnp|hmax|galaxy|etrg|eztv|tgx|nosub).*$",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -[]()")
    return cleaned or name


def add_root(arr_base: str, key: str, path: str) -> None:
    headers = {"X-Api-Key": key}
    existing = http("GET", f"{arr_base}/api/v3/rootfolder", headers)
    if any(r.get("path") == path for r in existing):
        print(f"  root {path} already present")
        return
    http("POST", f"{arr_base}/api/v3/rootfolder", headers, {"path": path})
    print(f"  added root {path}")


def quality_profile_id(arr_base: str, key: str) -> int:
    profiles = http("GET", f"{arr_base}/api/v3/qualityprofile", {"X-Api-Key": key})
    if not profiles:
        raise SystemExit("No quality profiles")
    return int(profiles[0]["id"])


def import_sonarr(base: str, key: str, tv_root: Path) -> int:
    headers = {"X-Api-Key": key}
    qid = quality_profile_id(base, key)
    existing = {s.get("tvdbId") for s in http("GET", f"{base}/api/v3/series", headers)}
    added = 0
    for folder in sorted(p for p in tv_root.iterdir() if p.is_dir()):
        term = folder_query(folder.name)
        hits = http(
            "GET",
            f"{base}/api/v3/series/lookup?{urllib.parse.urlencode({'term': term})}",
            headers,
        )
        if not hits:
            print(f"  no match: {folder.name}")
            continue
        hit = hits[0]
        tvdb = hit.get("tvdbId")
        if tvdb in existing:
            continue
        payload = {
            "title": hit["title"],
            "tvdbId": tvdb,
            "qualityProfileId": qid,
            "rootFolderPath": "/tv",
            "path": f"/tv/{folder.name}",
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {
                "searchForMissingEpisodes": False,
                "monitor": "all",
            },
            "titleSlug": hit.get("titleSlug"),
            "images": hit.get("images") or [],
            "seasons": hit.get("seasons") or [],
            "year": hit.get("year"),
        }
        try:
            http("POST", f"{base}/api/v3/series", headers, payload)
        except SystemExit as e:
            if "already been added" in str(e) or "409" in str(e):
                existing.add(tvdb)
                continue
            print(f"  skip {folder.name}: {e}")
            continue
        existing.add(tvdb)
        added += 1
        print(f"  series {hit.get('title')} <- {folder.name}")
    http("POST", f"{base}/api/v3/command", headers, {"name": "RescanSeries"})
    http("POST", f"{base}/api/v3/command", headers, {"name": "RefreshSeries"})
    return added


def import_radarr(base: str, key: str, movie_root: Path) -> int:
    headers = {"X-Api-Key": key}
    qid = quality_profile_id(base, key)
    existing = {m.get("tmdbId") for m in http("GET", f"{base}/api/v3/movie", headers)}
    added = 0
    for folder in sorted(p for p in movie_root.iterdir() if p.is_dir()):
        term = folder_query(folder.name)
        hits = http(
            "GET",
            f"{base}/api/v3/movie/lookup?{urllib.parse.urlencode({'term': term})}",
            headers,
        )
        if not hits:
            print(f"  no match: {folder.name}")
            continue
        hit = hits[0]
        tmdb = hit.get("tmdbId")
        if tmdb in existing:
            continue
        payload = {
            "title": hit["title"],
            "tmdbId": tmdb,
            "qualityProfileId": qid,
            "rootFolderPath": "/movies",
            "path": f"/movies/{folder.name}",
            "monitored": True,
            "minimumAvailability": "released",
            "addOptions": {"searchForMovie": False},
            "titleSlug": hit.get("titleSlug"),
            "images": hit.get("images") or [],
            "year": hit.get("year"),
        }
        try:
            http("POST", f"{base}/api/v3/movie", headers, payload)
        except SystemExit as e:
            print(f"  skip {folder.name}: {e}")
            continue
        existing.add(tmdb)
        added += 1
        print(f"  movie {hit.get('title')} <- {folder.name}")
    http("POST", f"{base}/api/v3/command", headers, {"name": "RescanMovie"})
    http("POST", f"{base}/api/v3/command", headers, {"name": "RefreshMovie"})
    return added


def configure_bazarr(bazarr_url: str, baz_key: str, sonarr_key: str, radarr_key: str) -> None:
    headers = {"X-API-KEY": baz_key}
    settings = wait_json(f"{bazarr_url}/api/system/settings", headers)
    settings.setdefault("general", {})
    settings["general"]["movie_default_enabled"] = True
    settings["general"]["serie_default_enabled"] = True
    settings["general"]["movie_default_profile"] = 1
    settings["general"]["serie_default_profile"] = 1
    settings["general"]["adaptive_searching"] = True
    settings.setdefault("sonarr", {})
    settings["sonarr"].update(
        {
            "ip": "sonarr",
            "port": 8989,
            "base_url": "",
            "ssl": False,
            "apikey": sonarr_key,
            "full_update": "Daily",
        }
    )
    settings.setdefault("radarr", {})
    settings["radarr"].update(
        {
            "ip": "radarr",
            "port": 7878,
            "base_url": "",
            "ssl": False,
            "apikey": radarr_key,
            "full_update": "Daily",
        }
    )
    settings.setdefault("languages", {})
    # Enabled language profile is created below if missing.
    http("POST", f"{bazarr_url}/api/system/settings", headers, settings)
    # Bazarr settings POST is form-urlencoded, not JSON.
    form = urllib.parse.urlencode(
        {
            "settings-general-use_embedded_subs": "false",
            "settings-general-ignore_ass_subs": "true",
        }
    ).encode()
    req = urllib.request.Request(
        f"{bazarr_url}/api/system/settings",
        data=form,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        print(f"  sidecar-force save {resp.status}")
    print("  wrote Sonarr/Radarr connection")

    langs = []
    for path in (
        "/api/system/languages/profiles",
        "/api/languages/profiles",
    ):
        try:
            langs = http("GET", f"{bazarr_url}{path}", headers)
            if langs:
                break
        except SystemExit:
            continue
    if isinstance(langs, list) and langs:
        profile_id = langs[0].get("profileId") or langs[0].get("id") or 1
        settings = http("GET", f"{bazarr_url}/api/system/settings", headers)
        settings["general"]["movie_default_profile"] = profile_id
        settings["general"]["serie_default_profile"] = profile_id
        http("POST", f"{bazarr_url}/api/system/settings", headers, settings)
        print(f"  using language profile {profile_id}")
    else:
        print("  no language profile API; using Bazarr defaults")

    providers = []
    for path in ("/api/providers", "/api/system/providers"):
        try:
            providers = http("GET", f"{bazarr_url}{path}", headers)
            break
        except SystemExit:
            continue
    wanted = {"podnapisi", "tvsubtitles", "gestdown", "opensubtitles", "addic7ed"}
    enabled = []
    if isinstance(providers, list):
        for p in providers:
            name = p.get("name") or p.get("id")
            if name in wanted:
                enabled.append(name)
    settings = http("GET", f"{bazarr_url}/api/system/settings", headers)
    settings.setdefault("general", {})
    if enabled:
        settings["general"]["enabled_providers"] = enabled
        print(f"  providers {enabled}")
    settings.setdefault("languages", {})
    settings["languages"]["enabled"] = ["en"]
    http("POST", f"{bazarr_url}/api/system/settings", headers, settings)

    for task in ("update_all_movies", "update_all_series", "wanted_search_all"):
        for path in ("/api/system/tasks", "/api/system/tasks/run"):
            try:
                http("POST", f"{bazarr_url}{path}", headers, {"taskid": task})
                print(f"  queued {task}")
                break
            except SystemExit:
                continue
    print("  Bazarr settings saved")


def main() -> None:
    load_dotenv()
    lan = os.environ.get("LAN_IP", "192.168.1.8")
    sonarr_xml = Path(os.environ["SONARR_CONFIG"]) / "config.xml"
    radarr_xml = Path(os.environ["RADARR_CONFIG"]) / "config.xml"
    bazarr_yaml = Path(os.environ["BAZARR_CONFIG"]) / "config" / "config.yaml"
    if not bazarr_yaml.is_file():
        bazarr_yaml = Path(os.environ["BAZARR_CONFIG"]) / "config.yaml"

    for path in (sonarr_xml, radarr_xml):
        for _ in range(40):
            if path.is_file() and "ApiKey" in path.read_text():
                break
            time.sleep(3)
        else:
            raise SystemExit(f"No API key yet in {path}")

    sonarr_key = xml_key(sonarr_xml, "ApiKey")
    radarr_key = xml_key(radarr_xml, "ApiKey")
    sonarr = f"http://{lan}:8989"
    radarr = f"http://{lan}:7878"
    bazarr = f"http://{lan}:6767"

    wait_json(f"{sonarr}/api/v3/system/status", {"X-Api-Key": sonarr_key})
    wait_json(f"{radarr}/api/v3/system/status", {"X-Api-Key": radarr_key})
    print("Sonarr/Radarr are up")

    add_root(sonarr, sonarr_key, "/tv")
    add_root(radarr, radarr_key, "/movies")
    print("Importing Sonarr series from /mnt/usbdrive/TV")
    n_s = import_sonarr(sonarr, sonarr_key, Path("/mnt/usbdrive/TV"))
    print("Importing Radarr movies from /mnt/usbdrive/Movies")
    n_m = import_radarr(radarr, radarr_key, Path("/mnt/usbdrive/Movies"))
    print(f"added series={n_s} movies={n_m}")

    baz_key = None
    for _ in range(40):
        if bazarr_yaml.is_file():
            text = bazarr_yaml.read_text()
            m = re.search(r"apikey:\s*([A-Za-z0-9]+)", text)
            if m:
                baz_key = m.group(1)
                break
        time.sleep(3)
    if not baz_key:
        raise SystemExit("Bazarr API key not ready")
    print("Configuring Bazarr")
    configure_bazarr(bazarr, baz_key, sonarr_key, radarr_key)
    print(f"Bazarr UI: http://{lan}:6767")


if __name__ == "__main__":
    main()
