#!/usr/bin/env python3
"""Select English subtitles on every Plex movie and episode (idempotent)."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_FILE = Path(__file__).resolve().parent / ".subtitle-skips.txt"
SDH_RE = re.compile(r"\b(sdh|hi|cc|hearing.?impaired)\b", re.I)
FORCED_RE = re.compile(r"\bforced\b", re.I)
ENG_CODES = {"eng", "en"}
SUBTITLE_ALWAYS = 2
STREAM_SUBTITLE = "3"


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


def plex_token() -> str:
    token = os.environ.get("PLEX_TOKEN", "").strip()
    if token and token != "change-me":
        return token
    base = Path(os.environ.get("PLEX_CONFIG", "/var/lib/plexmediaserver"))
    prefs = base / "Library/Application Support/Plex Media Server/Preferences.xml"
    if prefs.is_file() and os.access(prefs, os.R_OK):
        match = re.search(r'PlexOnlineToken="([^"]+)"', prefs.read_text())
        if match:
            return match.group(1)
    try:
        xml = subprocess.check_output(
            [
                "docker",
                "exec",
                "plex",
                "cat",
                "/config/Library/Application Support/Plex Media Server/Preferences.xml",
            ],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
        match = re.search(r'PlexOnlineToken="([^"]+)"', xml)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    sys.exit("Set PLEX_TOKEN in .env or run where Preferences.xml is readable.")


class Plex:
    def __init__(self, url: str, token: str) -> None:
        self.base = url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, params: dict | None = None) -> ET.Element | None:
        query = dict(params or {})
        query["X-Plex-Token"] = self.token
        url = f"{self.base}{path}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(
            url,
            method=method,
            headers={"Accept": "application/xml", "X-Plex-Token": self.token},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return ET.fromstring(raw)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "replace")
            raise SystemExit(f"{method} {path} -> {e.code}: {err[:500]}") from e

    def get(self, path: str, params: dict | None = None) -> ET.Element:
        root = self.request("GET", path, params)
        if root is None:
            raise SystemExit(f"Empty response for GET {path}")
        return root

    def put(self, path: str, params: dict | None = None) -> None:
        self.request("PUT", path, params)


def stream_text(el: ET.Element) -> str:
    return " ".join(
        el.attrib.get(k, "")
        for k in ("title", "displayTitle", "extendedDisplayTitle")
    )


def is_english(el: ET.Element) -> bool:
    code = el.attrib.get("languageCode", "").lower()
    lang = el.attrib.get("language", "").lower()
    return code in ENG_CODES or lang == "english"


def is_forced(el: ET.Element) -> bool:
    if el.attrib.get("forced") in ("1", "true"):
        return True
    return bool(FORCED_RE.search(stream_text(el)))


def is_sdh(el: ET.Element) -> bool:
    if el.attrib.get("hearingImpaired") in ("1", "true"):
        return True
    return bool(SDH_RE.search(stream_text(el)))


def pick_english(streams: list[ET.Element]) -> ET.Element | None:
    english = [s for s in streams if is_english(s)]
    if not english:
        return None
    ranked = (
        [s for s in english if not is_forced(s) and not is_sdh(s)]
        or [s for s in english if not is_forced(s)]
        or english
    )
    return ranked[0]


def item_label(el: ET.Element) -> str:
    itype = el.attrib.get("type")
    title = el.attrib.get("title", "")
    if itype == "episode":
        show = el.attrib.get("grandparentTitle", "")
        try:
            loc = f"S{int(el.attrib.get('parentIndex', '')):02d}E{int(el.attrib.get('index', '')):02d}"
        except ValueError:
            loc = ""
        return " ".join(p for p in (show, loc, title) if p)
    year = el.attrib.get("year")
    return f"{title} ({year})" if year else title


def list_items(plex: Plex, section_key: str, libtype: str) -> list[ET.Element]:
    type_code = "1" if libtype == "movie" else "4"
    items: list[ET.Element] = []
    start = 0
    page = 100
    while True:
        root = plex.get(
            f"/library/sections/{section_key}/all",
            {
                "type": type_code,
                "X-Plex-Container-Start": str(start),
                "X-Plex-Container-Size": str(page),
            },
        )
        batch = list(root)
        items.extend(batch)
        total = int(root.attrib.get("totalSize") or root.attrib.get("size") or len(batch))
        start += len(batch)
        if not batch or start >= total:
            break
    return items


def set_account_english_always(plex: Plex, dry_run: bool) -> None:
    root = plex.get("/accounts")
    accounts = [a for a in root.findall("Account") if a.attrib.get("id") not in (None, "0")]
    if not accounts:
        print("No Plex user accounts found; skipping account prefs.")
        return
    for acc in accounts:
        name = acc.attrib.get("name", "")
        lang = acc.attrib.get("defaultSubtitleLanguage")
        mode = acc.attrib.get("subtitleMode")
        key = acc.attrib.get("key") or f"/accounts/{acc.attrib.get('id')}"
        if not name:
            continue
        print(f"Account {name!r}: subtitleLanguage={lang} subtitleMode={mode}")
        if lang in ("en", "eng") and mode == str(SUBTITLE_ALWAYS):
            print("  already English always-on")
            continue
        if dry_run:
            print(f"  dry-run: would set defaultSubtitleLanguage=en subtitleMode={SUBTITLE_ALWAYS}")
            continue
        plex.put(
            key,
            {
                "defaultSubtitleLanguage": "en",
                "subtitleMode": str(SUBTITLE_ALWAYS),
            },
        )
        print("  set English always-on")


def apply_item(plex: Plex, rating_key: str, dry_run: bool) -> tuple[str, str]:
    meta = plex.get(f"/library/metadata/{rating_key}")
    video = meta.find("Video")
    if video is None:
        return "skipped", rating_key
    label = item_label(video)
    parts = video.findall("./Media/Part")
    if not parts:
        return "skipped", label

    any_english = False
    any_update = False
    already = True
    for part in parts:
        streams = [
            s
            for s in part.findall("Stream")
            if s.attrib.get("streamType") == STREAM_SUBTITLE
        ]
        chosen = pick_english(streams)
        if chosen is None:
            continue
        any_english = True
        if chosen.attrib.get("selected") in ("1", "true"):
            continue
        already = False
        track = (
            chosen.attrib.get("displayTitle")
            or chosen.attrib.get("title")
            or f"id={chosen.attrib.get('id')}"
        )
        if dry_run:
            print(f"  dry-run {label} -> {track}")
            any_update = True
            continue
        plex.put(
            f"/library/parts/{part.attrib['id']}",
            {"subtitleStreamID": chosen.attrib["id"], "allParts": "1"},
        )
        print(f"  set {label} -> {track}")
        any_update = True
        break

    if not any_english:
        return "skipped", label
    if any_update:
        return "updated", label
    if already:
        return "already", label
    return "updated", label


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--section", help="Only this library title (e.g. Movies)")
    args = parser.parse_args()

    load_dotenv()
    plex = Plex(os.environ.get("PLEX_URL", f"http://{os.environ['LAN_IP']}:32400"), plex_token())
    ident = plex.get("/identity")
    print(
        f"Connected to {ident.attrib.get('machineIdentifier', 'plex')} "
        f"({ident.attrib.get('version', '')})"
    )

    set_account_english_always(plex, args.dry_run)

    counts = {"updated": 0, "already": 0, "skipped": 0}
    skips: list[str] = []

    for directory in plex.get("/library/sections").findall("Directory"):
        stype = directory.attrib.get("type")
        title = directory.attrib.get("title", "")
        key = directory.attrib.get("key")
        if stype not in ("movie", "show") or not key:
            continue
        if args.section and title.lower() != args.section.lower():
            continue
        print(f"\n== {title} ({stype}) ==")
        libtype = "movie" if stype == "movie" else "episode"
        items = list_items(plex, key, libtype)
        print(f"  {len(items)} items")
        for i, item in enumerate(items, start=1):
            status, label = apply_item(plex, item.attrib["ratingKey"], args.dry_run)
            counts[status] += 1
            if status == "skipped":
                skips.append(label)
            if i % 50 == 0:
                print(f"  ... {i}/{len(items)}")

    SKIP_FILE.write_text("\n".join(skips) + ("\n" if skips else ""))
    print("\nSummary")
    print(f"  updated: {counts['updated']}")
    print(f"  already English: {counts['already']}")
    print(f"  no English track: {counts['skipped']}")
    if skips:
        print(f"  skip list: {SKIP_FILE}")


if __name__ == "__main__":
    main()
