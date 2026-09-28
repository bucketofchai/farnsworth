#!/usr/bin/env python3
"""Point Sonarr at qbit-organize Show (Year) folders, rescan, then Bazarr search."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
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
    match = re.search(rf"<{tag}>([^<]+)</{tag}>", path.read_text())
    if not match:
        raise SystemExit(f"{tag} not found in {path}")
    return match.group(1)


def bazarr_key(path: Path) -> str:
    in_auth = False
    for line in path.read_text().splitlines():
        if line.startswith("auth:"):
            in_auth = True
            continue
        if in_auth:
            if line and not line[:1].isspace():
                break
            match = re.search(r"apikey:\s*['\"]?([A-Za-z0-9]+)", line)
            if match:
                return match.group(1)
    raise SystemExit(f"Bazarr auth apikey not found in {path}")


def http(method: str, url: str, headers: dict, body: dict | list | None = None, timeout: int = 60) -> object:
    data = None if body is None else json.dumps(body).encode()
    hdrs = dict(headers)
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {}
            if resp.headers.get_content_type() == "application/json" or raw[:1] in (b"{", b"["):
                return json.loads(raw)
            return raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"{method} {url} -> {exc.code}: {err[:800]}") from exc


def norm(text: str) -> str:
    text = text.replace(":", " - ")
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(text.split())


def tokens(text: str) -> set[str]:
    return set(norm(text).split())


def match_folder(title: str, year: int | None, folders: list[str]) -> str | None:
    titled = title.replace(":", " - ")
    if year:
        want = norm(f"{titled} ({year})")
        exact = [name for name in folders if norm(name) == want]
        if len(exact) == 1:
            return exact[0]
    needed = tokens(titled)
    if year:
        needed.discard(str(year))
    hits = [name for name in folders if needed and needed <= tokens(name)]
    if year:
        yeared = [name for name in hits if str(year) in name]
        if len(yeared) == 1:
            return yeared[0]
        hits = yeared or hits
    if len(hits) == 1:
        return hits[0]
    return None


def main() -> None:
    load_dotenv()
    lan = os.environ["LAN_IP"]
    tv_root = Path(os.environ["MEDIA_ROOT"]) / "TV"
    sonarr_xml = Path(os.environ["SONARR_CONFIG"]) / "config.xml"
    baz_yaml = Path(os.environ["BAZARR_CONFIG"]) / "config" / "config.yaml"
    if not baz_yaml.is_file():
        baz_yaml = Path(os.environ["BAZARR_CONFIG"]) / "config.yaml"
    sonarr = f"http://{lan}:8989"
    bazarr = f"http://{lan}:6767"
    sonarr_h = {"X-Api-Key": xml_key(sonarr_xml, "ApiKey")}
    baz_h = {"X-API-KEY": bazarr_key(baz_yaml)}

    folders = sorted(p.name for p in tv_root.iterdir() if p.is_dir() and not p.name.startswith("."))
    series = http("GET", f"{sonarr}/api/v3/series", sonarr_h)
    if not isinstance(series, list):
        raise SystemExit("Sonarr series list failed")

    updated: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    for show in series:
        title = str(show.get("title") or "")
        year = show.get("year")
        old = str(show.get("path") or "")
        name = old.rsplit("/", 1)[-1]
        if name in folders:
            continue
        matched = match_folder(title, int(year) if year else None, folders)
        if not matched:
            skipped.append(f"{title}: {old}")
            continue
        new_path = f"/tv/{matched}"
        if old == new_path:
            continue
        show["path"] = new_path
        sid = int(show["id"])
        http("PUT", f"{sonarr}/api/v3/series/{sid}?moveFiles=false", sonarr_h, show)
        http("POST", f"{sonarr}/api/v3/command", sonarr_h, {"name": "RescanSeries", "seriesId": sid})
        updated.append((title, old, new_path))
        print(f"retargeted {title}\n  {old}\n  -> {new_path}")

    if skipped:
        print("unmatched:")
        for row in skipped:
            print(f"  {row}")
    print(f"updated {len(updated)} series")

    deadline = time.time() + 180
    cm = next((s for s in series if s.get("title") == "Criminal Minds"), None)
    if cm and any(t == "Criminal Minds" for t, _, _ in updated):
        sid = int(cm["id"])
        while time.time() < deadline:
            eps = http("GET", f"{sonarr}/api/v3/episode?seriesId={sid}&seasonNumber=11", sonarr_h)
            files = [e for e in eps if e.get("hasFile") or e.get("episodeFileId")]
            print(f"Criminal Minds S11 files {len(files)}/{len(eps) if isinstance(eps, list) else '?'}")
            if isinstance(eps, list) and files:
                break
            time.sleep(5)

    for task in ("update_series", "series_full_scan_subtitles", "wanted_search_missing_subtitles_series"):
        http("POST", f"{bazarr}/api/system/tasks?{urllib.parse.urlencode({'taskid': task})}", baz_h)
        print(f"queued bazarr {task}")


if __name__ == "__main__":
    main()
