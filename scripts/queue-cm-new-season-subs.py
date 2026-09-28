#!/usr/bin/env python3
"""Rescan Criminal Minds in Sonarr, then queue Bazarr subtitle searches for S13-S16."""
from __future__ import annotations

import importlib.util
import os
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).with_name("retarget-sonarr-paths.py")
spec = importlib.util.spec_from_file_location("retarget_sonarr_paths", HELPER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

SERIES_ID = 4
SEASONS = (13, 14, 15, 16)


def main() -> None:
    mod.load_dotenv()
    lan = os.environ.get("LAN_IP", "192.168.1.8")
    sonarr = f"http://{lan}:8989"
    bazarr = f"http://{lan}:6767"
    sonarr_h = {
        "X-Api-Key": mod.xml_key(Path(os.environ["SONARR_CONFIG"]) / "config.xml", "ApiKey")
    }
    yaml = Path(os.environ["BAZARR_CONFIG"]) / "config" / "config.yaml"
    if not yaml.is_file():
        yaml = Path(os.environ["BAZARR_CONFIG"]) / "config.yaml"
    baz_h = {"X-API-KEY": mod.bazarr_key(yaml)}

    mod.http(
        "POST",
        f"{sonarr}/api/v3/command",
        sonarr_h,
        {"name": "RescanSeries", "seriesId": SERIES_ID},
    )
    print("queued sonarr RescanSeries", flush=True)

    deadline = time.time() + 240
    while time.time() < deadline:
        counts = {}
        for season in SEASONS:
            eps = mod.http(
                "GET",
                f"{sonarr}/api/v3/episode?seriesId={SERIES_ID}&seasonNumber={season}",
                sonarr_h,
            )
            files = [e for e in eps if e.get("hasFile") or e.get("episodeFileId")]
            counts[season] = (len(files), len(eps) if isinstance(eps, list) else 0)
        print("sonarr files", counts, flush=True)
        if all(counts[s][0] > 0 for s in (14, 15, 16)):
            break
        time.sleep(8)
    else:
        print("warn: timed out waiting for S14-S16 import", flush=True)

    for task in (
        "update_series",
        "series_full_scan_subtitles",
        "wanted_search_missing_subtitles_series",
    ):
        mod.http(
            "POST",
            f"{bazarr}/api/system/tasks?{urllib.parse.urlencode({'taskid': task})}",
            baz_h,
        )
        print(f"queued bazarr {task}", flush=True)


if __name__ == "__main__":
    main()
