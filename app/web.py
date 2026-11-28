import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from http import HTTPStatus

from flask import Flask, abort, render_template_string, request
from requests import RequestException
from waitress import serve

from .constants import (
    ARTIST_RELEASE_ERROR_PREFIXES, ARTIST_RELEASE_SOURCE, AUTOMATIC_REFRESH_ENABLED_KEY, BYTES_PER_MEBIBYTE,
    DEFAULT_SPOTIFY_MARKET, DEFAULT_TIMEZONE, EMPTY_ARTIST_POOL_ERROR, EMPTY_GROUP_ERROR,
    EMPTY_GROUP_HISTORY_MARKER, HISTORY_ERROR_MARKERS,
    HISTORY_LIKED_TRACKS_LABEL, HISTORY_RECENT_TRACKS_LABEL, HISTORY_SOURCE_PLAYLIST_PREFIX,
    HISTORY_STATUS_SUCCEEDED, HISTORY_TOP_ARTISTS_PREFIX,
    HISTORY_TOP_TRACKS_PREFIX, HOME_ASSISTANT_INGRESS_IP, ITEMS_DETAIL_SEPARATOR,
    LONG_TERM, MAX_REQUEST_BODY_BYTES, MEDIUM_TERM, MISSING_SPOTIFY_REFRESH_TOKEN_ERROR,
    MUSIC_GROUP_SOURCES, OAUTH_STATE_TTL_SECONDS, PREVIOUS_CHANGE_OUTCOME_UNKNOWN_MESSAGE,
    SCHEDULER_POLL_INTERVAL_SECONDS, SECURE_TOKEN_ENTROPY_BYTES,
    SELECTION_DETAIL_SEPARATOR, SHOW_EPISODE_FETCH_FAILURE_PREFIX, SHOW_HISTORY_SEPARATOR,
    SHOW_SOURCE_PREFIX, SOURCE_CACHED_PREFIX, SOURCE_ERROR_PREFIX, SOURCE_FOLLOWED_ARTISTS, SOURCE_LIKED_TRACKS,
    SOURCE_LONG_TERM_TOP_TRACKS, SOURCE_MEDIUM_TERM_TOP_TRACKS, SOURCE_PARTIAL_PREFIX,
    SOURCE_PLAYLISTS, SOURCE_RECENT_TRACKS, SOURCE_SHORT_TERM_TOP_TRACKS, SOURCE_SKIP_PREFIX,
    SOURCE_TOP_ARTISTS, SOURCE_WARNING_PREFIX, SPOTIFY_CLIENT_ID_LENGTH, SHORT_TERM,
    TOP_TIME_RANGE_LABELS, TRACKS_DETAIL_SEPARATOR, WEB_REFRESH_ALREADY_RUNNING_MESSAGE,
    ZERO_ARTIST_RELEASE_LISTS_ERROR, ZERO_FETCH_RESULTS, ZERO_FOLLOWED_ARTISTS_ERROR,
    ZERO_SHOW_EPISODES_ERROR,
)
from .refresh import Refresh
from .selection import is_source_error
from .shows import format_show_lines, parse_show_lines, validate_shows
from .spotify import (
    REDIRECT_URI, SpotifyError, authorization, callback_code, playlist_id, token_request,
    validate_market,
)
from .store import Store
from .timezones import current_time, validate_timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

store = Store(os.environ.get("DAILY_DRIVE_DB", "/data/daily_drive.sqlite3"))
refresh = Refresh(store)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BODY_BYTES
csrf = secrets.token_urlsafe(SECURE_TOKEN_ENTROPY_BYTES)

# OAuth state is transient; refresh.lock protects account changes during a refresh.
oauth = {}
oauth_lock = threading.Lock()
REFRESH_TIMES = ("06:00", "10:00", "14:00", "18:00", "22:00")

# Failures are linked only to the selection groups they could have emptied.
GROUP_ERROR_SOURCES = MUSIC_GROUP_SOURCES


