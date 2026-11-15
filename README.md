# Music Seerr

[![Release](https://img.shields.io/github/v/release/giulio177/music-seerr?sort=semver)](https://github.com/giulio177/music-seerr/releases)
[![Docker image](https://github.com/giulio177/music-seerr/actions/workflows/docker.yml/badge.svg)](https://github.com/giulio177/music-seerr/pkgs/container/music-seerr)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Request single songs for your Jellyfin library: search, preview, download with synced lyrics, and play them in the same app.

![Search results with preview and download buttons](docs/screenshots/search.png)

## Why

Lidarr and similar tools manage whole albums and discographies. Music Seerr covers what they leave out:

- **Single tracks.** Download one song without grabbing the whole album.
- **Listen first.** Preview a track before you download it.
- **Covers included.** Album art is embedded in every download; when no catalog cover exists, the video thumbnail is used.
- **Lyrics included.** Synchronized lyrics are saved as `.lrc` next to each track, and missing ones can be fetched for songs already in your library.
- **One app.** Request a song, play your Jellyfin library, and read the lyrics without switching apps.
- **Works alongside Lidarr.** Files use Lidarr's naming and tags, so both can share one library.

Planned: an iOS app to request songs from your phone.

## Features

- **Search and preview** tracks before downloading; tracks already in your library are marked.
- **Picked for you**: recommendations from your listening history and your own Jellyfin playlists; tracks missing from your library come first.
- **Download queue** with live progress, cancel, retry, and per-user history.
- **Activity overview**: downloads and plays per day, failed downloads, top playlists, and most played artists.
- **Save to playlist**: add a download straight to a Jellyfin playlist.
- **Playlist CSV sync**: compare a CSV with a playlist, add matches, and queue missing tracks.
- **Playlist duplicates**: spot songs added more than once, choose which copy to keep, or mark title matches as different songs.
- **Built-in player** for your Jellyfin library with queue, shuffle, and synchronized lyrics from LRCLIB.
- **Library import**: move existing folders or browser uploads into the library layout, keeping the best-quality copy.
- **Admin controls**: settings, cross-user history, and playlist management for Jellyfin administrators.
- **Guest access** for search and downloads without a Jellyfin account.
- **Mobile layout**, English and Italian.

| Sign in with Jellyfin | Mobile |
| --- | --- |
| ![Sign-in screen](docs/screenshots/login.png) | ![Search on a phone](docs/screenshots/search-mobile.png) |

## Install

Requirements: Docker Engine with the Compose plugin and a reachable Jellyfin server.

```sh
mkdir music-seerr && cd music-seerr
curl -fsSLO https://raw.githubusercontent.com/giulio177/music-seerr/main/docker-compose.yml
mkdir -p music backend/data
sudo chown -R 10001:10001 music backend/data
docker compose up -d
```

Open `http://<server-ip>:8000` and complete the setup wizard.

Mount the same `music/` folder into Jellyfin's music library so Jellyfin can scan the downloads. To use an existing library (for example one managed by Lidarr), mount its root folder as `/music`.

Images are published for `linux/amd64` and `linux/arm64` (including 64-bit Raspberry Pi OS) as `ghcr.io/giulio177/music-seerr`.

## Configuration

Set these in a `.env` file next to `docker-compose.yml`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `JELLYFIN_URL` | `http://host.docker.internal:8096` | Jellyfin address as seen from the container. |
| `MUSIC_SEERR_TAG` | `latest` | Image tag to run. Pin a release such as `0.2.0` for predictable updates. |
| `MUSIC_SEERR_BIND` | `0.0.0.0` | Host address for port 8000. Use `127.0.0.1` behind a reverse proxy on the same host. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse proxy address trusted for forwarded headers. |

| Mount | Required | Purpose |
| --- | --- | --- |
| `/music` | Yes | Music library root, writable. Share it with Jellyfin. |
| `/app/data` | Yes | Configuration, sessions, history, and paired CSV files. |
| `/import` | No | Extra folder (for example a downloads folder) offered in **Settings > Import into library**. |
| `/jellyfin-playlists` | No | Jellyfin's `config/data/data/playlists` folder, read-only. Lets Music Seerr detect playlist owners. |

Jellyfin's API does not tell an API key who owns a playlist. Without the `/jellyfin-playlists` mount, an owner is detected only when a single account can see the playlist; otherwise an administrator selects it.

## Update

| Tag | Meaning |
| --- | --- |
| `X.Y.Z` | A release. |
| `X.Y` | Latest patch of that release line. |
| `latest` | Newest release. |
| `edge` | Current `main` branch. |

Back up `backend/data` and `music`, change `MUSIC_SEERR_TAG` if pinned, then:

```sh
docker compose pull && docker compose up -d
```

`GET /api/health` reports the running version. Releases follow Semantic Versioning.

To build the image yourself: `docker build -t ghcr.io/giulio177/music-seerr:dev ./backend`, then set `MUSIC_SEERR_TAG=dev`.

## Library layout

Tracks are saved as `Album Artist/Album (Year)/Album Artist - Album - NN - Title.mp3`.

- Existing artist and album folders are reused.
- A track already in its album folder is not downloaded again.
- Featured artists are written as separate artist values, so "Song (feat. Guest)" appears under both artists.

### Import existing music

An administrator opens **Settings > Import into library**, picks a server folder or uploads files, reviews the planned moves, and confirms.

- Duplicates: keep the extra copies, or delete them and keep the best quality (FLAC over MP3, then higher bitrate).
- Matching `.lrc` files move with their tracks.
- Every import writes a manifest to `/app/data`.
- From a shell: `python -m app.tools.reorganize_library <folder>` (add `--apply` to move).

### Tidy an existing library

`python -m app.tools.tidy_library`, run inside the container as the user that owns the music files, fixes combined artist credits, album folders split by year, and artists listed under a former name.

- Prints the plan by default; `--apply` writes the changes.
- `--alias "Old Name=New Name"` maps a former artist name and applies it to future downloads.
- `--undo <manifest>` restores the previous state.

## Security

- The web endpoint uses HTTP. Keep it on a trusted network.
- For remote access, use a VPN or a TLS reverse proxy. Do not expose port 8000 to the internet.
- The Jellyfin API key is stored in `backend/data`; protect that folder and its backups.
- If Jellyfin becomes unreachable, the sign-in screen can reconnect the same server without editing files.

## Beta limitations

- External services change; setup, Jellyfin integration, and download sources can break.
- A restart interrupts active downloads. They reappear in the queue for a manual retry.
- SQLite stores sessions and history for a single instance, with no migrations or automatic backups yet.
- Not validated on every Raspberry Pi model or storage setup.
- Downloads depend on `yt-dlp` and `ffmpeg`; availability and quality vary by track.

## Disclaimer

Music Seerr was built with AI-assisted coding and has received limited validation. Review the code and keep backups before relying on it. Only download content you have the right to access and store. Music Seerr is not affiliated with Jellyfin.

## License

[MIT](LICENSE)
