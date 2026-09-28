#!/usr/bin/env bash
# Stop the Docker Plex container and restore native plexmediaserver.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
set -a
source "$ROOT/.env"
set +a

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

COMPOSE=(docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml")

echo "==> Stopping Docker Plex"
if docker inspect plex >/dev/null 2>&1; then
  "${COMPOSE[@]}" down
fi

echo "==> Starting native plexmediaserver"
systemctl enable --now plexmediaserver.service

ok=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:32400/identity" >/tmp/plex-identity.xml; then
    cat /tmp/plex-identity.xml
    echo
    ok=1
    break
  fi
  sleep 2
done

if [[ "$ok" -ne 1 ]]; then
  echo "Native Plex did not answer /identity in time." >&2
  systemctl status plexmediaserver --no-pager >&2 || true
  exit 1
fi

echo "Rolled back to native plexmediaserver."
