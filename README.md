# Daily Drive for Home Assistant

Spotify used to have Daily Drive, a playlist that combines your favorite music, discoveries, new releases, the news and short informative podcasts. I loved this feature and decided to recreate it in the form of a Home Assistant app (so I don't have to pay for separate hosting).

## Installation

1. In Home Assistant, go to **Settings → Apps → Install app → menu → Repositories**. Add `https://github.com/RogierWijnands/spotify-daily-drive-hass-app`.
2. Install and start **Daily Drive**, then open its web interface. Home Assistant builds the container locally.
3. Create a [Spotify Developer app](https://developer.spotify.com/dashboard) with Web API access. Register `http://127.0.0.1:8888/callback` as the exact redirect URI. Development Mode requires Spotify Premium for the app owner.
4. Create a Spotify playlist for Daily Drive. Enter its URL and your Spotify client ID in the app. Optionally add your own source playlists and [shows](#news-and-podcast-shows), then save.
5. Select **Start connection**, open the Spotify link and grant access. Copy the full redirect URL from your browser into the app, even if an error page appears. Do not share this URL; it contains an authorization code. Nothing needs to listen on port 8888.
6. Select **Refresh now** and check the history and app log.

- No Spotify client secret is needed.
- Automatic refresh: **00:00, 04:00, 08:00, 12:00, 16:00, 20:00** (`Europe/Amsterdam`). The app must be running; missed times are not retried.
- Updates: push the new version to GitHub, then select **Check for updates** in the app store. Local and repository installations have separate data.
- After upgrading from version 0.1.0, reconnect Spotify and grant library access.
- After upgrading to version 0.1.9, reconnect Spotify and grant access to followed artists. For local use, run `python -m app.cli --authorize`.
- Tokens, settings and history are stored privately in `/data/daily_drive.sqlite3`. Removing the app or its data can delete them.

## News and podcast shows

Enter one Spotify **show** URL or ID and episode count per line, separated by a comma.

Example **News shows**:
```text
https://open.spotify.com/show/2EaVmyxrGQv0au2jjHki5v, 4
```

Example **Other podcast shows**:
```text
https://open.spotify.com/show/1lr6j4z70G5xke70V6nHhI, 1
https://open.spotify.com/show/3Ab6DGFJL7VE5ToIH4r6w1, 1
```

- Each show can appear once; the combined episode count is limited to 99.
- Leave both fields empty for music only. After upgrading from a version without show settings, configure shows before the next refresh.

## Selection and limitations

- **Episodes:** the latest playable episodes take priority. News and podcasts are mixed with music where possible. No duplicates within a playlist; previously used episodes can recur while still latest.
- **Music:** top, recent and liked tracks; releases by favorite artists and their collaborators; optional source playlists. Discoveries are not Spotify recommendations and individual sources are not guaranteed in every refresh.
- **History:** Spotify's longest top-artist and top-track ranges cover approximately one year, not all time.
- **Artists:** all available top and followed artists are combined, deduplicated and shuffled; up to eight are selected for release discovery per refresh.
- **Collaborators:** up to four are selected randomly; up to two albums or singles each are searched, favoring recent releases.
- **Source playlists:** new Spotify apps can read only playlists you own or collaborate on. Copy Discover Weekly or Release Radar into your own playlists to use them; the copies do not update automatically.
- **Duration:** target 90 minutes ±2 minutes and 70/30 music/podcasts. Episodes take priority, so duration and ratio can differ.
- **Preparation failures:** insufficient playable music or episodes, inaccessible sources, local or unreadable playlist items, API restrictions and rate limits can cause a refresh to fail without replacing the playlist. Check the app log.

## Playlist safety

- The app prepares the selection before replacing the playlist in one request, with at most 100 items. Do not edit the playlist manually during a refresh.
- After a network error, the app compares the playlist with its old and proposed contents. A timeout does **not** guarantee the old playlist is intact.
- If the outcome cannot be confirmed, further changes are blocked. Check the Spotify playlist before releasing the block in the app; history remains marked uncertain.

## Testing locally with Spotify

Requires Python 3.9 or newer; Home Assistant and Docker are not required.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp local-config.example.json local-config.json
```

- Set up the Spotify Developer app and redirect URI as above.
- In `local-config.json`, enter your client ID and Daily Drive playlist URL or ID. Prefer a separate test playlist.
- Optionally add your own `source_playlists` and shows in `news_shows` and `podcast_shows`. A show entry looks like `{"show": "https://open.spotify.com/show/SHOW_ID", "count": 1}`. Both show arrays may be empty or omitted for music only.
- Replace or remove the placeholder source playlist URLs in the example before running.

```sh
python -m app.cli --authorize  # connect once: open the link and paste the full callback URL
python -m app.cli              # preview the selection without changing your playlist
python -m app.cli --apply      # actually replace the configured playlist
```

- Use `--config /path/to/config.json` to select another configuration.
- Tokens and history are stored in `.daily-drive.sqlite3` next to the configuration. Protect it; it is separate from Home Assistant's database. Do not use the same database from multiple processes.
- `.gitignore` excludes JSON files and SQLite databases except `local-config.example.json`. Never put personal information in the example; check other extensions and locations before committing.
- A dry run may refresh the access token but does not change the playlist. Each run creates a new selection, so `--apply` may differ from the preview.
- After an uncertain `--apply`, the next attempt checks the playlist. If unresolved, use `python -m app.cli --pending` to view the old and proposed contents. Check Spotify before using `python -m app.cli --release-pending`; history remains uncertain.

## Automated tests

```sh
python -m unittest discover -s tests
```
