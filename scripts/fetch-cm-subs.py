#!/usr/bin/env python3
"""Download English subs for Criminal Minds seasons that have none."""
from __future__ import annotations

import importlib.util
import os
import time
import urllib.parse
from pathlib import Path

HELPER = Path(__file__).with_name("queue-bazarr-wanted.py")
spec = importlib.util.spec_from_file_location("queue_bazarr_wanted", HELPER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
bazarr_key = mod.bazarr_key
http = mod.http
load_dotenv = mod.load_dotenv


def main() -> None:
    load_dotenv()
    lan = os.environ["LAN_IP"]
    media_tv = os.environ["MEDIA_ROOT"].rstrip("/") + "/TV"
    yaml = Path(os.environ["BAZARR_CONFIG"]) / "config" / "config.yaml"
    headers = {"X-API-KEY": bazarr_key(yaml)}
    base = f"http://{lan}:6767/api"
    for _ in range(20):
        code, _ = http("GET", f"{base}/system/tasks", headers)
        if code == 200:
            break
        time.sleep(2)
    else:
        raise SystemExit("bazarr not up")

    seasons = [int(x) for x in os.environ.get("CM_SEASONS", "11").split(",")]
    force = os.environ.get("CM_FORCE", "1").lower() not in {"0", "false", "no"}
    code, eps = http("GET", f"{base}/episodes?seriesid%5B%5D=4", headers)

    def needs_sidecar(row: dict) -> bool:
        video = Path(str(row.get("path") or "").replace("/tv/", f"{media_tv}/", 1))
        if video.suffix.lower() in {".mkv", ".mp4", ".m4v"}:
            for suffix in (".en.srt", ".en.hi.srt", ".srt"):
                if video.with_name(video.stem + suffix).is_file():
                    return False
        if force:
            return True
        return bool(row.get("missing_subtitles"))

    rows = [
        row
        for row in (eps.get("data") or [])
        if row.get("season") in seasons and needs_sidecar(row)
    ]
    rows.sort(key=lambda row: (row.get("season") or 0, row.get("episode") or 0))
    print(f"missing {len(rows)} across {seasons}")
    ok = fail = 0
    for row in rows:
        eid = row["sonarrEpisodeId"]
        season = int(row["season"])
        episode = int(row["episode"])
        code, hits = http("GET", f"{base}/providers/episodes?episodeid={eid}", headers)
        data = (hits.get("data") if isinstance(hits, dict) else None) or []
        if not isinstance(hits, dict):
            print(f"S{season:02d}E{episode:02d} search={code} {str(hits)[:120]}")
            fail += 1
            time.sleep(2)
            continue
        english = [
            item
            for item in data
            if str(item.get("language") or "").startswith("en")
            and int(item.get("score") or 0) >= 50
        ]
        english.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
        if not english:
            print(f"S{season:02d}E{episode:02d} no candidate n={len(data)}")
            fail += 1
            continue
        best = english[0]
        query = urllib.parse.urlencode(
            {
                "seriesid": 4,
                "episodeid": eid,
                "hi": best.get("hearing_impaired") or "False",
                "forced": best.get("forced") or "False",
                "original_format": "False",
                "provider": best["provider"],
                "subtitle": best["subtitle"],
            }
        )
        code, body = http("POST", f"{base}/providers/episodes?{query}", headers)
        score = best.get("score")
        print(f"S{season:02d}E{episode:02d} score={score} post={code}")
        if code in {200, 204}:
            ok += 1
        else:
            fail += 1
            print(" ", str(body)[:160])
        time.sleep(2)
    print(f"done ok={ok} fail={fail}")
    for season in seasons:
        folder = Path(media_tv) / "Criminal Minds (2005)" / f"Season {season:02d}"
        srts = sorted(folder.glob("*.srt")) if folder.is_dir() else []
        print(f"Season {season:02d} srts={len(srts)}")


if __name__ == "__main__":
    main()
