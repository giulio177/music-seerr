# Music Seerr

Music Seerr is a self-hosted web app for finding music, previewing tracks, downloading them into a local music library, and connecting that library to Jellyfin. It includes per-account download history and Jellyfin playlist integration.

## Install with Docker Compose

Requirements: Docker Engine with the Compose plugin, a reachable Jellyfin server, and enough disk space for your music library. The image targets `linux/amd64` and `linux/arm64` through the official Python base image; Raspberry Pi performance still depends on the model and storage.

```sh
git clone https://github.com/<owner>/music-seerr.git
cd music-seerr
mkdir -p music backend/data
sudo chown -R 10001:10001 music backend/data
docker compose up -d --build
```

Open `http://<server-ip>:8000` and complete the setup wizard. If Jellyfin is not reachable at `http://host.docker.internal:8096`, set `JELLYFIN_URL` in a root `.env` file before starting Compose:

```dotenv
JELLYFIN_URL=http://<jellyfin-host>:8096
```

Mount the same host `music/` directory into Jellyfin's music library so Jellyfin can scan downloaded files. App configuration and history persist under `backend/data`; downloaded audio is stored under `music/`.

The default web endpoint uses HTTP. Keep it on a trusted network, or put it behind a VPN or a TLS-enabled reverse proxy before remote access. Do not expose port 8000 directly to the public internet.

## Beta limitations

- This is beta software. The setup, Jellyfin integration, and download sources can fail when external services change.
- Download jobs and queue state live in process memory. A restart interrupts active jobs; they do not resume automatically.
- SQLite stores app sessions and download history for a single app instance. There is no database migration or automatic backup workflow yet.
- The application has not been validated on every Raspberry Pi model or storage setup. Check logs, free disk space, and Jellyfin library visibility after installation.
- Downloads require a working `yt-dlp` source and `ffmpeg`; availability and quality vary by track and source.

## Versioning

Releases use Semantic Versioning. During beta, tags use prerelease versions such as `v0.1.0-alpha.1`; deploy a tagged release when you need a repeatable version.

## Disclaimer

Music Seerr is vibecoded: it was built with AI-assisted coding and has received limited validation. It is provided as-is, without warranties. Review the code and keep backups before relying on it. Only download content you have the right to access and store. Music Seerr is not affiliated with Jellyfin.
