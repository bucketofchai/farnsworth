#!/usr/bin/env python3
"""Throttle qBittorrent while Plex is streaming.

Two roles share a state file because the processes cannot share a network
namespace: Plex is on the host, qBit's API is localhost inside Gluetun.

  ROLE=poller  (network_mode: host)  — read Plex sessions, write state
  ROLE=apply   (network_mode: service:gluetun) — read state, set qBit alt-speed
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROLE = os.environ.get("ROLE", "poller").strip().lower()
STATE_PATH = Path(os.environ.get("STATE_PATH", "/var/lib/plex-qbit/state.json"))
INTERVAL = int(os.environ.get("INTERVAL_SEC", "10"))
THROTTLE_ON = os.environ.get("THROTTLE_ON", "wan").strip().lower()
ACTIVE_STATES = {"playing", "buffering"}

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400").rstrip("/")
PLEX_PREFS = os.environ.get("PLEX_PREFS", "/prefs.xml")
QBIT_URL = os.environ.get("QBIT_URL", "http://127.0.0.1:8080").rstrip("/")
QBIT_USER = os.environ.get("QBIT_USER", "").strip()
QBIT_PASS = os.environ.get("QBIT_PASS", "")


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def plex_token() -> str:
    token = os.environ.get("PLEX_TOKEN", "").strip()
    if token and token != "change-me":
        return token
    prefs = Path(PLEX_PREFS)
    if prefs.is_file():
        match = re.search(r'PlexOnlineToken="([^"]+)"', prefs.read_text(errors="replace"))
        if match:
            return match.group(1)
    raise SystemExit("Set PLEX_TOKEN or mount Preferences.xml at PLEX_PREFS")


def plex_sessions(token: str) -> list[dict]:
    req = urllib.request.Request(
        f"{PLEX_URL}/status/sessions",
        headers={"Accept": "application/json", "X-Plex-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        exc.read()
        raise RuntimeError(f"Plex GET /status/sessions -> {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Plex GET /status/sessions failed: {getattr(exc, 'reason', exc)}") from exc
    sessions = []
    for meta in as_list((data.get("MediaContainer") or {}).get("Metadata")):
        player = meta.get("Player") or {}
        session = meta.get("Session") or {}
        location = str(session.get("location") or "").lower()
        if not location:
            location = "lan" if player.get("local") else "wan"
        sessions.append(
            {
                "state": str(player.get("state") or "").lower(),
                "location": location,
                "title": meta.get("title") or meta.get("grandparentTitle") or "unknown",
                "user": (meta.get("User") or {}).get("title") or "unknown",
            }
        )
    return sessions


def should_throttle(sessions: list[dict]) -> tuple[bool, list[dict]]:
    active = [s for s in sessions if s["state"] in ACTIVE_STATES]
    if THROTTLE_ON == "all":
        hits = active
    else:
        hits = [s for s in active if s["location"] not in {"lan", "1", "true"}]
    return bool(hits), hits


def write_state(throttle: bool, hits: list[dict], streams: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "throttle": throttle,
        "streams": streams,
        "hits": hits,
        "updated": time.time(),
    }
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(STATE_PATH)


def read_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"throttle": False, "hits": [], "streams": 0, "updated": 0}
    if time.time() - float(data.get("updated") or 0) > INTERVAL * 6:
        # Stale poller: do not leave torrents throttled forever.
        data["throttle"] = False
        data["stale"] = True
    return data


COOKIES = CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIES))
LOGGED_IN = False


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
        with OPENER.open(req, timeout=15) as resp:
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


def qbit_alt_mode() -> int:
    qbit_login()
    code, text = qbit_request("GET", "/api/v2/transfer/speedLimitsMode")
    if code in {401, 403} and QBIT_USER and not LOGGED_IN:
        qbit_login()
        code, text = qbit_request("GET", "/api/v2/transfer/speedLimitsMode")
    if code != 200:
        raise RuntimeError(f"qBit speedLimitsMode -> {code}: {text[:200]}")
    return int(text.strip() or "0")


def qbit_set_alt_mode(enable: bool) -> None:
    current = qbit_alt_mode()
    want = 1 if enable else 0
    if current == want:
        return
    code, text = qbit_request("POST", "/api/v2/transfer/toggleSpeedLimitsMode")
    if code != 200:
        raise RuntimeError(f"qBit toggleSpeedLimitsMode -> {code}: {text[:200]}")
    after = qbit_alt_mode()
    if after != want:
        raise RuntimeError(f"qBit alt-speed stuck at {after}, wanted {want}")


def summarize(hits: list[dict]) -> str:
    if not hits:
        return "none"
    return "; ".join(f"{h.get('user')}/{h.get('location')}: {h.get('title')}" for h in hits[:4])


def run_poller() -> None:
    token = plex_token()
    print(
        f"plex-qbit poller started throttle_on={THROTTLE_ON} plex={PLEX_URL} state={STATE_PATH}",
        flush=True,
    )
    last: bool | None = None
    while True:
        try:
            sessions = plex_sessions(token)
            throttle, hits = should_throttle(sessions)
            write_state(throttle, hits, len(sessions))
            if throttle != last:
                print(
                    f"throttle={'on' if throttle else 'off'} "
                    f"streams={len(sessions)} hits={summarize(hits)}",
                    flush=True,
                )
                last = throttle
        except Exception as exc:
            print(f"poll failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(INTERVAL)


def run_apply() -> None:
    print(f"plex-qbit apply started qbit={QBIT_URL} state={STATE_PATH}", flush=True)
    last: bool | None = None
    while True:
        try:
            state = read_state()
            throttle = bool(state.get("throttle"))
            qbit_set_alt_mode(throttle)
            if throttle != last:
                stale = " stale" if state.get("stale") else ""
                print(
                    f"qBit alt-speed={'on' if throttle else 'off'}{stale} "
                    f"hits={summarize(state.get('hits') or [])}",
                    flush=True,
                )
                last = throttle
        except Exception as exc:
            global LOGGED_IN
            LOGGED_IN = False
            print(f"apply failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(INTERVAL)


def main() -> None:
    if ROLE == "apply":
        run_apply()
    else:
        run_poller()


if __name__ == "__main__":
    main()
