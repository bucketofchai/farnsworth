#!/usr/bin/env python3
"""Add Gluetun v3.41+ GET /v1/portforward to the live auth file without printing secrets."""
from pathlib import Path

p = Path("/home/klg/gluetun/auth/config.toml")
text = p.read_text()
if "GET /v1/portforward" in text:
    print("gluetun auth: portforward route already present")
    raise SystemExit(0)
needle = '"GET /v1/openvpn/portforwarded"'
if needle not in text:
    raise SystemExit("gluetun auth: expected openvpn portforwarded route missing")
p.write_text(text.replace(needle, needle + ',\n  "GET /v1/portforward"'))
print("gluetun auth: added GET /v1/portforward")
