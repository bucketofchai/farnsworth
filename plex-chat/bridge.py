#!/usr/bin/env python3
"""Forward qBittorrent WebAPI and search indexers from Gluetun localhost."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import urllib.error
import urllib.parse
import urllib.request

LISTEN = os.environ.get("BRIDGE_BIND", "0.0.0.0")
PORT = int(os.environ.get("BRIDGE_PORT", "18081"))
UPSTREAM = os.environ.get("QBIT_UPSTREAM", "http://127.0.0.1:8080").rstrip("/")
SEARCH_TIMEOUT = int(os.environ.get("SEARCH_TIMEOUT", "12"))
USER_AGENT = os.environ.get(
    "SEARCH_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
)
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]


def _magnet(infohash: str, name: str) -> str:
    qs = urllib.parse.urlencode({"dn": name})
    trs = "&".join(urllib.parse.urlencode({"tr": t}) for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{infohash}&{qs}&{trs}"


def _fetch_json(url: str, timeout: int):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


def _row(url: str, name: str, size, seeds, leech, site: str) -> dict | None:
    name = str(name or "").strip()
    url = str(url or "").strip()
    if not name or not url:
        return None
    try:
        size_n = int(size)
    except (TypeError, ValueError):
        size_n = -1
    try:
        seeds_n = int(seeds)
    except (TypeError, ValueError):
        seeds_n = 0
    try:
        leech_n = int(leech)
    except (TypeError, ValueError):
        leech_n = 0
    return {
        "fileUrl": url,
        "fileName": name,
        "fileSize": size_n,
        "nbSeeders": max(seeds_n, 0),
        "nbLeechers": max(leech_n, 0),
        "siteUrl": site,
        "engineName": site,
    }


def search_torrentscsv(pattern: str, timeout: int) -> list[dict]:
    qs = urllib.parse.urlencode({"size": 100, "q": pattern})
    data = _fetch_json(f"https://torrents-csv.com/service/search?{qs}", timeout)
    torrents = data.get("torrents") if isinstance(data, dict) else data
    out = []
    for item in torrents or []:
        infohash = str(item.get("infohash") or "")
        name = item.get("name") or ""
        row = _row(
            _magnet(infohash, name) if infohash else "",
            name,
            item.get("size_bytes"),
            item.get("seeders"),
            item.get("leechers"),
            "https://torrents-csv.com",
        )
        if row:
            out.append(row)
    return out


def search_apibay(pattern: str, timeout: int) -> list[dict]:
    qs = urllib.parse.urlencode({"q": pattern})
    data = _fetch_json(f"https://apibay.org/q.php?{qs}", timeout)
    out = []
    for item in data or []:
        name = item.get("name") or ""
        if name.lower() == "no results returned":
            continue
        infohash = str(item.get("info_hash") or "")
        row = _row(
            _magnet(infohash, name) if infohash else "",
            name,
            item.get("size"),
            item.get("seeders"),
            item.get("leechers"),
            "https://thepiratebay.org",
        )
        if row:
            out.append(row)
    return out


def search_solidtorrents(pattern: str, timeout: int) -> list[dict]:
    qs = urllib.parse.urlencode({"q": pattern, "skip": 0, "sort": "seeders", "category": "all"})
    data = _fetch_json(f"https://solidtorrents.to/api/v1/search?{qs}", timeout)
    results = data.get("results") if isinstance(data, dict) else data
    out = []
    for item in results or []:
        row = _row(
            item.get("magnet") or "",
            item.get("title") or item.get("name") or "",
            item.get("size"),
            item.get("seeders"),
            item.get("leechers"),
            "https://solidtorrents.to",
        )
        if row:
            out.append(row)
    return out


SOURCES = (
    ("torrentscsv", search_torrentscsv),
    ("apibay", search_apibay),
    ("solidtorrents", search_solidtorrents),
)


def run_search(pattern: str) -> tuple[list[dict], list[str]]:
    results: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        futs = {pool.submit(fn, pattern, SEARCH_TIMEOUT): name for name, fn in SOURCES}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                rows = fut.result()
                print(f"search {name}: {len(rows)} hits", flush=True)
                results.extend(rows)
            except Exception as exc:
                msg = f"{name}: {type(exc).__name__}: {exc}"
                print(f"search {msg}", flush=True)
                errors.append(msg)
    return results, errors


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(fmt % args, flush=True)

    def do_GET(self) -> None:
        if self.path.startswith("/nova-search"):
            self._nova_search()
            return
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _nova_search(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        pattern = (qs.get("pattern") or [""])[0].strip()
        if not pattern:
            self._json(400, {"error": "missing pattern"})
            return
        print("search", pattern, flush=True)
        results, errors = run_search(pattern)
        if not results and errors:
            self._json(502, {"error": "; ".join(errors), "results": []})
            return
        self._json(200, {"results": results, "total": len(results), "errors": errors})

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            "Referer": f"{UPSTREAM}/",
            "Origin": UPSTREAM,
        }
        ctype = self.headers.get("Content-Type")
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(
            f"{UPSTREAM}{self.path}",
            data=body,
            method=self.command,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() in {"transfer-encoding", "connection", "keep-alive"}:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            msg = str(exc).encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(msg)


if __name__ == "__main__":
    print(f"qbit-bridge {LISTEN}:{PORT} -> {UPSTREAM}", flush=True)
    ThreadingHTTPServer((LISTEN, PORT), Handler).serve_forever()
