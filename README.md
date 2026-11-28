# Daily Drive for Home Assistant

A Home Assistant app that combines your favorite music, discoveries, artist releases, news and podcasts in a Spotify playlist.

## Installation

1. In Home Assistant, go to **Settings → Apps → Install app → menu → Repositories**. Add `https://github.com/RogierWijnands/spotify-daily-drive-hass-app`.
2. Install and start **Daily Drive**, then select **Open web UI**. Home Assistant builds the container locally.
3. Create a [Spotify Developer app](https://developer.spotify.com/dashboard) with Web API access and register `http://127.0.0.1:8888/callback` as the exact redirect URI. Development Mode requires Spotify Premium for the app owner; no client secret is needed.
4. Create a Spotify playlist and enter its URL and your client ID in the web UI. Optionally configure the market, timezone, source playlists and [shows](#news-and-podcast-shows), then save.
5. Select **Start connection**, open the Spotify link and grant access. Copy the full redirect URL into the app, even if the browser shows an error. Do not share this URL: it contains an authorization code. Nothing needs to listen on port 8888.
6. Select **Refresh now** and check the history and app log.

## Settings and storage

- **Automatic refresh:** enabled by default at **06:00, 10:00, 14:00, 18:00 and 22:00**. Toggle it at the top of the web UI; manual refresh remains available and an ongoing refresh continues. The app must be running; missed times are not retried.
- **Timezone:** defaults to the system timezone; override it with an IANA timezone such as `Europe/Amsterdam`. Schedules, daily caches and request counts use this timezone.
- **Market:** a two-letter country code, default `NL`. Spotify determines supported markets and may prioritize the connected account's country.
- Changing the market or timezone clears daily caches; changing the timezone also clears daily request statistics. The connection and followed artists are kept. Resolve an uncertain playlist change before changing the market. Data from before `0.1.27` used Amsterdam time and may reflect the old day boundary until the next local day or data removal.
- **Updates:** select **Check for updates** in the app store after the new version is pushed to GitHub. Repository and local installations have separate data.
- **Storage:** tokens, settings and history are private in `/data/daily_drive.sqlite3`. Removing the app or its data can delete them.

## News and podcast shows

Enter one Spotify show URL or ID and episode count per line:

**News shows:**
```text
https://open.spotify.com/show/2EaVmyxrGQv0au2jjHki5v, 4
```

**Other podcast shows:**
```text
https://open.spotify.com/show/1lr6j4z70G5xke70V6nHhI, 1
https://open.spotify.com/show/3Ab6DGFJL7VE5ToIH4r6w1, 1
```

- Each show can appear once; the combined episode count is at most 99. Leave both fields empty for music only.
- After upgrading from a version without show settings, configure shows before the next refresh.

## Playlist selection

- **Music:** top, recent and liked tracks, discoveries from artist releases, and optional source playlists. Discoveries are not Spotify recommendations; not every source appears in every refresh. Spotify's longest top ranges cover approximately one year, not all time.
- **Artists:** up to 50 top artists from one randomly chosen daily time range are combined with followed artists and artists from top, recent and cached liked tracks. The pool is deduplicated and shuffled.
- **Episodes:** the latest playable news and podcast episodes are mixed with music where possible. Episodes are not duplicated within a playlist, but previously used episodes can recur.
- **Duration:** 90 minutes ±2 minutes, with a music/podcast ratio based on available content. Podcasts alone can fill the playlist. Music playability is not checked, so unavailable tracks can shorten actual playback.
- **Source playlists:** new Spotify apps can read only playlists you own or collaborate on. Copies of Discover Weekly or Release Radar can be used, but do not update automatically.

## Cache and source errors

- **Tracks and top artists:** top tracks are cached daily per time range; one top-artist range is chosen and cached daily. Recently played tracks and up to five randomly selected liked-track pages (50 tracks each) are also cached daily. If a source fails without data, its last cache can be reused, even from a previous day. Incomplete liked-track results are not cached.
- **Followed artists:** stored persistently and scanned up to five pages per day, continuing from the previous cursor. A new scan starts on a later day after the last page; unfollowed artists are removed when that scan completes. Errors preserve scan progress and cached artists.
- **Discoveries:** once per local day, search up to eight artists and four collaborators, choosing up to two random artist releases per artist. Cache the resulting tracks, including partial or empty results after errors, for the rest of the day. Changes to the artist pool take effect on the next day's search. Additional pages and fallback lookups may require extra requests.
- **Episodes:** if a show fetch fails, playable episodes fetched earlier that local day can fill missing slots; fresh episodes take priority.
- **Errors:** source errors are logged, and successfully fetched pages can still be used. History reports errors and cache use, including the date of older cache and errors from an earlier discovery fetch.

## History and requests

- **History:** shows source origins, selection details, errors and, for successful refreshes, the music count and playlist-ordered items. Successful status includes the number of source errors; a show with too few episodes is not itself an error. Failed refreshes retain available selection details. Spotify error messages, reason codes and `Retry-After` are shown when available; the app does not automatically retry.
- **Per refresh:** History shows Spotify API requests, token requests and cache hits separately, including failed and verification requests. A cache hit is a reused source or artist release track list, not necessarily one avoided request. Older entries have no counts.
- **Today:** the web UI counts API and token requests by category, including unsuccessful responses, network errors and invalid JSON. It records the first HTTP 429 or `QUOTA_EXCEEDED` response with its request number, category and available details. Counts persist across restarts, reset for display at local midnight and are cleared with stored data. Reload to update; these are app counts, not Spotify's rolling quota or remaining allowance.

## Playlist safety

- The app prepares the selection before replacing the playlist in one request, with at most 100 items. Insufficient content, inaccessible playlists, local or unreadable target items, authentication or storage errors can stop preparation without replacing it. Check the app log.
- Do not edit the playlist during a refresh. After a network error, the app compares the playlist with the old and proposed contents; a timeout does **not** guarantee the old playlist is intact.
- If the outcome remains uncertain, further changes are blocked. Check Spotify before releasing the block; history remains uncertain.

## Testing locally with Spotify

Requires Python 3.9 or newer; Home Assistant and Docker are not required.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp local-config.example.json local-config.json
```

- Set up the Spotify Developer app and redirect URI as above. In `local-config.json`, enter your client ID and playlist URL or ID; preferably use a separate test playlist.
- Optionally set `market` (default `NL`), `timezone` (`system` or an IANA timezone), `source_playlists`, `news_shows` and `podcast_shows`. A show entry looks like `{"show": "https://open.spotify.com/show/SHOW_ID", "count": 1}`; omit or empty both show arrays for music only. Replace or remove the example's placeholder playlist URLs.

```sh
python -m app.cli --authorize  # connect once: open the link and paste the full callback URL
python -m app.cli              # preview the selection without changing your playlist
python -m app.cli --apply      # actually replace the configured playlist
```

- Use `--config /path/to/config.json` to select another configuration. A preview may refresh the access token but does not change the playlist; `--apply` creates a new selection that may differ.
- Tokens and history are stored in `.daily-drive.sqlite3` next to the configuration, separately from Home Assistant. Protect it and do not use it from multiple processes. `.gitignore` excludes JSON files and SQLite databases except `local-config.example.json`; never put personal data in the example and check other extensions and locations before committing.
- After an uncertain `--apply`, the next attempt checks the playlist. If unresolved, use `python -m app.cli --pending` to view the old and proposed contents. Check Spotify before using `python -m app.cli --release-pending`; history remains uncertain.

## Automated tests

```sh
python -m unittest discover -s tests
```
