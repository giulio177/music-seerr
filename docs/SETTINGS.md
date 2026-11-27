# Sonalis settings guide

Every setting in the Sonalis web app and the Sonalis iOS app, with the problem it solves, what it does, and who can change it.

[Account](#account) · [Preferences](#preferences) · [Playback](#playback-ios) · [Connection](#connection-administrators) · [Storage](#storage-administrators) · [Permissions](#permissions-administrators) · [Library](#library-administrators) · [iOS only](#ios-only) · [Beta](#beta-ios)

## Where a setting is saved

| Scope | Meaning |
| --- | --- |
| **Account** | Saved on the server for your account. The web app and every iPhone signed in as you use it, and changes reach open devices at once. |
| **Server** | Saved on the server for everyone. Only administrators change it. |
| **Device** | Saved on this iPhone or in this browser only. |

Changes to account settings made on an iPhone without a connection are kept and sent when the server is reachable again.

## Account

### Switch account and Sign out

| | |
| --- | --- |
| Where | Web: top of Settings. iOS: bottom of Settings. |
| Scope | Device |

- **Why**: several people share one Sonalis server, often on the same computer.
- **What it does**: Sign out ends the session on this device only. Switch account (web) signs in as another Jellyfin user without signing out first.
- **Notes**: on the iPhone, signing out stops playback. Downloaded songs stay on the iPhone.

## Preferences

### Language

| | |
| --- | --- |
| Where | Web and iOS: Preferences |
| Scope | Server (web), Device (iOS) |

- **Why**: the family uses Sonalis in English and Italian.
- **What it does**: sets the interface language. The web app keeps one language on the server. The iOS app follows the iPhone language unless you pick one; a choice made in the app is also sent to the server.
- **Notes**: translations are managed with Crowdin; English is the default.

### Accent color

| | |
| --- | --- |
| Where | Web and iOS: Preferences |
| Scope | Account |

- **Why**: lets each person make the app their own and tell accounts apart at a glance.
- **What it does**: colors the logo, buttons, progress bars, and highlights. Pick a preset or keep the server default.
- **Notes**: the server default is chosen by an administrator in the setup wizard. Guests keep their color in the browser only.

### Crossfade

| | |
| --- | --- |
| Where | Web and iOS: Preferences |
| Scope | Account |

- **Why**: the silence between songs breaks the flow of a playlist or a party queue.
- **What it does**: the end of each song fades out while the next one fades in, over 1 to 12 seconds. Off plays songs back to back.
- **Notes**:
  - Skipped when repeating one song, on the last song without repeat all, for catalog previews, and for songs too short for two fades.
  - Any skip, seek, or pause during a fade cancels it.
  - The web app and the iOS app use the same logic, so a fade sounds the same on both.

### Show other users' activity (administrators)

| | |
| --- | --- |
| Where | Web: Preferences > Administrator visibility. iOS: Preferences. |
| Scope | Account |

- **Why**: an administrator sometimes needs to check another person's downloads or playlists, but does not want them in every list all the time.
- **What it does**: shows or hides the controls for viewing other users' activity and playlists.
- **Notes**: it only changes what you see.

## Playback (iOS)

### Stabilize volume

| | |
| --- | --- |
| Where | iOS: Playback |
| Scope | Device |

- **Why**: some songs are much louder or quieter than others, so you kept changing the volume from one song to the next.
- **What it does**: plays every song at a similar loudness. Jellyfin's Audio Normalization task measures each song, and the app turns down loud songs to the level of quieter ones.
- **Notes**:
  - The iPhone cannot play a song above full volume, so quiet songs are never boosted; loud ones are lowered instead. After turning it on, raise the iPhone volume once.
  - Songs Jellyfin has not measured yet play at the library's typical level.
  - Works for downloaded songs offline, once the app has seen their measurement online.
  - Needs Sonalis server 0.3.0 or later and Jellyfin's **Audio Normalization** scheduled task (Dashboard > Scheduled Tasks).

## Connection (administrators)

### Jellyfin server URL, API key, and Test connection

| | |
| --- | --- |
| Where | Web: Settings > Connection. iOS: Jellyfin section. |
| Scope | Server |

- **Why**: Sonalis does not keep its own music database. It reads the library, playlists, and accounts from Jellyfin.
- **What it does**: sets the address Sonalis uses to reach Jellyfin and the API key it uses. Test connection checks both and finds the music library folders.
- **Notes**:
  - Create the API key in Jellyfin: Dashboard > Advanced > API Keys.
  - The address must start with `http://` or `https://`. Use the address the Sonalis container can reach, which may differ from the one in your browser.
  - If Jellyfin moves, Sonalis can repair the saved address by itself when the new one answers with the same server ID.
  - Changes apply after Save.

### Default Jellyfin user

| | |
| --- | --- |
| Where | Web: Settings > Connection. iOS: Jellyfin section. |
| Scope | Server |

- **Why**: guests have no Jellyfin account, but the library and playlists are read through one.
- **What it does**: chooses the Jellyfin user whose library and playlists are used when nobody is signed in.
- **Notes**: No default user keeps guests out of personal playlists.

### Run the setup wizard again (web)

- **Why**: the quickest way to fix a server that moved or a lost API key.
- **What it does**: reopens the first-run wizard with the Jellyfin address, API key, music folder, language, and default accent color.

## Storage (administrators)

### Music library folder

| | |
| --- | --- |
| Where | Web: Settings > Storage. iOS: Jellyfin section. |
| Scope | Server |

- **Why**: downloaded songs must land in the folder Jellyfin scans, or they never appear in the library.
- **What it does**: the folder where Sonalis saves downloaded songs and looks for `.lrc` lyrics files.
- **Notes**:
  - In Docker it must match the mounted music volume (`/music` in the example compose file). An explicit `MUSIC_OUTPUT_DIR` environment value always wins.
  - The iOS app shows whether the folder exists on the server.

## Permissions (administrators)

### Users can edit lyrics

| | |
| --- | --- |
| Where | Web: Preferences > Lyrics editing for users. iOS: Permissions. |
| Scope | Server |

- **Why**: lyrics files are shared by everyone, so by default only administrators may change them. Some families want everyone to fix a line that comes in late.
- **What it does**: lets every Jellyfin account edit synced lyrics in the web app and in the iOS beta editor. Administrators always can.
- **Notes**: guests can never edit lyrics. Off by default.

## Library (administrators)

These live in the web app; artist aliases are also in the iOS app.

### Import into library (web)

- **Why**: music you already own, or files from another download tool, rarely follow the library layout, so Jellyfin shows them as separate or broken albums.
- **What it does**: pick a server folder or upload files from your device. Each track is identified, a plan of moves is shown, and nothing moves until you confirm.
- **Notes**:
  - Layout: `Artist/Album (Year)/Artist - Album - NN - Title`.
  - Duplicates: keep the extra copies, or delete them and keep the best quality (FLAC over MP3, then higher bitrate).
  - Matching `.lrc` files move with their tracks. Every import writes a manifest to `/app/data`.
  - The optional `/import` volume (for example a downloads folder) is offered as a source.

### Library health (web)

- **Why**: wrong files hide in a big library, such as an instrumental saved under the original title, or an album filed under the wrong artist.
- **What it does**: scans the tags and lists files whose name, folder, or tags disagree, and files that share a track number. You rename, move, delete, or ignore each one.
- **Notes**: the scan changes nothing on its own. Every fix is logged to `/app/data/library-health.jsonl`. Ignored files stay hidden until ignores are cleared.

### Artist aliases

| | |
| --- | --- |
| Where | Web: Settings > Library. iOS: Artist aliases. |
| Scope | Server |

- **Why**: artists change names (for example `tha Supreme` became `thasup`). The catalog shows old releases under the old name, so they were not matched with the artist in your library.
- **What it does**: maps a former or alternate name, as the catalog shows it, to the name used in your library. Album and artist pages then find those releases, and new downloads are filed under the library name.
- **Notes**: aliases are also added automatically when an album turns up under another name. Existing files can be retagged with `python -m app.tools.tidy_library --alias "Old Name=New Name"`.

## iOS only

### Server address

| | |
| --- | --- |
| Where | iOS: Server |
| Scope | Device |

- **Why**: the same server is reached at a home address, over a VPN such as Tailscale, or through a domain, depending on where you are.
- **What it does**: changes the address this iPhone uses. You stay signed in when the address leads to the same Sonalis server; another server asks you to sign in there.
- **Notes**: an `http://` address without a port is also tried on port `8000`. Status shows whether the server answers right now.

### Downloads

| | |
| --- | --- |
| Where | iOS: Downloads |
| Scope | Device |

- **Why**: music should play on a plane, abroad, or with weak signal, without the server.
- **What it does**: shows the space used by downloaded songs. Remove all downloads deletes every downloaded song from the iPhone.
- **Notes**: songs are in the Files app under On My iPhone > Sonalis. Downloaded songs always play from the iPhone, even online, which saves data.

### Signature

| | |
| --- | --- |
| Where | iOS: Signature |
| Scope | Device |

- **Why**: apps installed with a free Apple ID stop opening after 7 days, often without warning.
- **What it does**: shows how many days are left before the signature expires.
- **Notes**: refresh the app in SideStore, or install it again, before then. Your sign-in and downloads are kept. Hidden in builds without a signature.

### Updates

| | |
| --- | --- |
| Where | iOS: Updates |
| Scope | Device |

- **Why**: updating through SideStore by hand was too fiddly for family members.
- **What it does**: shows the installed version and checks for a newer one. Sonalis also checks by itself and offers an update at most once a day. Update opens SideStore, which installs over the app and keeps your sign-in and downloads.
- **Switch VPN with shortcuts**:
  - **Why**: SideStore needs LocalDevVPN, but iOS runs one VPN at a time, so updating disconnects your usual VPN (such as Tailscale) and it had to be switched back by hand.
  - **What it does**: with it on, Update runs your shortcut `Sonalis Update` (connect LocalDevVPN, then open the Shortcut Input as a URL). The next time you open Sonalis it runs `Sonalis VPN` (connect your usual VPN) and comes back.
  - **Notes**: create both shortcuts once on each iPhone, with exactly these names.

## Beta (iOS)

Beta features are still being tested. Turn them on per iPhone.

### Lyrics editor

| | |
| --- | --- |
| Where | iOS: Beta (shown only to accounts allowed to edit lyrics) |
| Scope | Device |

- **Why**: lyrics that come in early or late were only fixable from a computer.
- **What it does**: adds Edit lyrics to the full player's menu. While the song plays you can set a line to the current moment, nudge a line by 0.1 s, or shift the whole song by 0.5 s, then save to the server.
- **Notes**: needs the [Users can edit lyrics](#users-can-edit-lyrics) permission (or an administrator account). A downloaded copy of the lyrics is updated at once. Check the result in the player after saving.
