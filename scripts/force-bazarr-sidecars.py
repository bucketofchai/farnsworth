#!/usr/bin/env python3
"""Stop Bazarr treating embedded tracks as enough; always search for sidecar files."""
from __future__ import annotations

import importlib.util
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HELPER = Path(__file__).with_name("queue-bazarr-wanted.py")
spec = importlib.util.spec_from_file_location("queue_bazarr_wanted", HELPER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def pick(general: dict, *keys: str) -> dict:
    return {key: general.get(key) for key in keys}


def post_form(url: str, headers: dict, fields: dict[str, str], timeout: int = 180) -> int:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"POST {url} -> {exc.code}: {err}") from exc


def main() -> None:
    mod.load_dotenv()
    lan = os.environ["LAN_IP"]
    yaml = Path(os.environ["BAZARR_CONFIG"]) / "config" / "config.yaml"
    if not yaml.is_file():
        yaml = Path(os.environ["BAZARR_CONFIG"]) / "config.yaml"
    headers = {"X-API-KEY": mod.bazarr_key(yaml)}
    base = f"http://{lan}:6767/api"
    keys = (
        "use_embedded_subs",
        "ignore_ass_subs",
        "ignore_pgs_subs",
        "ignore_vobsub_subs",
        "embedded_subs_show_desired",
    )
    code, settings = mod.http("GET", f"{base}/system/settings", headers)
    if code != 200 or not isinstance(settings, dict):
        raise SystemExit(f"settings GET {code}")
    print("before", pick(settings.get("general") or {}, *keys))
    status = post_form(
        f"{base}/system/settings",
        headers,
        {
            "settings-general-use_embedded_subs": "false",
            "settings-general-ignore_ass_subs": "true",
        },
    )
    print("save", status)
    code, after = mod.http("GET", f"{base}/system/settings", headers)
    if code != 200 or not isinstance(after, dict):
        raise SystemExit(f"settings GET after {code}")
    print("after", pick(after.get("general") or {}, *keys))
    for line in yaml.read_text().splitlines():
        if "use_embedded_subs:" in line or "ignore_ass_subs:" in line:
            print("yaml", line.strip())
    for task in (
        "update_series",
        "series_full_scan_subtitles",
        "wanted_search_missing_subtitles_series",
    ):
        code, _ = mod.http(
            "POST",
            f"{base}/system/tasks?{urllib.parse.urlencode({'taskid': task})}",
            headers,
        )
        print(f"queued {task} -> {code}")


if __name__ == "__main__":
    main()
