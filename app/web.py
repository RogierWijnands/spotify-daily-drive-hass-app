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
from .spotify import REDIRECT_URI, SpotifyError, authorization, callback_code, playlist_id, token_request
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

PAGE = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Daily Drive</title><style>
body {font: 16px system-ui; max-width: 760px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5}
input, textarea {box-sizing: border-box; width: 100%; padding: .5rem; margin: .25rem 0 1rem}
button {padding: .6rem 1rem; cursor: pointer} table {border-collapse: collapse; width: 100%}
td, th {padding: .4rem; text-align: left; border-bottom: 1px solid #bbb}
.notice {padding: .7rem; background: #e9f2ee; color: #17251d} code {overflow-wrap: anywhere}
</style></head><body>
<h1>Daily Drive</h1>
{% if message %}<p class="notice">{{ message }}</p>{% endif %}
{% if pending %}<p><strong>Let op:</strong> De uitkomst van een eerdere wijziging is onzeker.
Controleer de playlist. Nieuwe wijzigingen worden geblokkeerd totdat de inhoud automatisch is bevestigd.</p>
<form action="release" method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label><input style="width:auto" type="checkbox" name="confirm" required>
Ik heb de playlist gecontroleerd en begrijp dat de eerdere uitkomst onbekend blijft.</label>
<button>Blokkade handmatig opheffen</button></form>{% endif %}
<h2>Instellingen</h2>
<form action="configure" method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Spotify client-ID <input name="client_id" required value="{{ client_id }}"></label>
<label>URL van je Daily Drive-playlist <input name="target" required value="{{ target }}"></label>
<label>Eigen bronplaylists (één Spotify-URL per regel)
<textarea name="sources" rows="4">{{ sources }}</textarea></label>
<button>Instellingen opslaan</button></form>
<h2>Spotify koppelen</h2>
<p>Status: {{ 'gekoppeld' if connected else 'niet gekoppeld' }}.
Registreer eerst <code>{{ redirect_uri }}</code> als redirect-URI in je Spotify-app.</p>
<form action="authorize" method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Koppeling starten</button></form>
{% if auth_url %}
<p><a href="{{ auth_url }}" target="_blank" rel="noopener noreferrer">Open Spotify en geef toestemming</a>.
Na toestemming kan een foutpagina verschijnen; kopieer de volledige URL uit de adresbalk en plak die hieronder.</p>
<form action="callback" method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Terugkeer-URL <input name="callback" required autocomplete="off"></label><button>Koppeling afronden</button></form>
{% endif %}
<h2>Verversen</h2><p>Automatisch om 09:00, 14:00 en 18:00 (Europe/Amsterdam).</p>
<form action="refresh" method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Nu verversen</button></form>
<h2>Geschiedenis</h2><table><tr><th>Gestart (UTC)</th><th>Status</th><th>Details</th></tr>
{% for entry in history %}<tr><td>{{ entry.started }}</td><td>{{ entry.status }}</td><td>{{ entry.detail }}</td></tr>{% endfor %}
</table></body></html>"""


@app.before_request
def ingress_only():
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
    return render_template_string(PAGE, csrf=csrf, message=message, auth_url=auth_url,
                                  redirect_uri=REDIRECT_URI, client_id=store.get("client_id", ""),
                                  target=store.get("playlist_id", ""),
                                  sources="\n".join(store.get("source_ids", [])),
                                  connected=bool(store.get("refresh_token")), pending=store.pending(),
                                  history=store.history()), status


@app.get("/")
def index():
    return home()


@app.post("/configure")
def configure():
    try:
        client_id = request.form.get("client_id", "").strip()
        if len(client_id) != 32 or not client_id.isascii() or not client_id.isalnum():
            raise ValueError("Ongeldige Spotify client-ID")
        target = playlist_id(request.form.get("target", ""))
        sources = list(dict.fromkeys(playlist_id(line) for line in request.form.get("sources", "").splitlines()
                                     if line.strip()))
        if target in sources:
            raise ValueError("Daily Drive mag niet ook een bronplaylist zijn")
        if not refresh.lock.acquire(blocking=False):
            raise ValueError("Wacht tot de huidige verversing klaar is")
        try:
            values = {"client_id": client_id, "playlist_id": target, "source_ids": sources}
            if client_id != store.get("client_id"):
                values["refresh_token"] = None
                with oauth_lock:
                    oauth.clear()
            store.set_many(values)
        finally:
            refresh.lock.release()
        return home("Instellingen opgeslagen.")
    except ValueError as exc:
        return home(str(exc), 400)


@app.post("/authorize")
def authorize():
    client_id = store.get("client_id")
    if not client_id:
        return home("Sla eerst je client-ID op.", 400)
    url, verifier, state = authorization(client_id)
    with oauth_lock:
        oauth.clear()
        oauth.update(url=url, verifier=verifier, state=state, client_id=client_id, created=time.monotonic())
    return home()


@app.post("/callback")
def callback():
    try:
        with oauth_lock:
            if not oauth or time.monotonic() - oauth["created"] > 600:
                raise ValueError("Koppeling verlopen; start opnieuw")
            if oauth["client_id"] != store.get("client_id"):
                raise ValueError("Client-ID gewijzigd; start opnieuw")
            code = callback_code(request.form.get("callback", ""), oauth["state"])
            data = token_request({"client_id": oauth["client_id"], "grant_type": "authorization_code",
                                  "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": oauth["verifier"]})
            if not data.get("refresh_token"):
                raise SpotifyError("Spotify gaf geen refresh-token terug")
            store.set_many({"refresh_token": data["refresh_token"]})
            oauth.clear()
        return home("Spotify is gekoppeld.")
    except (ValueError, SpotifyError) as exc:
        return home(str(exc), 400)
    except RequestException:
        log.exception("Spotify-koppeling mislukt")
        return home("Spotify is niet bereikbaar; probeer de koppeling opnieuw.", 502)


def run_refresh(key=None):
    try:
        refresh.run(key)
    except Exception:
        # Refresh logs and records errors; the web request must not crash the scheduler.
        pass


@app.post("/refresh")
def manual_refresh():
    if refresh.lock.locked():
        return home("Er loopt al een verversing.", 409)
    threading.Thread(target=run_refresh, daemon=True).start()
    return home("Verversing gestart; vernieuw deze pagina voor de status.")


@app.post("/release")
def release_pending():
    if request.form.get("confirm") != "on":
        return home("Bevestig eerst dat je de playlist hebt gecontroleerd.", 400)
    if not refresh.lock.acquire(blocking=False):
        return home("Er loopt al een verversing.", 409)
    try:
        store.release_pending()
    except ValueError as exc:
        return home(str(exc), 400)
    finally:
        refresh.lock.release()
    return home("Blokkade opgeheven; de eerdere uitkomst blijft onbekend.")


def scheduler():
    tz = ZoneInfo("Europe/Amsterdam")
    while True:
        now = datetime.now(tz)
        clock = now.strftime("%H:%M")
        if clock in {"09:00", "14:00", "18:00"}:
            run_refresh(now.strftime("%Y-%m-%d") + " " + clock)
        time.sleep(15)


def main():
    threading.Thread(target=scheduler, daemon=True).start()
    serve(app, host="0.0.0.0", port=8099, threads=4)


if __name__ == "__main__":
    main()
