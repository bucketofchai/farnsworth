#!/usr/bin/env bash
# Restore qBittorrent WebUI access behind Gluetun.
# qBittorrent splits ServerDomains on ';' not ','. Host-header checks also
# reject the LAN address because qBit sees Gluetun's addresses, not LAN_IP.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

CONF="${QBIT_CONF:-$QBIT_CONFIG/qBittorrent/qBittorrent.conf}"
docker stop qbittorrent
python3 - <<PY
from pathlib import Path
p = Path("$CONF")
text = p.read_text()
updates = {
    "WebUI\\HostHeaderValidation": "false",
    "WebUI\\ServerDomains": "*",
}
out = []
found = set()
for line in text.splitlines():
    key = line.split("=", 1)[0]
    if key in updates:
        out.append(f"{key}={updates[key]}")
        found.add(key)
    else:
        out.append(line)
for key, val in updates.items():
    if key not in found:
        out.append(f"{key}={val}")
p.write_text("\n".join(out) + "\n")
print("wrote", ", ".join(f"{k}={updates[k]}" for k in updates))
PY
docker start qbittorrent
