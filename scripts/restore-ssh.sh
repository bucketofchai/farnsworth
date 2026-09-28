#!/usr/bin/env bash
# Run this on the server console when SSH from the LAN is blocked.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

echo "=== sshd ==="
sudo systemctl enable --now ssh sshd 2>/dev/null || sudo systemctl enable --now ssh
sudo systemctl --no-pager --full status ssh | head -20
ss -tlnp | grep ':22' || echo "WARNING: nothing listening on :22"

echo "=== ufw ==="
if command -v ufw >/dev/null; then
  sudo ufw allow OpenSSH || true
  sudo ufw allow from "$LAN_CIDR" to any port 22 proto tcp
  if [[ -n "${EXTRA_CIDRS:-}" ]]; then
    IFS=',' read -ra extras <<<"$EXTRA_CIDRS"
    for cidr in "${extras[@]}"; do
      [[ -n "$cidr" ]] || continue
      sudo ufw allow from "$cidr" to any port 22 proto tcp
    done
  fi
  sudo ufw status verbose
fi

echo "=== fail2ban ==="
sudo fail2ban-client status sshd 2>/dev/null || echo "no fail2ban"
if [[ -n "${UNBAN_IPS:-}" ]]; then
  # shellcheck disable=SC2086
  sudo fail2ban-client set sshd unbanip $UNBAN_IPS 2>/dev/null || true
fi

echo "done. from another machine: ssh ${REMOTE}"