def _warning_matches_source(warning, source):
    detail = warning.removeprefix(SOURCE_WARNING_PREFIX).removeprefix(SOURCE_ERROR_PREFIX)
    if source == ARTIST_RELEASE_SOURCE:
        return detail.startswith(ARTIST_RELEASE_ERROR_PREFIXES)
    if source == SOURCE_PLAYLISTS:
        return detail.startswith((
            f"{SOURCE_SKIP_PREFIX}{SOURCE_PLAYLISTS} ",
            f"{SOURCE_CACHED_PREFIX}{SOURCE_PLAYLISTS} ",
            f"{SOURCE_PARTIAL_PREFIX}{SOURCE_PLAYLISTS} ",
        ))
    if source.startswith(SHOW_SOURCE_PREFIX):
        show_id = source[len(SHOW_SOURCE_PREFIX):]
        return detail.startswith(f"{SHOW_EPISODE_FETCH_FAILURE_PREFIX}{show_id}:")
    return detail.startswith((
        f"{SOURCE_SKIP_PREFIX}{source}:",
        f"{SOURCE_CACHED_PREFIX}{source} after an error:",
        f"{SOURCE_PARTIAL_PREFIX}{source} after an error:",
    ))


def _error_sources_for_marker(line, marker, start):
    # Find the source warnings that can explain this specific zero-result message.
    if marker == ZERO_FETCH_RESULTS:
        if line.startswith(HISTORY_SOURCE_PLAYLIST_PREFIX):
            playlist = line[len(HISTORY_SOURCE_PLAYLIST_PREFIX):].partition(":")[0]
            return [f"{SOURCE_PLAYLISTS} {playlist}"] if playlist else []
        sources = (
            (f"{HISTORY_TOP_TRACKS_PREFIX}{TOP_TIME_RANGE_LABELS[SHORT_TERM]}:",
             SOURCE_SHORT_TERM_TOP_TRACKS),
            (f"{HISTORY_TOP_TRACKS_PREFIX}{TOP_TIME_RANGE_LABELS[MEDIUM_TERM]}:",
             SOURCE_MEDIUM_TERM_TOP_TRACKS),
            (f"{HISTORY_TOP_TRACKS_PREFIX}{TOP_TIME_RANGE_LABELS[LONG_TERM]}:",
             SOURCE_LONG_TERM_TOP_TRACKS),
            (HISTORY_RECENT_TRACKS_LABEL, SOURCE_RECENT_TRACKS),
            (HISTORY_LIKED_TRACKS_LABEL, SOURCE_LIKED_TRACKS),
            (HISTORY_TOP_ARTISTS_PREFIX, SOURCE_TOP_ARTISTS),
        )
        return [source for label, source in sources if line.startswith(label)]
    if marker == ZERO_FOLLOWED_ARTISTS_ERROR:
        return [SOURCE_FOLLOWED_ARTISTS]
    if marker == ZERO_SHOW_EPISODES_ERROR:
        show = line.partition(SHOW_HISTORY_SEPARATOR)[2].partition(":")[0]
        return [f"{SHOW_SOURCE_PREFIX}{show}"] if show else []
    if marker == EMPTY_ARTIST_POOL_ERROR:
        return (
            SOURCE_SHORT_TERM_TOP_TRACKS, SOURCE_MEDIUM_TERM_TOP_TRACKS,
            SOURCE_LONG_TERM_TOP_TRACKS, SOURCE_RECENT_TRACKS, SOURCE_LIKED_TRACKS,
            SOURCE_TOP_ARTISTS, SOURCE_FOLLOWED_ARTISTS, SOURCE_PLAYLISTS,
        )
    if marker == EMPTY_GROUP_ERROR:
        matched = [
            (line.rfind(f"{group}{EMPTY_GROUP_HISTORY_MARKER}", 0, start), sources)
            for group, sources in GROUP_ERROR_SOURCES.items()
            if line.rfind(f"{group}{EMPTY_GROUP_HISTORY_MARKER}", 0, start) >= 0
        ]
        return max(matched, key=lambda match: match[0])[1] if matched else []
    if marker == ZERO_ARTIST_RELEASE_LISTS_ERROR:
        return [ARTIST_RELEASE_SOURCE]
    return []


