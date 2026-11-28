# Daily Drive for Home Assistant

Combine Spotify music, discoveries, artist releases, news and podcasts in one playlist.

## Installation

1. In Home Assistant, open **Settings → Apps → Install app → menu → Repositories** and add `https://github.com/RogierWijnands/spotify-daily-drive-hass-app`.
2. Install and start **Daily Drive**, then open its web UI. Home Assistant builds the container locally.
3. Create a [Spotify Developer app](https://developer.spotify.com/dashboard) with Web API access and the exact redirect URI `http://127.0.0.1:8888/callback`. Development Mode requires Spotify Premium for the app owner; no client secret is needed.
4. Create a Spotify playlist. Under **Settings**, enter its URL and your client ID; optionally configure market, timezone, source playlists and shows.
5. Select **Start connection**, authorize Spotify and paste the full redirect URL into the app. An error page is expected: nothing needs to listen on port 8888. Do not share the URL; it contains an authorization code.
6. Select **Refresh now** and check History and the app log.

## Settings

- **Schedule:** enabled by default at 08:15, 12:15, 16:15 and 20:15. Edit times or disable it in the web UI. Enter at least one 24-hour time per line. The app must be running; missed refreshes are not retried. Manual refresh remains available.
- **Timezone and market:** default to the system timezone and `NL`. The timezone controls schedules, cache days and request counts. Spotify may prioritize the account's country.
- **Shows:** enter one Spotify show URL or ID and episode count per line, such as `https://open.spotify.com/show/SHOW_ID, 4`. Each show may appear once; the combined count is at most 99. Leave both show fields empty for music only.
- **Source playlists:** use playlists you own or collaborate on. Copies of Discover Weekly or Release Radar can be used, but do not update automatically.
- Changing market or timezone clears caches; changing timezone also clears daily request statistics. Changing Spotify client ID clears caches and disconnects Spotify. Resolve an uncertain playlist change before changing market.
- **Updates and storage:** use **Check for updates** in the app store; existing settings, caches and history are retained. Data is private in `/data/daily_drive.sqlite3`; removing app data deletes it. Repository and local installations have separate data.

## Playlist and cache

- The playlist targets 90 minutes ±2 minutes and at most 100 items. Playable episodes are mixed with music; podcasts alone can fill it. Music playability is not checked. Episodes are unique within a playlist, but previously used episodes can recur.
- Music comes from top, recent and liked tracks, optional playlists and artist discoveries. Discoveries are not Spotify recommendations. The artist pool combines top and followed artists with artists from retrieved tracks; Spotify's longest top ranges cover approximately one year.
- Top tracks, recent tracks, liked tracks, top artists and source playlists are cached per local day. Source playlist changes appear the next day. Followed artists are scanned incrementally, up to five pages per day.
- Discoveries are searched once per local day: up to eight artists and four collaborators, with up to two randomly chosen artist releases each. Results and any errors are reused that day.
- News and podcasts are checked at every refresh. Playable episodes are cached per show; fresh episodes take priority.
- If a source fails, available cached results can be reused regardless of age. Partial fresh results are combined with cached results and saved as a fallback; an empty response does not erase the last nonempty fallback. Cached episodes may be outdated. Cache is cleared on the settings changes above or when stored data is cleared.

## History, errors and safety

- History shows the latest refresh; expand earlier refreshes and load more in batches of 10. A successful playlist update with source errors shows `error` and describes cache use and available content. `failed` means the refresh stopped; `uncertain` means the playlist outcome is unknown. A show with too few episodes is not itself a Spotify error.
- Details show selection, source errors, playlist items and per-refresh Spotify API requests, token requests and cache hits. A cache hit is a reused source or track list, not necessarily one avoided request. Counts are unavailable until recorded.
- **Spotify requests today** counts started API and token requests, including failures, and records the first rate-limit/quota error. Counts persist until cleared and reset for display at local midnight. Reload to update; they are not Spotify's rolling quota.
- The target playlist is read before replacement, not from cache. Insufficient content, authentication, access or storage errors can stop preparation without replacing it. The app does not automatically retry Spotify errors.
- Do not edit the playlist during a refresh. After a lost response, the app checks the old and proposed contents. If the outcome remains uncertain, further changes are blocked: check Spotify before releasing the block.

## Testing locally

Requires Python 3.9 or newer; Home Assistant and Docker are not required.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp local-config.example.json local-config.json
```

- Configure the Spotify app and redirect URI as above. Enter your client ID and a preferably separate test playlist in `local-config.json`; replace or remove placeholder playlist URLs.
- Optional fields: `market`, `timezone`, `source_playlists`, `news_shows`, `podcast_shows`. A show entry is `{"show": "https://open.spotify.com/show/SHOW_ID", "count": 1}`.

```sh
python -m app.cli --authorize       # connect once
python -m app.cli                   # preview without changing the playlist
python -m app.cli --apply           # replace the playlist
python -m unittest discover -s tests
```

- Use `--config /path/to/config.json` for another configuration. Preview may refresh the access token; `--apply` creates a new selection.
- Local tokens and history are stored in `.daily-drive.sqlite3` next to the configuration. Protect it and do not use it from multiple processes. Never commit personal data or credentials.
- After an uncertain `--apply`, check Spotify and use `--pending` to inspect the snapshots; `--release-pending` releases the block without determining the outcome.
