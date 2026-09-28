#!/usr/bin/env bash
# Run this ON puck (keyboard/console), not from the laptop.
# Restores SSH from the home LAN and from the 10.127.1.0/24 WiFi subnet.
set -euo pipefail

echo "=== sshd ==="
sudo systemctl enable --now ssh sshd 2>/dev/null || sudo systemctl enable --now ssh
sudo systemctl --no-pager --full status ssh | head -20
ss -tlnp | grep ':22' || echo "WARNING: nothing listening on :22"

echo "=== ufw ==="
if command -v ufw >/dev/null; then
  sudo ufw allow OpenSSH || true
  sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
  sudo ufw allow from 10.127.1.0/24 to any port 22 proto tcp
  sudo ufw status verbose
fi

echo "=== fail2ban ==="
sudo fail2ban-client status sshd 2>/dev/null || echo "no fail2ban"
sudo fail2ban-client set sshd unbanip 10.127.1.89 2>/dev/null || true

echo "done. from the laptop: ssh klg@192.168.1.8"
