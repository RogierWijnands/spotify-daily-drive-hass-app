# Daily Drive for Home Assistant

A local Home Assistant app that fills a Spotify playlist with music and podcasts. The app runs in a container on Home Assistant OS; no external server is required.

## Installation

1. Copy the project folder to the Home Assistant local apps folder as `local_apps/daily_drive`. Depending on the Samba or SSH app used, this folder may also be called `addons`. See the [Home Assistant guide](https://contributors.home-assistant.io/apps/first-app/).
2. Go to **Settings → Apps → App Store**, select **Check for updates**, install **Daily Drive** under local apps and start it. Open the web interface through Home Assistant.
3. Create an app with Web API access on [Spotify for Developers](https://developer.spotify.com/dashboard). For Development Mode, the owner needs Spotify Premium. Add `http://127.0.0.1:8888/callback` as the exact redirect URI.
4. Create a Spotify playlist named Daily Drive. Enter the Spotify client ID and this playlist's URL in the app. Optionally add URLs for playlists you own or collaborate on, one per line, and save the settings.
5. Select **Start connection**, open the Spotify link and grant access. An error page may appear after the redirect: copy the entire URL from the address bar and paste it into the app. The URL contains a one-time authorization code: do not share it. Nothing needs to listen on port 8888.
6. Select **Refresh now** and check the history and the Home Assistant app log. Automatic refreshes run at 09:00, 14:00 and 18:00 in `Europe/Amsterdam` while the app is running. Missed refresh times are not retried.

After updating from version 0.1.0, reconnect Spotify and grant permission to read your library. Without that permission, refreshes fail and the existing playlist remains intact.

The app only requires a Spotify client ID, not a client secret. Spotify tokens, settings, refresh history and used episodes are stored in `/data/daily_drive.sqlite3`. This storage is private to the app and persists across restarts. Removing the app or its data can delete the history and connection. Statuses from earlier versions are converted to English, but existing history details remain in their original language.

## Selection and limitations

- The four latest playable episodes from the specified NU.nl news show and the latest episodes from Alledaagse Vragen and Lang Verhaal Kort take priority. News and other podcasts are mixed and separated by music where possible. An episode will not appear twice in the same playlist. Previously used episodes are recorded but are still included while they remain the latest.
- Music comes from current and older Spotify top tracks, recently played and liked tracks, releases and collaborations by favorite artists, and optional source playlists. Liked tracks are randomly selected from your library; not every refresh necessarily includes a liked track. Spotify's longest top-track range covers approximately one year and is not a complete listening history. Discoveries are not Spotify recommendations.
- New Spotify apps can only read playlists you own or collaborate on. Discover Weekly and Release Radar cannot be read directly; manually maintained copies you own can be added as sources. An inaccessible source causes the refresh to fail.
- The app aims for 120 minutes ±2 minutes. The six latest episodes take priority; if they are too long, the total duration may differ. Podcast duration determines the ratio: 70/30 is a target, not a guarantee. If there is not enough playable music to reach the target, the playlist is not replaced.
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

Create a Spotify Developer app and register `http://127.0.0.1:8888/callback` as its redirect URI, as described in the Home Assistant installation above. In `local-config.json`, enter the Spotify client ID, the URL or ID of your Daily Drive playlist and, optionally, the URLs or IDs of your own source playlists. Prefer a separate test playlist.

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
