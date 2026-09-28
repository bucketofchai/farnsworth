#!/usr/bin/env python3
"""Add extra Bazarr providers so OpenSubtitles quota does not stall searches."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRA = ("podnapisi", "tvsubtitles", "gestdown")


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


def main() -> None:
    load_dotenv()
    yaml = Path(os.environ["BAZARR_CONFIG"]) / "config" / "config.yaml"
    text = yaml.read_text()
    needle = "  enabled_providers:\n  - opensubtitlescom\n"
    if not needle in text:
        if all(f"  - {name}\n" in text for name in EXTRA):
            print("providers already enabled")
            return
        raise SystemExit("enabled_providers block not in expected shape")
    extra = "".join(f"  - {name}\n" for name in EXTRA)
    if extra.strip() in text:
        print("providers already enabled")
        return
    yaml.write_text(text.replace(needle, needle + extra, 1))
    print("added", ", ".join(EXTRA))


if __name__ == "__main__":
    main()
