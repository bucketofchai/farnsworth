#!/usr/bin/env python3
"""Convert Criminal Minds S13 Hi10P mkv files to 8-bit H.264 so Fire TV can Direct Play."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()
_media = os.environ.get("MEDIA_ROOT", "")
_config = os.environ.get("CONFIG_ROOT", "")
if not _media or not _config:
    raise SystemExit("Set MEDIA_ROOT and CONFIG_ROOT in .env")
HOST_TV = Path(_media) / "TV"
SEASON = HOST_TV / "Criminal Minds (2005)" / "Season 13"
CT_TV = Path("/tv")
TMP_DIR = Path(_config) / "bazarr" / "config" / "s13-tmp"
CT_TMP = Path("/config/s13-tmp")


def tv_in_container(path: Path) -> str:
    return str(CT_TV / path.relative_to(HOST_TV))


def ffprobe(path: Path) -> dict:
    rel = tv_in_container(path)
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "bazarr",
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,profile,pix_fmt",
            "-of",
            "json",
            rel,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr[-300:]}
    st = (json.loads(proc.stdout).get("streams") or [{}])[0]
    return st


def is_hi10(info: dict) -> bool:
    return "10" in str(info.get("profile") or "") or "p10" in str(info.get("pix_fmt") or "")


def playing_path() -> str:
    try:
        out = subprocess.check_output(
            ["pgrep", "-a", "Plex Transcoder"], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return ""
    return out


def convert(src: Path) -> str:
    if src.name in playing_path():
        return "skip-playing"
    info = ffprobe(src)
    if info.get("error"):
        return f"fail probe {info['error']}"
    if not is_hi10(info):
        return f"skip-already {info.get('profile')} {info.get('pix_fmt')}"
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TMP_DIR / (src.stem + ".8bit.tmp.mkv")
    tmp.unlink(missing_ok=True)
    start = time.time()
    log = Path("/tmp/s13-ffmpeg.log")
    with log.open("ab") as fh:
        fh.write(f"\n===== {src.name} =====\n".encode())
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "bazarr",
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-y",
                "-i",
                tv_in_container(src),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "copy",
                str(CT_TMP / tmp.name),
            ],
            stdout=fh,
            stderr=fh,
            timeout=3600,
        )
    elapsed = time.time() - start
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 10_000_000:
        tail = log.read_text(errors="replace")[-600:] if log.is_file() else ""
        tmp.unlink(missing_ok=True)
        return f"fail rc={proc.returncode} {elapsed:.0f}s {tail}"
    out_info = ffprobe(tmp)
    if is_hi10(out_info) or out_info.get("pix_fmt") != "yuv420p":
        tmp.unlink(missing_ok=True)
        return f"fail still-10bit {out_info} {elapsed:.0f}s"
    bak = src.with_name(src.stem + ".hi10.bak.mkv")
    bak.unlink(missing_ok=True)
    src.rename(bak)
    tmp.rename(src)
    bak.unlink()
    return (
        f"ok {elapsed:.0f}s {src.stat().st_size / 1024 / 1024:.0f}MiB "
        f"{out_info.get('profile')} {out_info.get('pix_fmt')}"
    )


def main() -> None:
    files = sorted(SEASON.glob("*.mkv"))
    files = [p for p in files if ".tmp." not in p.name and ".bak." not in p.name]
    if not files:
        sys.exit(f"no mkv in {SEASON}")
    print(f"season13 files={len(files)}", flush=True)
    ok = fail = skip = 0
    for src in files:
        print(f"start {src.name}", flush=True)
        try:
            result = convert(src)
        except subprocess.TimeoutExpired:
            result = "fail timeout"
            (TMP_DIR / (src.stem + ".8bit.tmp.mkv")).unlink(missing_ok=True)
        print(f"  {result}", flush=True)
        if result.startswith("ok"):
            ok += 1
        elif result.startswith("skip"):
            skip += 1
        else:
            fail += 1
    print(f"done ok={ok} skip={skip} fail={fail}", flush=True)
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
