#!/usr/bin/env python3
"""Push Plex usage and Gluetun VPN samples to VictoriaMetrics / VictoriaLogs."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400").rstrip("/")
PLEX_PREFS = os.environ.get("PLEX_PREFS", "/prefs.xml")
VM_URL = os.environ.get("VM_URL", "http://127.0.0.1:8428").rstrip("/")
VL_URL = os.environ.get("VL_URL", "http://127.0.0.1:9428").rstrip("/")
INTERVAL = int(os.environ.get("INTERVAL_SEC", "30"))
GLUETUN_URL = os.environ.get("GLUETUN_URL", "http://127.0.0.1:8000").rstrip("/")
GLUETUN_AUTH_FILE = os.environ.get("GLUETUN_AUTH_FILE", "/gluetun-auth.toml")


def plex_token() -> str:
    token = os.environ.get("PLEX_TOKEN", "").strip()
    if token and token != "change-me":
        return token
    prefs = Path(PLEX_PREFS)
    if prefs.is_file():
        match = re.search(r'PlexOnlineToken="([^"]+)"', prefs.read_text(errors="replace"))
        if match:
            return match.group(1)
    raise RuntimeError("Set PLEX_TOKEN or mount Preferences.xml at PLEX_PREFS")


def as_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def gluetun_creds() -> tuple[str, str]:
    user = os.environ.get("GLUETUN_CONTROL_USER", "").strip()
    password = os.environ.get("GLUETUN_CONTROL_PASSWORD", "")
    if user:
        return user, password
    path = Path(GLUETUN_AUTH_FILE)
    if not path.is_file():
        return "", ""
    parsed_user = parsed_pass = ""
    for line in path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("username"):
            parsed_user = stripped.split("=", 1)[-1].strip().strip('"').strip("'")
        elif stripped.startswith("password"):
            parsed_pass = stripped.split("=", 1)[-1].strip().strip('"').strip("'")
            if parsed_user:
                return parsed_user, parsed_pass
    return parsed_user, parsed_pass


def prom_escape(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", " ")
        .replace('"', '\\"')
    )


def plex_get(token: str, path: str, params: dict | None = None) -> dict:
    query = urllib.parse.urlencode(params or {})
    url = f"{PLEX_URL}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "X-Plex-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        exc.read()
        raise RuntimeError(f"Plex GET {path} -> {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Plex GET {path} failed: {getattr(exc, 'reason', exc)}") from exc


def http_json(url: str, *, data: bytes | None = None, content_type: str = "application/json", auth=None) -> dict | str:
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    if auth:
        import base64

        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            if not raw:
                return {}
            text = raw.decode("utf-8", "replace")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code} {url}: {err}") from exc


def display_title(meta: dict) -> str:
    kind = meta.get("type") or ""
    title = meta.get("title") or "unknown"
    show = meta.get("grandparentTitle") or ""
    if kind == "episode" and show:
        season = meta.get("parentIndex")
        episode = meta.get("index")
        if season is not None and episode is not None:
            return f"{show} S{int(season):02d}E{int(episode):02d} — {title}"
        return f"{show} — {title}"
    year = meta.get("year")
    if year:
        return f"{title} ({year})"
    return title


def flatten_session(meta: dict) -> dict:
    user = meta.get("User") or {}
    player = meta.get("Player") or {}
    session = meta.get("Session") or {}
    transcode = meta.get("TranscodeSession") or {}
    media = (as_list(meta.get("Media")) or [{}])[0]
    duration = as_int(meta.get("duration"))
    offset = as_int(meta.get("viewOffset"))
    video_decision = transcode.get("videoDecision") or ""
    if transcode:
        decision = "transcode" if video_decision == "transcode" else "direct"
        if video_decision == "copy" and (transcode.get("audioDecision") == "transcode"):
            decision = "transcode"
        elif video_decision == "copy":
            decision = "direct"
    else:
        decision = "direct"
        video_decision = "direct"
    progress = 0.0
    if duration:
        progress = round(100.0 * float(offset) / float(duration), 1)
    sid = str(session.get("id") or meta.get("sessionKey") or meta.get("ratingKey") or "")
    return {
        "id": sid,
        "user": user.get("title") or "unknown",
        "title": display_title(meta),
        "show": meta.get("grandparentTitle") or "",
        "type": meta.get("type") or "",
        "state": player.get("state") or "",
        "player": player.get("title") or player.get("product") or "",
        "platform": player.get("platform") or "",
        "product": player.get("product") or "",
        "location": session.get("location") or ("lan" if player.get("local") else "wan"),
        "decision": decision,
        "video_decision": video_decision or decision,
        "resolution": media.get("videoResolution") or "",
        "bitrate": as_int(media.get("bitrate")),
        "progress_pct": progress,
    }


def snapshot_from_sessions(sessions: list[dict]) -> dict:
    playing = paused = buffering = transcoding = direct = local = remote = 0
    for sess in sessions:
        state = sess.get("state")
        if state == "playing":
            playing += 1
        elif state == "paused":
            paused += 1
        elif state == "buffering":
            buffering += 1
        if sess.get("decision") == "transcode":
            transcoding += 1
        else:
            direct += 1
        loc = str(sess.get("location") or "").lower()
        if loc in {"lan", "1", "true"}:
            local += 1
        else:
            remote += 1
    return {
        "total": len(sessions),
        "playing": playing,
        "paused": paused,
        "buffering": buffering,
        "transcoding": transcoding,
        "direct": direct,
        "local": local,
        "remote": remote,
    }


def library_count(token: str, key: str) -> int:
    data = plex_get(
        token,
        f"/library/sections/{key}/all",
        {"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "0"},
    )
    mc = data.get("MediaContainer") or {}
    for field in ("totalSize", "size"):
        value = mc.get(field)
        if value is not None:
            return int(value)
    return 0


def collect_plex(token: str) -> tuple[list[str], list[dict], list[dict]]:
    data = plex_get(token, "/status/sessions")
    sessions = [flatten_session(meta) for meta in as_list((data.get("MediaContainer") or {}).get("Metadata"))]
    snap = snapshot_from_sessions(sessions)
    lines = [
        f"puck_plex_streams {snap['total']}",
        f"puck_plex_streams_playing {snap['playing']}",
        f"puck_plex_streams_paused {snap['paused']}",
        f"puck_plex_streams_buffering {snap['buffering']}",
        f"puck_plex_streams_transcoding {snap['transcoding']}",
        f"puck_plex_streams_direct {snap['direct']}",
        f"puck_plex_streams_local {snap['local']}",
        f"puck_plex_streams_remote {snap['remote']}",
        "puck_plex_up 1",
    ]
    for sess in sessions:
        if not sess["id"]:
            continue
        labels = ",".join(
            [
                f'id="{prom_escape(sess["id"])}"',
                f'user="{prom_escape(sess["user"])}"',
                f'title="{prom_escape(sess["title"])}"',
                f'player="{prom_escape(sess["player"])}"',
                f'state="{prom_escape(sess["state"])}"',
                f'location="{prom_escape(sess["location"])}"',
                f'decision="{prom_escape(sess["decision"])}"',
                f'product="{prom_escape(sess["product"])}"',
                f'resolution="{prom_escape(sess["resolution"])}"',
            ]
        )
        lines.append(f"puck_plex_session_info{{{labels}}} 1")
        lines.append(
            f'puck_plex_session_progress{{{labels}}} {sess["progress_pct"]}'
        )
    libs = []
    try:
        sections = plex_get(token, "/library/sections")
        dirs = as_list((sections.get("MediaContainer") or {}).get("Directory"))
        for directory in dirs:
            key = str(directory.get("key") or "")
            if not key:
                continue
            try:
                count = library_count(token, key)
            except RuntimeError:
                count = as_int(directory.get("size") or directory.get("leafCount"))
            title = directory.get("title") or key
            kind = directory.get("type") or ""
            libs.append({"key": key, "title": title, "type": kind, "count": count})
            lines.append(
                'puck_plex_library_count{{key="{k}",title="{t}",type="{y}"}} {c}'.format(
                    k=prom_escape(key),
                    t=prom_escape(title),
                    y=prom_escape(kind),
                    c=count,
                )
            )
    except RuntimeError as exc:
        print(f"library collect failed: {exc}", flush=True)
    return lines, sessions, libs


def parse_geo(payload: dict) -> tuple[str, str]:
    lat = payload.get("latitude")
    lon = payload.get("longitude")
    if lat not in (None, "") and lon not in (None, ""):
        return str(lat), str(lon)
    loc = str(payload.get("location") or payload.get("loc") or "")
    if "," in loc:
        parts = [p.strip() for p in loc.split(",", 1)]
        if len(parts) == 2:
            return parts[0], parts[1]
    return "", ""


def collect_vpn() -> list[str]:
    user, password = gluetun_creds()
    if not user:
        return ["puck_vpn_up 0"]
    auth = (user, password)
    status = http_json(f"{GLUETUN_URL}/v1/vpn/status", auth=auth)
    if not isinstance(status, dict):
        status = {}
    running = str(status.get("status") or status.get("state") or "").lower() in {
        "running",
        "ok",
        "true",
        "up",
        "connected",
        "on",
    }
    public = http_json(f"{GLUETUN_URL}/v1/publicip/ip", auth=auth)
    if not isinstance(public, dict):
        public = {}
    try:
        pf = http_json(f"{GLUETUN_URL}/v1/portforward", auth=auth)
    except RuntimeError:
        pf = http_json(f"{GLUETUN_URL}/v1/openvpn/portforwarded", auth=auth)
    if not isinstance(pf, dict):
        pf = {}
    ip = public.get("public_ip") or public.get("ip") or public.get("ip_address") or ""
    city = public.get("city") or ""
    country = public.get("country") or ""
    lat, lon = parse_geo(public)
    port = as_int(pf.get("port"))
    lines = [
        f"puck_vpn_up {1 if running else 0}",
        f"puck_vpn_forwarded_port {port}",
    ]
    if ip or city or country:
        labels = ",".join(
            [
                f'public_ip="{prom_escape(ip)}"',
                f'city="{prom_escape(city)}"',
                f'country="{prom_escape(country)}"',
                f'latitude="{prom_escape(lat)}"',
                f'longitude="{prom_escape(lon)}"',
            ]
        )
        lines.append(f"puck_vpn_geo{{{labels}}} 1")
    if lat and lon:
        try:
            lines.append(f"puck_vpn_geo_lat {float(lat)}")
            lines.append(f"puck_vpn_geo_lon {float(lon)}")
        except ValueError:
            pass
    if not running and (ip or port):
        running = True
        lines[0] = f"puck_vpn_up {1 if running else 0}"
    return lines


def push_prometheus(lines: list[str]) -> None:
    body = ("\n".join(lines) + "\n").encode()
    http_json(
        f"{VM_URL}/api/v1/import/prometheus",
        data=body,
        content_type="text/plain",
    )


def push_logs(events: list[dict]) -> None:
    if not events:
        return
    payload = "\n".join(json.dumps(ev, default=str) for ev in events) + "\n"
    http_json(
        f"{VL_URL}/insert/jsonline?_stream_fields=dataset,service",
        data=payload.encode(),
        content_type="application/stream+json",
    )


def collect_once(prev: dict[str, dict], play_starts: int) -> tuple[dict[str, dict], int]:
    token = plex_token()
    lines, sessions, _libs = collect_plex(token)
    by_id = {sess["id"]: sess for sess in sessions if sess["id"]}
    started = set(by_id) - set(prev)
    stopped = set(prev) - set(by_id)
    play_starts += len(started)
    lines.append(f"puck_plex_play_starts_total {play_starts}")
    try:
        lines.extend(collect_vpn())
    except Exception as exc:
        print(f"vpn collect failed: {type(exc).__name__}: {exc}", flush=True)
        lines.append("puck_vpn_up 0")
    push_prometheus(lines)
    events = []
    ts = now_iso()
    for sid in started:
        sess = by_id[sid]
        events.append(
            {
                "_time": ts,
                "_msg": f'{sess["user"]} started {sess["title"]} on {sess["player"]}',
                "dataset": "plex.usage.play_start",
                "service": "plex",
                "user": sess["user"],
                "title": sess["title"],
                "player": sess["player"],
                "decision": sess["decision"],
                "location": sess["location"],
                "product": sess["product"],
            }
        )
    for sid in stopped:
        sess = prev[sid]
        events.append(
            {
                "_time": ts,
                "_msg": f'{sess["user"]} stopped {sess["title"]} on {sess["player"]}',
                "dataset": "plex.usage.play_stop",
                "service": "plex",
                "user": sess["user"],
                "title": sess["title"],
                "player": sess["player"],
                "decision": sess["decision"],
                "location": sess["location"],
                "product": sess["product"],
            }
        )
    push_logs(events)
    print(
        f"{ts} streams={len(sessions)} starts={len(started)} stops={len(stopped)}",
        flush=True,
    )
    return by_id, play_starts


def main() -> None:
    print("puck-metrics collector started", flush=True)
    prev: dict[str, dict] = {}
    play_starts = 0
    while True:
        try:
            prev, play_starts = collect_once(prev, play_starts)
        except Exception as exc:
            print(f"collect failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                push_prometheus(["puck_plex_up 0"])
            except Exception:
                pass
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
