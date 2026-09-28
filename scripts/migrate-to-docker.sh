#!/usr/bin/env bash
# Cut native plexmediaserver over to linuxserver/plex on this host (puck).
# Requires root: stopping systemd Plex, mounting the media disk, and Docker.
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

if [[ "$(hostname -s)" != "puck" ]]; then
  echo "This script must run on puck (hostname is $(hostname -s))." >&2
  echo "From this computer: ssh -t klg@puck 'sudo $REMOTE_DIR/scripts/migrate-to-docker.sh'" >&2
  exit 1
fi

USB_MOUNT="/mnt/usbdrive"
CONFIG="${PLEX_CONFIG:-/var/lib/plexmediaserver}"
COMPOSE=(docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml")

mount_usbdrive() {
  mkdir -p "$USB_MOUNT"
  if findmnt -n "$USB_MOUNT" >/dev/null; then
    echo "usbdrive already mounted"
    return
  fi

  local uuid fstype
  uuid="$(blkid -s UUID -o value /dev/sdb1 2>/dev/null || true)"
  fstype="$(blkid -s TYPE -o value /dev/sdb1 2>/dev/null || echo ext4)"

  if [[ -n "$uuid" ]]; then
    if ! grep -q "$uuid" /etc/fstab; then
      # Replace the incomplete /dev/sdb1 fstab line so the 5.5T disk remounts on boot.
      sed -i.bak '/[[:space:]]\/mnt\/usbdrive[[:space:]]/d' /etc/fstab
      echo "UUID=$uuid $USB_MOUNT $fstype defaults,nofail 0 2" >> /etc/fstab
      echo "Updated /etc/fstab for $USB_MOUNT (backup: /etc/fstab.bak)"
    fi
  fi

  mount "$USB_MOUNT"
  echo "Mounted $USB_MOUNT"
}

echo "==> Mounting media disk"
mount_usbdrive

if [[ ! -d "$USB_MOUNT/TV" || ! -d "$USB_MOUNT/Movies" ]]; then
  echo "Expected $USB_MOUNT/TV and $USB_MOUNT/Movies after mount." >&2
  ls -la "$USB_MOUNT" >&2 || true
  exit 1
fi

if [[ ! -d "$CONFIG/Library/Application Support/Plex Media Server" ]]; then
  echo "Plex library not found at $CONFIG" >&2
  exit 1
fi

if ! command -v docker >/dev/null || ! docker compose version >/dev/null; then
  echo "Docker CE + Compose plugin are required (not snap Docker)." >&2
  exit 1
fi

if getent group docker >/dev/null; then
  usermod -aG docker klg || true
fi

echo "==> Stopping native plexmediaserver"
systemctl disable --now plexmediaserver.service

# Host networking: native Plex must be fully down before the container binds :32400.
if ss -lnt | grep -q ':32400 '; then
  echo "Port 32400 is still in use after stopping plexmediaserver." >&2
  ss -lntp | grep 32400 || true
  exit 1
fi

echo "==> Starting linuxserver/plex"
"${COMPOSE[@]}" pull
"${COMPOSE[@]}" up -d

echo "==> Waiting for /identity"
ok=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://127.0.0.1:32400/identity" >/tmp/plex-identity.xml; then
    cat /tmp/plex-identity.xml
    echo
    ok=1
    break
  fi
  sleep 2
done

if [[ "$ok" -ne 1 ]]; then
  echo "Plex container did not answer /identity in time. Logs:" >&2
  docker logs plex --tail 80 >&2 || true
  exit 1
fi

echo
echo "Migration complete. Web UI: http://192.168.1.8:32400/web"
echo "Rollback: sudo $ROOT/scripts/rollback-to-native.sh"
