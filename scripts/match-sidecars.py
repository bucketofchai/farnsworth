#!/usr/bin/env python3
"""Rename sidecar subs so Plex can attach them (Linux is case-sensitive)."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".wmv"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
LANG_SUFFIXES = (".en", ".eng", ".english", ".en-us", ".en.us")


def base_stem(path: Path) -> str:
    stem = path.stem
    lower = stem.lower()
    for suf in LANG_SUFFIXES:
        if lower.endswith(suf):
            return stem[: -len(suf)]
    return stem


def pair_and_rename(root: Path, dry_run: bool) -> tuple[int, int, int]:
    renamed = skipped = missing = 0
    videos = [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    ]
    for video in sorted(videos):
        wanted = video.with_name(f"{video.stem}.en.srt")
        if wanted.is_file():
            skipped += 1
            continue
        candidates = [
            p
            for p in video.parent.iterdir()
            if p.is_file() and p.suffix.lower() in SUB_EXTS
        ]
        match = None
        for cand in candidates:
            if base_stem(cand).lower() == video.stem.lower():
                match = cand
                break
        if match is None:
            missing += 1
            continue
        dest = video.with_name(f"{video.stem}.en{match.suffix.lower()}")
        if match.resolve() == dest.resolve():
            skipped += 1
            continue
        print(f"{'dry-run ' if dry_run else ''}{match.name} -> {dest.name}")
        if not dry_run:
            if dest.exists() and match.resolve() != dest.resolve():
                print(f"  skip, target exists: {dest.name}")
                skipped += 1
                continue
            os.rename(match, dest)
        renamed += 1
    return renamed, skipped, missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Not a directory: {root}")
    renamed, skipped, missing = pair_and_rename(root, args.dry_run)
    print(f"renamed={renamed} already_ok={skipped} no_sidecar={missing}")


if __name__ == "__main__":
    main()