def _selection_lines_with_links(lines, entry_index):
    # Keep warning details separate, but link each inline error note to its warning.
    warning_ids = {
        index: f"history-{entry_index}-error-{index}"
        for index, line in enumerate(lines)
        if line.startswith((SOURCE_WARNING_PREFIX, SOURCE_ERROR_PREFIX))
    }
    warnings = [(index, lines[index]) for index in warning_ids]
    rendered = []

    for index, line in enumerate(lines):
        links = []
        for marker in HISTORY_ERROR_MARKERS:
            start = line.find(marker)
            while start >= 0:
                sources = _error_sources_for_marker(line, marker, start)
                target = next(
                    (warning_ids[warning_index]
                     for warning_index, warning in warnings
                     if any(_warning_matches_source(warning, source) for source in sources)),
                    None,
                )
                if target:
                    links.append((start, start + len(marker), target))
                start = line.find(marker, start + len(marker))

        parts = []
        cursor = 0
        for start, end, target in sorted(links):
            if start < cursor:
                continue
            if cursor < start:
                parts.append({"text": line[cursor:start]})
            parts.append({"text": line[start:end], "href": target})
            cursor = end
        if cursor < len(line) or not parts:
            parts.append({"text": line[cursor:]})
        rendered.append({"anchor": warning_ids.get(index), "parts": parts})
    return rendered


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Daily Drive</title><style>
body {font: 16px system-ui; max-width: 760px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5}
input, textarea {box-sizing: border-box; width: 100%; padding: .5rem; margin: .25rem 0 1rem}
button {padding: .6rem 1rem; cursor: pointer} table {border-collapse: collapse; width: 100%}
td, th {padding: .4rem; text-align: left; border-bottom: 1px solid #bbb}
.notice {padding: .7rem; background: #e9f2ee; color: #17251d} code {overflow-wrap: anywhere}
</style></head><body>
<h1>Daily Drive</h1>
{% if message %}<p class="notice">{{ message }}</p>{% endif %}
{% if pending %}<p><strong>Warning:</strong> The outcome of a previous change is uncertain.
Check the playlist. New changes are blocked until the contents can be confirmed automatically.</p>
<form action="release" method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label><input style="width:auto" type="checkbox" name="confirm" required>
I have checked the playlist and understand that the previous outcome remains unknown.</label>
<button>Release block manually</button></form>{% endif %}
<h2>Automatic refresh</h2>
<p>Automatic refresh is {{ 'enabled' if automatic_refresh_enabled else 'disabled' }}.
Scheduled times: {{ refresh_times | join(', ') }} ({{ timezone_display }}).</p>
<form action="automatic-refresh" method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="enabled" value="{{ 'off' if automatic_refresh_enabled else 'on' }}">
<button>{{ 'Disable' if automatic_refresh_enabled else 'Enable' }} automatic refresh</button></form>
<p>Changing this setting does not stop a refresh already in progress. Manual refresh remains available.</p>
<h2>Settings</h2>
<form action="configure" method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Spotify client ID <input name="client_id" required value="{{ client_id }}"></label>
<label>Spotify market (two-letter country code)
<input name="market" required maxlength="2" value="{{ market }}"></label>
<label>Timezone (<code>system</code> or an IANA timezone, e.g. <code>Europe/Amsterdam</code>)
<input name="timezone" required value="{{ timezone }}"></label>
<p>Changing the market or timezone clears daily caches. Changing the timezone also clears
request statistics because the boundaries of each day change.</p>
<label>Daily Drive playlist URL <input name="target" required value="{{ target }}"></label>
<label>Source playlists you own (one Spotify URL per line)
<textarea name="sources" rows="4">{{ sources }}</textarea></label>
<label>News shows (one Spotify show URL and episode count per line, separated by a comma)
<textarea name="news_shows" rows="3" placeholder="https://open.spotify.com/show/SHOW_ID, 4">{{ news_shows }}</textarea></label>
<label>Other podcast shows (one Spotify show URL and episode count per line, separated by a comma)
<textarea name="podcast_shows" rows="3" placeholder="https://open.spotify.com/show/SHOW_ID, 1">{{ podcast_shows }}</textarea></label>
{% if not news_shows and not podcast_shows %}<p>No shows configured: refreshes will contain only music.</p>{% endif %}
<button>Save settings</button></form>
<h2>Connect Spotify</h2>
<p>Status: {{ 'connected' if connected else 'not connected' }}.
First register <code>{{ redirect_uri }}</code> as a redirect URI in your Spotify app.</p>
<form action="authorize" method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Start connection</button></form>
{% if auth_url %}
<p><a href="{{ auth_url }}" target="_blank" rel="noopener noreferrer">Open Spotify and grant access</a>.
An error page may appear after authorization; copy the full URL from the address bar and paste it below.</p>
<form action="callback" method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Callback URL <input name="callback" required autocomplete="off"></label><button>Complete connection</button></form>
{% endif %}
<h2>Refresh</h2>
<form action="refresh" method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Refresh now</button></form>
<h2>History</h2><table><tr><th>Started (UTC)</th><th>Status</th><th>Details</th></tr>
{% for entry in history %}<tr><td>{{ entry.started }}</td><td>{{ entry.status }}{% if entry.status == succeeded_status and entry.error_count %} ({{ entry.error_count }} error{{ "s" if entry.error_count != 1 else "" }}){% endif %}</td><td>{{ entry.summary }}
{% if entry.request_stats %}<br>Spotify API requests: {{ entry.request_stats.api_requests }};
token requests: {{ entry.request_stats.token_requests }}; cache hits: {{ entry.request_stats.cache_hits }}.{% endif %}
{% if entry.selection %}<details><summary>How this playlist was selected</summary><ul>
{% for line in entry.selection %}
<li{% if line.anchor %} id="{{ line.anchor }}"{% endif %}>{% for part in line.parts -%}
{% if part.href -%}
<a href="#{{ part.href }}">{{ part.text }}</a>
{%- else -%}
{{ part.text }}
{%- endif -%}
{%- endfor %}</li>
{% endfor %}
</ul></details>{% endif %}
{% if entry.items_list %}<details><summary>Show playlist items</summary><ol>
{% for item in entry.items_list %}<li>{{ item }}</li>{% endfor %}
</ol></details>{% endif %}</td></tr>{% endfor %}
</table>
<p>Cache hits count reused cached sources or artist release track lists, not necessarily requests avoided.
Token requests are counted separately from Spotify API requests.</p>
<h2>Spotify requests today</h2>
<p>{{ request_day }} ({{ timezone_display }}): {{ request_overview.requests }} request{{ "s" if request_overview.requests != 1 else "" }},
{{ request_overview.errors }} error{{ "s" if request_overview.errors != 1 else "" }}. Includes Spotify API and token requests.
Counts start when a request is started; errors include unsuccessful responses and network failures.
Reload this page to update the overview.</p>
{% if request_overview.first_quota %}
<p>First observed quota or rate-limit error: request {{ request_overview.first_quota.number }} of the day,
request {{ request_overview.first_quota.category_number }} for
{{ request_overview.first_quota.category }}, started at {{ request_overview.first_quota.time }}.
Request details: {{ request_overview.first_quota.detail }}.</p>
{% else %}<p>No quota or rate-limit error recorded today.</p>{% endif %}
{% if request_overview.categories %}
<table><tr><th>Category</th><th>Requests</th><th>Errors</th></tr>
{% for category in request_overview.categories %}
<tr><td>{{ category.category }}</td><td>{{ category.requests }}</td><td>{{ category.errors }}</td></tr>
{% endfor %}</table>
{% else %}<p>No requests recorded today.</p>{% endif %}
<p>These are app request counts, not Spotify's rolling quota or remaining allowance.</p>
<p>Artist release and artist release track requests are only needed when the daily discovery cache is filled;
later refreshes reuse it.</p>
<h2>Storage</h2>
<p>SQLite database: {{ storage_bytes }} bytes ({{ "%.2f" | format(storage_mib) }} MiB).
Temporary SQLite journal files are not included.</p>
<p>Clear cached tracks and artists, refresh history, request statistics and used episodes. Settings and the Spotify
connection are kept. This cannot be undone and does not change the Spotify playlist.</p>
<form action="clear-storage" method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label><input style="width:auto" type="checkbox" name="confirm" required>
I understand that cached data and history will be deleted.</label>
<button>Clear stored data</button></form>
</body></html>"""


@app.before_request
def ingress_only():
    # Only accept Home Assistant ingress requests; POST forms also need the CSRF token.
    if request.remote_addr != HOME_ASSISTANT_INGRESS_IP:
        abort(HTTPStatus.FORBIDDEN)
    if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), csrf):
        abort(HTTPStatus.FORBIDDEN)


@app.after_request
def security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def home(message=None, status=HTTPStatus.OK):
    with oauth_lock:
        auth_url = oauth.get("url")

    history = store.history()
    for entry_index, entry in enumerate(history):
        summary, separator, items = entry["detail"].partition(ITEMS_DETAIL_SEPARATOR)
        if not separator:
            summary, separator, items = entry["detail"].partition(TRACKS_DETAIL_SEPARATOR)
        summary, selection_separator, selection = summary.partition(SELECTION_DETAIL_SEPARATOR)
        selection_lines = selection.splitlines() if selection_separator else []
        entry["summary"] = summary

        # Older history used "Source warning" for errors too; keep its stored text unchanged.
        error_count = 0
        display_lines = []
        for line in selection_lines:
            if line.startswith((SOURCE_WARNING_PREFIX, SOURCE_ERROR_PREFIX)):
                detail = line.removeprefix(SOURCE_WARNING_PREFIX).removeprefix(SOURCE_ERROR_PREFIX)
                if is_source_error(detail):
                    error_count += 1
                    line = SOURCE_ERROR_PREFIX + detail
            display_lines.append(line)
        entry["error_count"] = error_count
        entry["selection"] = _selection_lines_with_links(display_lines, entry_index)
        entry["items_list"] = items.splitlines() if separator else []
        entry["request_stats"] = (
            {key: entry[key] for key in ("api_requests", "token_requests", "cache_hits")}
            if all(entry.get(key) is not None for key in ("api_requests", "token_requests", "cache_hits"))
            else None
        )

    now = current_time(store, datetime)
    timezone = store.get("timezone", DEFAULT_TIMEZONE)
    timezone_display = f"{timezone} ({now.strftime('%Z %z').strip()})"
    request_day = now.date().isoformat()
    request_overview = store.request_overview(request_day)
    if request_overview["first_quota"]:
        request_overview["first_quota"]["time"] = datetime.fromisoformat(
            request_overview["first_quota"]["started"]
        ).strftime("%H:%M:%S %z")
    storage_bytes = os.path.getsize(store.path)
    return render_template_string(
        PAGE,
        csrf=csrf,
        message=message,
        auth_url=auth_url,
        redirect_uri=REDIRECT_URI,
        client_id=store.get("client_id", ""),
        market=store.get("market", DEFAULT_SPOTIFY_MARKET),
        target=store.get("playlist_id", ""),
        sources="\n".join(store.get("source_ids", [])),
        news_shows=format_show_lines(store.get("news_shows", [])),
        podcast_shows=format_show_lines(store.get("podcast_shows", [])),
        connected=bool(store.get("refresh_token")),
        pending=store.pending(),
        history=history,
        succeeded_status=HISTORY_STATUS_SUCCEEDED,
        automatic_refresh_enabled=store.automatic_refresh_enabled(),
        request_day=request_day,
        request_overview=request_overview,
        refresh_times=REFRESH_TIMES,
        timezone=timezone,
        timezone_display=timezone_display,
        storage_bytes=storage_bytes,
        storage_mib=storage_bytes / BYTES_PER_MEBIBYTE,
    ), status


@app.get("/")
def index():
    return home()


@app.post("/automatic-refresh")
def configure_automatic_refresh():
    enabled = request.form.get("enabled")
    if enabled not in ("on", "off"):
        return home("Invalid automatic refresh setting.", HTTPStatus.BAD_REQUEST)
    try:
        store.set_many({AUTOMATIC_REFRESH_ENABLED_KEY: enabled == "on"})
    except sqlite3.Error:
        log.exception("Failed to save automatic refresh setting")
        return home("Could not save automatic refresh setting; check the app log.",
                    HTTPStatus.INTERNAL_SERVER_ERROR)
    return home(f"Automatic refresh {'enabled' if enabled == 'on' else 'disabled'}.")


@app.post("/configure")
def configure():
    try:
        client_id = request.form.get("client_id", "").strip()
        if (len(client_id) != SPOTIFY_CLIENT_ID_LENGTH
                or not client_id.isascii() or not client_id.isalnum()):
            raise ValueError("Invalid Spotify client ID")

        target = playlist_id(request.form.get("target", ""))
        market = validate_market(request.form.get("market", store.get("market", DEFAULT_SPOTIFY_MARKET)))
        timezone = validate_timezone(request.form.get("timezone", store.get("timezone", DEFAULT_TIMEZONE)))
        sources = list(dict.fromkeys(
            playlist_id(line)
            for line in request.form.get("sources", "").splitlines()
            if line.strip()
        ))
        if target in sources:
            raise ValueError("Daily Drive cannot also be a source playlist")

        # Validate all settings before saving any of them.
        news, podcasts = validate_shows(
            parse_show_lines(request.form.get("news_shows", "")),
            parse_show_lines(request.form.get("podcast_shows", "")),
        )
        if not refresh.lock.acquire(blocking=False):
            raise ValueError("Wait for the current refresh to finish")

        try:
            values = {
                "client_id": client_id, "playlist_id": target, "source_ids": sources,
                "news_shows": news, "podcast_shows": podcasts, "market": market, "timezone": timezone,
            }
            client_changed = client_id != store.get("client_id")
            market_changed = market != store.get("market", DEFAULT_SPOTIFY_MARKET)
            timezone_changed = timezone != store.get("timezone", DEFAULT_TIMEZONE)
            if market_changed and store.pending():
                raise ValueError(
                    "An uncertain playlist change is still pending; keep the original market first"
                )
            if client_changed:
                values["refresh_token"] = None
                with oauth_lock:
                    oauth.clear()
            store.set_many(
                values, clear_cache=client_changed,
                clear_daily_cache=market_changed or timezone_changed,
                clear_request_stats=timezone_changed,
            )
        finally:
            refresh.lock.release()

        return home("Settings saved.")
    except ValueError as exc:
        return home(str(exc), HTTPStatus.BAD_REQUEST)


@app.post("/authorize")
def authorize():
    client_id = store.get("client_id")
    if not client_id:
        return home("Save your client ID first.", HTTPStatus.BAD_REQUEST)

    # Keep the PKCE verifier and state only in memory until the callback arrives.
    url, verifier, state = authorization(client_id)
    with oauth_lock:
        oauth.clear()
        oauth.update(
            url=url, verifier=verifier, state=state, client_id=client_id,
            created=time.monotonic(),
        )
    return home()


@app.post("/callback")
def callback():
    # Keep the same lock order as configure: refresh lock before OAuth lock.
    if not refresh.lock.acquire(blocking=False):
        return home(
            "Wait for the current refresh to finish before connecting Spotify.",
            HTTPStatus.CONFLICT,
        )

    try:
        with oauth_lock:
            if not oauth or time.monotonic() - oauth["created"] > OAUTH_STATE_TTL_SECONDS:
                raise ValueError("Connection expired; start again")
            if oauth["client_id"] != store.get("client_id"):
                raise ValueError("Client ID changed; start again")

            code = callback_code(request.form.get("callback", ""), oauth["state"])
            data = token_request({
                "client_id": oauth["client_id"], "grant_type": "authorization_code",
                "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": oauth["verifier"],
            }, store)
            if not data.get("refresh_token"):
                raise SpotifyError(MISSING_SPOTIFY_REFRESH_TOKEN_ERROR)
            store.set_many({"refresh_token": data["refresh_token"]}, clear_cache=True)
            oauth.clear()

        return home("Spotify is connected.")
    except (ValueError, SpotifyError) as exc:
        return home(str(exc), HTTPStatus.BAD_REQUEST)
    except RequestException:
        log.exception("Spotify connection failed")
        return home(
            "Spotify is unreachable; try connecting again.", HTTPStatus.BAD_GATEWAY
        )
    finally:
        refresh.lock.release()


def run_refresh(key=None):
    try:
        refresh.run(key)
    except Exception:
        # Refresh logs and records errors; the web request must not crash the scheduler.
        pass


@app.post("/refresh")
def manual_refresh():
    if refresh.lock.locked():
        return home(WEB_REFRESH_ALREADY_RUNNING_MESSAGE, HTTPStatus.CONFLICT)

    threading.Thread(target=run_refresh, daemon=True).start()
    return home("Refresh started; reload this page for the status.")


@app.post("/release")
def release_pending():
    if request.form.get("confirm") != "on":
        return home(
            "First confirm that you have checked the playlist.", HTTPStatus.BAD_REQUEST
        )
    if not refresh.lock.acquire(blocking=False):
        return home(WEB_REFRESH_ALREADY_RUNNING_MESSAGE, HTTPStatus.CONFLICT)

    try:
        store.release_pending()
    except ValueError as exc:
        return home(str(exc), HTTPStatus.BAD_REQUEST)
    finally:
        refresh.lock.release()

    return home(PREVIOUS_CHANGE_OUTCOME_UNKNOWN_MESSAGE)


@app.post("/clear-storage")
def clear_storage():
    if request.form.get("confirm") != "on":
        return home(
            "First confirm that stored data will be deleted.", HTTPStatus.BAD_REQUEST
        )
    if not refresh.lock.acquire(blocking=False):
        return home(
            "Wait for the current refresh to finish before clearing data.",
            HTTPStatus.CONFLICT,
        )

    try:
        try:
            store.clear_data()
        except ValueError as exc:
            return home(str(exc), HTTPStatus.CONFLICT)
        except sqlite3.Error:
            log.exception("Failed to clear stored data")
            return home(
                "Could not clear stored data; check the app log.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        try:
            store.compact()
        except sqlite3.Error:
            log.exception("Failed to compact database after clearing stored data")
            return home(
                "Data was cleared, but storage could not be reclaimed; check the app log.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return home("Stored data cleared. Settings and the Spotify connection were kept.")
    finally:
        refresh.lock.release()


def scheduler():
    # Check local time regularly; the stored key prevents duplicate scheduled runs.
    while True:
        try:
            enabled = store.automatic_refresh_enabled()
            now = current_time(store, datetime) if enabled else None
        except (sqlite3.Error, ValueError):
            log.exception("Could not read the scheduler settings")
            time.sleep(SCHEDULER_POLL_INTERVAL_SECONDS)
            continue
        if now is not None:
            clock = now.strftime("%H:%M")
            if clock in REFRESH_TIMES:
                run_refresh(now.strftime("%Y-%m-%d") + " " + clock)
        time.sleep(SCHEDULER_POLL_INTERVAL_SECONDS)


def main():
    threading.Thread(target=scheduler, daemon=True).start()
    serve(app, host="0.0.0.0", port=8099, threads=4)


if __name__ == "__main__":
    main()
