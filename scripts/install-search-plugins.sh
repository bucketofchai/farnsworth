#!/usr/bin/env bash
# Install qBittorrent search engines into the linuxserver config dir on this host.
set -euo pipefail

ENG="${QBIT_ENGINES:-/home/klg/qbittorrent/config/qBittorrent/nova3/engines}"
mkdir -p "$ENG"

fetch() {
  local url="$1" dest="$2"
  if curl -fsSL --retry 3 --retry-delay 1 -o "$dest" "$url"; then
    echo "ok  $(basename "$dest")"
  else
    rm -f "$dest"
    echo "skip $(basename "$dest") ($url)"
  fi
}

# Official engines (skip jackett — no Jackett service on puck)
OFFICIAL=(eztv limetorrents piratebay solidtorrents torlock torrentproject torrentscsv)
for name in "${OFFICIAL[@]}"; do
  fetch "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/${name}.py" "$ENG/${name}.py"
  fetch "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/${name}.png" "$ENG/${name}.png"
done
fetch "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/versions.txt" "$ENG/versions.txt"

# Community engines (public indexers, still maintained)
LIGHT=(
  academictorrents bitsearch bt4g cloudtorrents glotorrents
  kickasstorrents nyaa snowfl thepiratebay torrentclaw
  torrentdownload torrentgalaxy yourbittorrent
)
for name in "${LIGHT[@]}"; do
  fetch "https://raw.githubusercontent.com/LightDestory/qBittorrent-Search-Plugins/master/src/engines/${name}.py" "$ENG/${name}.py"
  fetch "https://raw.githubusercontent.com/LightDestory/qBittorrent-Search-Plugins/master/src/engines/${name}.png" "$ENG/${name}.png"
done

# YTS (movies API) — unofficial wiki listing
fetch "https://codeberg.org/lazulyra/qbit-plugins/raw/branch/main/yts.py" "$ENG/yts.py" \
  || fetch "https://raw.githubusercontent.com/nindogo/qbtSearchPlugins/master/engines/yts.py" "$ENG/yts.py"

chown -R klg:klg "$ENG" 2>/dev/null || true
echo "--- installed ---"
ls -1 "$ENG"/*.py | xargs -n1 basename | sort
