# Farnsworth

Farnsworth is a home media server in Docker. The part you use every day is **she-Knew**, a small chat on the LAN that knows the library and IMDb.

Open it at `http://<LAN_IP>:7680`. The username and password are `PLEX_CHAT_USER` and `PLEX_CHAT_PASSWORD` in `.env`.

## she-Knew

Ask in plain language.

- **A genre.** “Find me a comedy” or “recommend a sci-fi show” returns three IMDb titles rated over 7, newest first, skipping anything already on disk. Pick one. That title is searched, and you choose a result to download.
- **A title.** “The Apartment (1960)” searches directly and lists the best matches.
- **The library.** The side panel is the folders on disk, with a filter.
- **What is downloading.** Active transfers and files being moved into TV or Movies stay on the same page.
- **Last night.** A line under the status chips shows the latest automatic picks and whether each one was queued or missed.

The header also shows whether Plex, the VPN, qBit, the local model, and the filer are up, plus CPU, memory, and disk.

she-Knew listens on the LAN address and on `127.0.0.1` only. A second subnet, such as a guest Wi-Fi, does not get a listener unless you put it in `LAN_IP`.

## The rest of the stack

Plex uses host networking so clients keep a stable address. qBittorrent shares Gluetun’s network namespace; if the VPN is down, qBit has no network. Finished downloads are renamed into the TV and Movies folders and removed from seeding. While Plex is streaming, qBit is throttled.

Sonarr, Radarr, and Bazarr are on the LAN address and require a login. Grafana is there for host and playback graphs. VictoriaMetrics and VictoriaLogs stay on localhost.

Between midnight and 6am local time, a picker chooses three IMDb Top 250 movies that are not already owned and queues them. It does not run again until the next midnight. she-Knew shows that report the next time you open the page.

## Install

Docker with the Compose plugin is required. Plex expects `/dev/dri` when hardware decode is available.

```bash
scripts/install.sh
```

The installer asks for the media disk, the config directory, and the LAN address. It writes `.env`, creates the directories, and copies `proton.env.example` to `proton.env`. It will not replace an `.env` that is already there.

Paste a WireGuard private key into `proton.env`, then:

```bash
docker compose up -d
```

`scripts/install.sh --up` builds the chat image and starts the stack after that key is set. For a scripted run, export `MEDIA_ROOT`, `CONFIG_ROOT`, and `LAN_IP` and pass `--non-interactive`.

Paths, the LAN subnet, an optional extra subnet, and the VPN country all live in `.env`. See `.env.example`. Do not commit `.env` or `proton.env`.

Plex and qBit mount `MEDIA_ROOT` at the same path inside the container, so library paths stay valid when the disk is the one Plex already knows.

## Tests

From `plex-chat/`:

```bash
python3 test_genre.py
python3 test_imdb_daily.py
python3 test_imdb_season.py
```
