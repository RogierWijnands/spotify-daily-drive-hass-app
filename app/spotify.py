import base64
import hashlib
import secrets
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import requests

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-top-read user-read-recently-played user-library-read user-follow-read playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyError(RuntimeError):
    pass


class SpotifyRateLimitError(SpotifyError):
    pass


class SpotifyPartialError(SpotifyError):
    def __init__(self, error, items):
        super().__init__(str(error))
        self.items = items


def _request_error(response, message):
    # Keep Spotify's retry delay visible in logs and failed refresh history.
    if response.status_code != 429:
        return SpotifyError(message)
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        message += f"; Retry-After: {retry_after!r}"
    else:
        message += "; Retry-After not provided"
    return SpotifyRateLimitError(message)


def _spotify_id(value, kind):
    value = value.strip()
    parsed = urlparse(value)

    # Accept a Spotify URL, URI, or bare ID, then validate the extracted ID.
    if parsed.scheme == "https" and parsed.netloc == "open.spotify.com":
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == kind:
            value = parts[1]
    elif value.startswith(f"spotify:{kind}:"):
        value = value.removeprefix(f"spotify:{kind}:")

    if len(value) != 22 or not value.isascii() or not value.isalnum():
        raise ValueError(f"Invalid Spotify {kind} URL or ID")
    return value


def playlist_id(value):
    return _spotify_id(value, "playlist")


def show_id(value):
    return _spotify_id(value, "show")


def authorization(client_id):
    # PKCE binds the authorization code to this request without a client secret.
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = "https://accounts.spotify.com/authorize?" + urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    return url, verifier, state


def callback_code(url, state):
    # Only accept a callback for our redirect address and this authorization attempt.
    parsed = urlparse(url.strip())
    expected = urlparse(REDIRECT_URI)
    if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path):
        raise ValueError("The callback URL does not match the expected address")

    params = parse_qs(parsed.query)
    if len(params.get("state", [])) != 1 or not secrets.compare_digest(params["state"][0], state):
        raise ValueError("Spotify state does not match; start again")
    if params.get("error"):
        raise ValueError("Spotify authorization denied: " + params["error"][0])
    if len(params.get("code", [])) != 1:
        raise ValueError("Authorization code is missing")
    return params["code"][0]


def token_request(data):
    response = requests.post(TOKEN_URL, data=data, timeout=20)
    if not response.ok:
        raise _request_error(response, f"Spotify token request failed (HTTP {response.status_code})")
    return response.json()


