import logging
import os
import secrets
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, render_template_string, request
from requests import RequestException
from waitress import serve

from .refresh import Refresh
from .shows import format_show_lines, parse_show_lines, validate_shows
from .spotify import (
    REDIRECT_URI, SpotifyError, authorization, callback_code, playlist_id, token_request,
)
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
store = Store(os.environ.get("DAILY_DRIVE_DB", "/data/daily_drive.sqlite3"))
refresh = Refresh(store)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024
csrf = secrets.token_urlsafe(32)
oauth = {}
oauth_lock = threading.Lock()
REFRESH_TIMES = ("00:00", "04:00", "08:00", "12:00", "16:00", "20:00")

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
<h2>Settings</h2>
<form action="configure" method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Spotify client ID <input name="client_id" required value="{{ client_id }}"></label>
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
<h2>Refresh</h2><p>Automatically at {{ refresh_times | join(', ') }} (Europe/Amsterdam).</p>
<form action="refresh" method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Refresh now</button></form>
<h2>History</h2><table><tr><th>Started (UTC)</th><th>Status</th><th>Details</th></tr>
{% for entry in history %}<tr><td>{{ entry.started }}</td><td>{{ entry.status }}</td><td>{{ entry.detail }}</td></tr>{% endfor %}
</table></body></html>"""


@app.before_request
def ingress_only():
    # Home Assistant's ingress proxy is the only supported entry point.
    if request.remote_addr != "172.30.32.2":
        abort(403)
    if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), csrf):
        abort(403)


@app.after_request
def security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def home(message=None, status=200):
    with oauth_lock:
        auth_url = oauth.get("url")

    return render_template_string(
        PAGE,
        csrf=csrf,
        message=message,
        auth_url=auth_url,
        redirect_uri=REDIRECT_URI,
        client_id=store.get("client_id", ""),
        target=store.get("playlist_id", ""),
        sources="\n".join(store.get("source_ids", [])),
        news_shows=format_show_lines(store.get("news_shows", [])),
        podcast_shows=format_show_lines(store.get("podcast_shows", [])),
        connected=bool(store.get("refresh_token")),
        pending=store.pending(),
        history=store.history(),
        refresh_times=REFRESH_TIMES,
    ), status


@app.get("/")
def index():
    return home()


@app.post("/configure")
def configure():
    try:
        client_id = request.form.get("client_id", "").strip()
        if len(client_id) != 32 or not client_id.isascii() or not client_id.isalnum():
            raise ValueError("Invalid Spotify client ID")

        target = playlist_id(request.form.get("target", ""))
        sources = list(dict.fromkeys(
            playlist_id(line)
            for line in request.form.get("sources", "").splitlines()
            if line.strip()
        ))
        if target in sources:
            raise ValueError("Daily Drive cannot also be a source playlist")

        news, podcasts = validate_shows(
            parse_show_lines(request.form.get("news_shows", "")),
            parse_show_lines(request.form.get("podcast_shows", "")),
        )
        if not refresh.lock.acquire(blocking=False):
            raise ValueError("Wait for the current refresh to finish")

        try:
            values = {
                "client_id": client_id, "playlist_id": target, "source_ids": sources,
                "news_shows": news, "podcast_shows": podcasts,
            }
            client_changed = client_id != store.get("client_id")
            if client_changed:
                values["refresh_token"] = None
                with oauth_lock:
                    oauth.clear()
            store.set_many(values, clear_cache=client_changed)
        finally:
            refresh.lock.release()

        return home("Settings saved.")
    except ValueError as exc:
        return home(str(exc), 400)


@app.post("/authorize")
def authorize():
    client_id = store.get("client_id")
    if not client_id:
        return home("Save your client ID first.", 400)

    url, verifier, state = authorization(client_id)
    with oauth_lock:
        oauth.clear()
        oauth.update(url=url, verifier=verifier, state=state, client_id=client_id, created=time.monotonic())
    return home()


@app.post("/callback")
def callback():
    if not refresh.lock.acquire(blocking=False):
        return home("Wait for the current refresh to finish before connecting Spotify.", 409)

    try:
        with oauth_lock:
            if not oauth or time.monotonic() - oauth["created"] > 600:
                raise ValueError("Connection expired; start again")
            if oauth["client_id"] != store.get("client_id"):
                raise ValueError("Client ID changed; start again")

            code = callback_code(request.form.get("callback", ""), oauth["state"])
            data = token_request({
                "client_id": oauth["client_id"], "grant_type": "authorization_code",
                "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": oauth["verifier"],
            })
            if not data.get("refresh_token"):
                raise SpotifyError("Spotify did not return a refresh token")
            store.set_many({"refresh_token": data["refresh_token"]}, clear_cache=True)
            oauth.clear()

        return home("Spotify is connected.")
    except (ValueError, SpotifyError) as exc:
        return home(str(exc), 400)
    except RequestException:
        log.exception("Spotify connection failed")
        return home("Spotify is unreachable; try connecting again.", 502)
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
        return home("A refresh is already running.", 409)

    threading.Thread(target=run_refresh, daemon=True).start()
    return home("Refresh started; reload this page for the status.")


@app.post("/release")
def release_pending():
    if request.form.get("confirm") != "on":
        return home("First confirm that you have checked the playlist.", 400)
    if not refresh.lock.acquire(blocking=False):
        return home("A refresh is already running.", 409)

    try:
        store.release_pending()
    except ValueError as exc:
        return home(str(exc), 400)
    finally:
        refresh.lock.release()

    return home("Block released; the previous outcome remains unknown.")


def scheduler():
    tz = ZoneInfo("Europe/Amsterdam")

    while True:
        now = datetime.now(tz)
        clock = now.strftime("%H:%M")
        if clock in REFRESH_TIMES:
            # The stored key prevents duplicate runs during the same minute.
            run_refresh(now.strftime("%Y-%m-%d") + " " + clock)
        time.sleep(15)


def main():
    threading.Thread(target=scheduler, daemon=True).start()
    serve(app, host="0.0.0.0", port=8099, threads=4)


if __name__ == "__main__":
    main()
