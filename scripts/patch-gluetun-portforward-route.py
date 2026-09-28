#!/usr/bin/env python3
"""Add Gluetun v3.41+ GET /v1/portforward to the live auth file without printing secrets."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
env_path = ROOT / ".env"
if env_path.is_file():
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())
config = os.environ.get("GLUETUN_CONFIG", "")
if not config:
    raise SystemExit("Set GLUETUN_CONFIG in .env")
p = Path(config) / "auth" / "config.toml"
text = p.read_text()
if "GET /v1/portforward" in text:
    print("gluetun auth: portforward route already present")
    raise SystemExit(0)
needle = '"GET /v1/openvpn/portforwarded"'
if needle not in text:
    raise SystemExit("gluetun auth: expected openvpn portforwarded route missing")
p.write_text(text.replace(needle, needle + ',\n  "GET /v1/portforward"'))
print("gluetun auth: added GET /v1/portforward")
