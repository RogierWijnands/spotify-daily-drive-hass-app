# Daily Drive for Home Assistant

A local Home Assistant app that fills a Spotify playlist with music and podcasts. The app runs in a container on Home Assistant OS; no external server is required.

## Installation

1. In Home Assistant, go to **Settings → Apps**, select **Install app**, then select **Repositories** from the top-right menu. Add `https://github.com/RogierWijnands/spotify-daily-drive-hass-app`.
2. Find **Daily Drive** under the added repository in the app store, install it and start it. Open the web interface through Home Assistant. The Raspberry Pi builds the container locally during installation and updates.
3. Create an app with Web API access on [Spotify for Developers](https://developer.spotify.com/dashboard). For Development Mode, the owner needs Spotify Premium. Add `http://127.0.0.1:8888/callback` as the exact redirect URI.
4. Create a Spotify playlist named Daily Drive. Enter the Spotify client ID and this playlist's URL in the app. Optionally add URLs for playlists you own or collaborate on, one per line. Configure news and other podcast shows as described below, then save the settings.
5. Select **Start connection**, open the Spotify link and grant access. An error page may appear after the redirect: copy the entire URL from the address bar and paste it into the app. The URL contains a one-time authorization code: do not share it. Nothing needs to listen on port 8888.
6. Select **Refresh now** and check the history and the Home Assistant app log. Automatic refreshes run at 00:00, 04:00, 08:00, 12:00, 16:00 and 20:00 in `Europe/Amsterdam` while the app is running. Missed refresh times are not retried.

After updating from version 0.1.0, reconnect Spotify and grant permission to read your library. Without that permission, refreshes fail and the existing playlist remains intact.

The repository changes must be committed and pushed to GitHub before Home Assistant can install them. To update the app, push a new version and select **Check for updates** in the app store. A local app installation and an installation from this repository are separate apps with separate data.

The app only requires a Spotify client ID, not a client secret. Spotify tokens, settings, refresh history and used episodes are stored in `/data/daily_drive.sqlite3`. This storage is private to the app and persists across restarts. Removing the app or its data can delete the history and connection.

## News and podcast shows

In the app's settings, add one Spotify **show** URL or ID and the desired number of latest episodes per line, separated by a comma. Configure news and other podcasts in their respective fields. For example, these reproduce the earlier selection:

News shows:
```text
https://open.spotify.com/show/2EaVmyxrGQv0au2jjHki5v, 4
```

Other podcast shows:
```text
https://open.spotify.com/show/1lr6j4z70G5xke70V6nHhI, 1
https://open.spotify.com/show/3Ab6DGFJL7VE5ToIH4r6w1, 1
```

Each show can be configured only once. The combined episode count must be between 1 and 99 when shows are configured. Leave both fields empty for music only. After updating from an earlier version, configure the shows before the next refresh; existing installations have no shows configured by default.

## Selection and limitations

- The configured number of latest playable episodes from each show takes priority. News and other podcasts are mixed and separated by music where possible. An episode will not appear twice in the same playlist. Previously used episodes are recorded but are still included while they remain the latest. If a show does not have enough playable episodes, the refresh fails without replacing the playlist.
- Music comes from current and older Spotify top tracks, recently played and liked tracks, releases and collaborations by favorite artists, releases by their collaborators, and optional source playlists. Up to four collaborators are randomly selected from tracks that credit a favorite artist; up to two albums or singles per collaborator are searched, favoring recent releases. These tracks are candidates, not guaranteed additions. Liked tracks are randomly selected from your library; not every refresh necessarily includes a liked track. Spotify's longest top-track range covers approximately one year and is not a complete listening history. Discoveries are not Spotify recommendations.
- New Spotify apps can only read playlists you own or collaborate on. Discover Weekly and Release Radar cannot be read directly; manually maintained copies you own can be added as sources. An inaccessible source causes the refresh to fail.
- The app aims for 90 minutes ±2 minutes, distributing music evenly around the episodes. Configured episodes take priority; if they leave too little room for music, the total duration may differ. Podcast duration determines the ratio: 70/30 is a target, not a guarantee. If there is not enough playable music to reach the target, the playlist is not replaced.
- The entire selection is prepared before replacing the playlist with a single Spotify request of at most 100 items. After a network error, the app checks whether Spotify contains the old or the new contents. If it cannot determine this, the outcome remains uncertain and further changes are blocked until the contents can be confirmed automatically. If that fails, check the playlist in Spotify and explicitly release the block in the app; the history will still show the previous outcome as uncertain. A timeout cannot guarantee that the existing playlist was left unchanged. Do not edit the playlist manually at the same time. Playlists containing local or unreadable items are not replaced.
- Spotify may restrict API access or impose rate limits. Check the app log for errors; the existing playlist is not changed if preparation fails.

## Testing locally with Spotify

This works without Home Assistant or Docker, using Python 3.9 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp local-config.example.json local-config.json
```

Create a Spotify Developer app and register `http://127.0.0.1:8888/callback` as its redirect URI, as described in the Home Assistant installation above. In `local-config.json`, enter the Spotify client ID, the URL or ID of your Daily Drive playlist and, optionally, the URLs or IDs of your own source playlists. Add shows under `news_shows` and `podcast_shows`, for example `{"show": "https://open.spotify.com/show/SHOW_ID", "count": 1}`. Both arrays may be empty or omitted for music only. Prefer a separate test playlist.

```sh
python -m app.cli --authorize  # connect once: open the link and paste the full callback URL
python -m app.cli              # preview the selection without changing your playlist
python -m app.cli --apply      # actually replace the configured playlist
```

Use `--config /path/to/config.json` for a different configuration file. The app stores tokens and history in `.daily-drive.sqlite3` next to that file; protect this database and do not share it. `.gitignore` excludes JSON files and SQLite databases, except for the example file `local-config.example.json`: do not put personal information in it. Check that configurations with other extensions, alternative locations and other sensitive files do not enter version control. The local database is separate from Home Assistant's; do not use the same database from multiple processes at once.

Each run creates a new random selection. `--apply` uses the same refresh and recovery logic as the Home Assistant app, but does not necessarily apply the previously displayed selection. A dry run reads Spotify data and refreshes the access token if needed, but does not change the playlist. After an uncertain `--apply` outcome, the next `--apply` first checks the playlist. If it does not match the old or new contents, view both with `python -m app.cli --pending`. Check the playlist in Spotify and, if necessary, manually release the block with `python -m app.cli --release-pending`; the previous outcome will remain uncertain in the history.

## Automated tests

With Python 3.9 or newer and the dependencies in `requirements.txt`:

```sh
python -m unittest discover -s tests
```
