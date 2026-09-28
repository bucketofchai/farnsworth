#!/usr/bin/env bash
# Copy this repo to puck over SSH. Run from the LAN (this script cannot reach
# 192.168.1.8 from a cloud VM).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
set -a
source "$ROOT/.env"
set +a

REMOTE="${REMOTE:-klg@puck}"
REMOTE_DIR="${REMOTE_DIR:-/home/klg/puck-plex}"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$REMOTE" "mkdir -p '$REMOTE_DIR'"
# Do not --delete: puck may have extra compose services and host-only files.
rsync -az \
  --exclude '.git/' \
  --exclude 'backups/' \
  --exclude '*.log' \
  --exclude '.env' \
  --exclude 'proton.env' \
  "$ROOT/" "$REMOTE:$REMOTE_DIR/"

echo "Pushed to $REMOTE:$REMOTE_DIR"