class Spotify:
    def __init__(self, store):
        self.store = store
        client_id = store.get("client_id")
        refresh = store.get("refresh_token")
        if not client_id or not refresh:
            raise SpotifyError("Connect Spotify in the settings first")

        # Refresh tokens may rotate; save a replacement without clearing the cache.
        token = token_request({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
        if token.get("refresh_token"):
            store.set_many({"refresh_token": token["refresh_token"]})
        self.token = token["access_token"]

    def request(self, method, path, **kwargs):
        response = requests.request(
            method, API + path, headers={"Authorization": "Bearer " + self.token},
            timeout=30, **kwargs,
        )
        if not response.ok:
            if path == "/me/tracks" and response.status_code == 403:
                raise SpotifyError("Liked tracks are inaccessible (HTTP 403); reconnect Spotify and grant library access")
            if path == "/me/following" and response.status_code in (401, 403):
                raise SpotifyError(
                    f"Followed artists are inaccessible (HTTP {response.status_code}); "
                    "reconnect Spotify and grant access to followed artists"
                )
            raise _request_error(response, f"Spotify {method} {path} failed (HTTP {response.status_code})")
        return response.json() if response.content else {}

    def get(self, path, **params):
        return self.request("GET", path, params=params)

    def daily_cached(self, key, fetch, trace=None):
        day = datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
        cached = self.store.get_cached(key, day)

        # An empty list is a valid hit; only cache after a complete fetch.
        if cached is not None:
            if trace is not None:
                trace.append(f"{key.removesuffix('_cache').replace('_', ' ').capitalize()}: "
                             "retrieved from today's cache.")
            return cached
        result = fetch()
        self.store.set_cached(key, day, result)
        if trace is not None:
            trace.append(f"{key.removesuffix('_cache').replace('_', ' ').capitalize()}: "
                         "retrieved from Spotify and saved in today's cache.")
        return result

    def liked_tracks(self, rng):
        # Discover the library size before sampling up to five distinct pages.
        first = self.get("/me/tracks", market="NL", limit=1, offset=0)
        if not isinstance(first, dict):
            raise SpotifyError("Spotify returned an invalid liked-track page")
        total = first.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise SpotifyError("Spotify returned an invalid liked-track count")
        if not isinstance(first.get("items"), list) or len(first["items"]) != min(total, 1):
            raise SpotifyError("Spotify returned an inconsistent liked-track count")
        if total <= 1:
            return first["items"]

        page_count = (total + 49) // 50
        pages = rng.sample(range(page_count), min(5, page_count))
        result = []
        try:
            for page in pages:
                response = self.get("/me/tracks", market="NL", limit=50, offset=page * 50)
                if not isinstance(response, dict) or not isinstance(response.get("items"), list) \
                        or not response["items"]:
                    raise SpotifyError("Spotify returned an invalid sampled liked-track page")
                result.extend(response["items"])
        except (SpotifyError, requests.RequestException) as exc:
            if result:
                raise SpotifyPartialError(exc, result) from exc
            raise
        return result

    def pages(self, path, params=None, max_items=1000):
        # Follow offset-based pages in order; an incomplete read is not a full result.
        params = dict(params or {})
        params["limit"] = 50
        offset = 0
        result = []

        try:
            while True:
                page = self.get(path, **params, offset=offset)
                if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                    raise SpotifyError(f"Invalid page in {path}")
                items = page["items"]
                result.extend(items)
                if not page.get("next"):
                    return result
                if not items:
                    raise SpotifyError(f"Empty page with a next page in {path}")
                offset += len(items)
                if max_items is not None and offset >= max_items:
                    raise SpotifyError(f"Too many items in {path}; change aborted")
        except (SpotifyError, requests.RequestException) as exc:
            if result:
                raise SpotifyPartialError(exc, result) from exc
            raise

    def followed_artists(self, trace=None):
        day = datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
        key = "followed_artists_cache"

        # Keep the collected artists and scan position between refreshes and restarts.
        state = self.store.get(key, {
            "artists": {}, "cursor": None, "seen_ids": [], "seen_cursors": [],
            "day": None, "requests": 0, "completed_day": None,
        })
        state.pop("last_error", None)
        cached_ids = set(state["artists"])
        fetched_ids = set()

        def report_cache():
            if trace is not None:
                result_ids = set(state["artists"])
                reused = len(result_ids & cached_ids - fetched_ids)
                refreshed = len(result_ids & fetched_ids)
                trace.append(f"Followed artists: {reused} reused from cache; "
                             f"{refreshed} retrieved from Spotify during this refresh.")

        if state["day"] != day:
            state["day"] = day
            state["requests"] = 0

        if state["requests"] >= 5 and state["completed_day"] != day:
            if trace is not None:
                trace.append("Followed-artist daily limit of 5 requests reached; no new Spotify request.")
            report_cache()
            return list(state["artists"].values())

        # Spotify gives us a cursor for the next page, not a random page number.
        # Continue from that cursor with at most five requests per Amsterdam day.
        try:
            while state["requests"] < 5 and state["completed_day"] != day:
                params = {"type": "artist", "limit": 50}
                if state["cursor"] is not None:
                    params["after"] = state["cursor"]

                # Count the attempt before sending it, including failed requests.
                state["requests"] += 1
                self.store.set_many({key: state})
                response = self.get("/me/following", **params)
                if not isinstance(response, dict) or not isinstance(response.get("artists"), dict):
                    raise SpotifyError("Spotify returned an invalid followed-artist page")
                page = response["artists"]
                if not isinstance(page.get("items"), list) or "next" not in page or not all(
                    isinstance(artist, dict) and isinstance(artist.get("id"), str) and artist["id"]
                    for artist in page["items"]
                ):
                    raise SpotifyError("Spotify returned an invalid followed-artist page")
                if page["next"]:
                    if not page["items"]:
                        raise SpotifyError("Empty followed-artist page with a next page")
                    cursors = page.get("cursors")
                    cursor = cursors.get("after") if isinstance(cursors, dict) else None
                    # A missing or repeated cursor cannot safely advance the scan.
                    if not isinstance(cursor, str) or not cursor or cursor in state["seen_cursors"]:
                        raise SpotifyError("Spotify returned an invalid followed-artist pagination cursor")
                else:
                    cursor = None

                # Keep every discovered artist available while the scan is incomplete.
                for artist in page["items"]:
                    state["artists"][artist["id"]] = artist
                    state["seen_ids"].append(artist["id"])
                    fetched_ids.add(artist["id"])
                state["cursor"] = cursor
                if cursor is not None:
                    state["seen_cursors"].append(cursor)
                else:
                    # Only remove unfollowed artists after the whole scan succeeds.
                    # The next scan starts at the beginning on a later day.
                    seen = set(state["seen_ids"])
                    state["artists"] = {
                        artist_id: artist for artist_id, artist in state["artists"].items() if artist_id in seen
                    }
                    state["seen_ids"] = []
                    state["seen_cursors"] = []
                    state["completed_day"] = day
                self.store.set_many({key: state})
        except (SpotifyError, requests.RequestException) as exc:
            # Report the error but let selection use any artists already in the cache.
            report_cache()
            if state["artists"]:
                raise SpotifyPartialError(exc, list(state["artists"].values())) from exc
            raise
        report_cache()
        return list(state["artists"].values())

    def playlist_items(self, pid):
        entries = self.pages(f"/playlists/{pid}/items", {"additional_types": "track,episode", "market": "NL"})
        result = []

        # Unreadable items prevent a reliable before/after comparison after a timeout.
        for entry in entries:
            item = entry.get("item")
            if not item or entry.get("is_local") or not item.get("uri"):
                raise SpotifyError("Playlist contains local or unreadable items; safe replacement is not possible")
            result.append(item["uri"])
        return result

    def replace(self, pid, uris):
        return self.request("PUT", f"/playlists/{pid}/items", json={"uris": uris})
