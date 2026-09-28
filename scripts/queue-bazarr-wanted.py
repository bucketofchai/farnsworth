#!/usr/bin/env python3
"""Queue Bazarr series sync + missing subtitle search. Prints no secrets."""
from __future__ import annotations

import json
import os
import re
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


def http(method: str, url: str, headers: dict) -> tuple[int, object]:
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, {}
            if raw[:1] in (b"{", b"["):
                return resp.status, json.loads(raw)
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")
        return exc.code, err[:400]


def main() -> None:
    load_dotenv()
    lan = os.environ["LAN_IP"]
    yaml = Path(os.environ["BAZARR_CONFIG"]) / "config" / "config.yaml"
    if not yaml.is_file():
        yaml = Path(os.environ["BAZARR_CONFIG"]) / "config.yaml"
    headers = {"X-API-KEY": bazarr_key(yaml)}
    base = f"http://{lan}:6767/api"
    code, tasks = http("GET", f"{base}/system/tasks", headers)
    print("tasks", code)
    ids = []
    rows = tasks.get("data") if isinstance(tasks, dict) else tasks
    if isinstance(rows, list):
        for task in rows:
            if not isinstance(task, dict):
                continue
            job = str(task.get("job_id") or "")
            ids.append(job)
            print(f"  {job} | {task.get('name')}")
    wanted = [
        "update_series",
        "series_full_scan_subtitles",
        "wanted_search_missing_subtitles_series",
    ]
    for task in wanted:
        if ids and task not in ids:
            print(f"skip unknown {task}")
            continue
        code, body = http(
            "POST",
            f"{base}/system/tasks?{urllib.parse.urlencode({'taskid': task})}",
            headers,
        )
        print(f"queued {task} -> {code}")
        if code >= 400:
            print(str(body)[:200])


if __name__ == "__main__":
    main()
