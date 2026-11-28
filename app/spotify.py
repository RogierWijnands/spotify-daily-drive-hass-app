import base64
import hashlib
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from http import HTTPStatus
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .constants import (
    API_REQUEST_TIMEOUT_SECONDS, ARTIST_DISCOVERY_RELEASE_GROUPS,
    CACHE_KEY_SUFFIX, COLLABORATOR_ARTIST_RELEASE_GROUPS, DEFAULT_SPOTIFY_MARKET, FOLLOWED_ARTISTS_CACHE_KEY,
    MAX_FOLLOWED_ARTISTS_REQUESTS_PER_DAY,
    MAX_LIKED_TRACK_PAGES_PER_REFRESH, MAX_PAGINATED_ITEMS, PKCE_VERIFIER_ENTROPY_BYTES,
    REQUEST_CATEGORY_ARTIST_RELEASES, REQUEST_CATEGORY_FOLLOWED_ARTISTS,
    REQUEST_CATEGORY_LIKED_TRACKS, REQUEST_CATEGORY_OTHER, REQUEST_CATEGORY_PLAYLIST_READS,
    REQUEST_CATEGORY_PLAYLIST_UPDATES, REQUEST_CATEGORY_RECENT_TRACKS,
    REQUEST_CATEGORY_ARTIST_RELEASE_TRACKS_KEY, REQUEST_CATEGORY_SHOW_EPISODES, REQUEST_CATEGORY_TOKEN,
    REQUEST_CATEGORY_TOP_ARTISTS, REQUEST_CATEGORY_TOP_TRACKS, SECURE_TOKEN_ENTROPY_BYTES,
    SOURCE_FOLLOWED_ARTISTS, SPOTIFY_FOLLOWED_ARTISTS_PATH, SPOTIFY_ID_LENGTH,
    SPOTIFY_LIKED_TRACKS_PATH, SPOTIFY_MARKET_CODE_LENGTH, SPOTIFY_PAGE_SIZE,
    SPOTIFY_PLAYLIST_ITEMS_PATH, SPOTIFY_QUOTA_EXCEEDED_REASON, SPOTIFY_RECENT_TRACKS_PATH,
    SPOTIFY_TOP_ITEMS_PATH, TOKEN_REQUEST_TIMEOUT_SECONDS, TOP_ARTISTS_TIME_RANGE_CACHE_KEY,
    TOP_TIME_RANGE_LABELS, ZERO_FETCH_RESULTS,
)
from .request_stats import record_cache_hit, record_request
from .timezones import current_time

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = " ".join(
    (
        "user-top-read",
        "user-read-recently-played",
        "user-library-read",
        "user-follow-read",
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-private",
        "playlist-modify-public",
    )
)
API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
log = logging.getLogger(__name__)


class SpotifyError(RuntimeError):
    pass


class SpotifyRateLimitError(SpotifyError):
    pass


class SpotifyPartialError(SpotifyError):
    def __init__(self, error, items):
        super().__init__(str(error))
        self.items = items


class SpotifyCachedError(SpotifyPartialError):
    def __init__(self, error, items, cached_items):
        super().__init__(error, items)
        self.cached_items = cached_items


def source_item_uri(entry):
    if not isinstance(entry, dict):
        return None
    item = entry.get("track") or entry.get("item") or entry
    if isinstance(item, dict) and isinstance(item.get("uri"), str):
        return item["uri"]
    return None


def _request_category(method, path, params):
    if path in (SPOTIFY_TOP_ITEMS_PATH.format(kind="tracks"),
                SPOTIFY_TOP_ITEMS_PATH.format(kind="artists")):
        kind = REQUEST_CATEGORY_TOP_TRACKS if path.endswith("/tracks") else REQUEST_CATEGORY_TOP_ARTISTS
        time_range = (params or {}).get("time_range")
        if isinstance(time_range, str) and time_range in TOP_TIME_RANGE_LABELS:
            return f"{kind} ({TOP_TIME_RANGE_LABELS[time_range]})"
        return kind

    if path == SPOTIFY_RECENT_TRACKS_PATH:
        return REQUEST_CATEGORY_RECENT_TRACKS
    if path == SPOTIFY_LIKED_TRACKS_PATH:
        return REQUEST_CATEGORY_LIKED_TRACKS
    if path == SPOTIFY_FOLLOWED_ARTISTS_PATH:
        return REQUEST_CATEGORY_FOLLOWED_ARTISTS

    parts = path.strip("/").split("/")
    if len(parts) == 3:
        if parts[0] == "shows" and parts[2] == "episodes":
            return REQUEST_CATEGORY_SHOW_EPISODES
        if parts[0] == "artists" and parts[2] == "albums":
            return REQUEST_CATEGORY_ARTIST_RELEASES
        if parts[0] == "albums" and parts[2] == "tracks":
            return REQUEST_CATEGORY_ARTIST_RELEASE_TRACKS_KEY
        if parts[0] == "playlists" and parts[2] == "items":
            return REQUEST_CATEGORY_PLAYLIST_READS if method == "GET" else REQUEST_CATEGORY_PLAYLIST_UPDATES

    return REQUEST_CATEGORY_OTHER


def _request_detail(method, path, params, category):
    labels = {
        REQUEST_CATEGORY_SHOW_EPISODES: "show",
        REQUEST_CATEGORY_ARTIST_RELEASES: "artist",
        REQUEST_CATEGORY_ARTIST_RELEASE_TRACKS_KEY: "artist release",
        REQUEST_CATEGORY_PLAYLIST_READS: "playlist",
        REQUEST_CATEGORY_PLAYLIST_UPDATES: "playlist",
    }
    detail = {"GET": "Read", "PUT": "Update"}.get(method, method)

    if category in labels:
        parts = path.strip("/").split("/")
        resource_id = parts[1]
        if resource_id.isascii() and resource_id.isalnum():
            detail += f"; {labels[category]} ID {resource_id}"

    # Only keep known, non-secret parameters needed to identify the failed request.
    for key in ("offset", "limit"):
        value = (params or {}).get(key)
        if type(value) is int and value >= 0:
            detail += f"; {key} {value}"

    artist_release_types = {
        ARTIST_DISCOVERY_RELEASE_GROUPS: "albums, singles and appearances",
        COLLABORATOR_ARTIST_RELEASE_GROUPS: "albums and singles",
    }
    groups = (params or {}).get("include_groups")
    if category == REQUEST_CATEGORY_ARTIST_RELEASES and isinstance(groups, str) and groups in artist_release_types:
        detail += f"; artist release types: {artist_release_types[groups]}"

    after = (params or {}).get("after")
    if (category == REQUEST_CATEGORY_FOLLOWED_ARTISTS and isinstance(after, str)
            and after.isascii() and after.isalnum()):
        detail += f"; after artist ID {after}"
    return detail


def _is_quota_response(response):
    if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        return True
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    return isinstance(error, dict) and error.get("reason") == SPOTIFY_QUOTA_EXCEEDED_REASON


@contextmanager
def _tracked_request(store, category, detail):
    try:
        started = current_time(store, datetime)
        request_info = store.start_request(category, started, detail)
    except sqlite3.Error:
        # A statistics failure must not stop or repeat a Spotify playlist write.
        log.exception("Could not record Spotify request")
        request_info = None

    outcome = {"quota": False}
    try:
        record_request(token=category == REQUEST_CATEGORY_TOKEN)
        yield outcome
    except (SpotifyError, requests.RequestException, ValueError):
        if request_info is not None:
            try:
                store.fail_request(request_info, quota=outcome["quota"])
            except sqlite3.Error:
                log.exception("Could not record Spotify request failure")
        raise


def _request_error(response, message, sensitive_values=()):
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        fields = (("Message", error.get("message")), ("Reason", error.get("reason")))
    elif isinstance(error, str):
        fields = (("Error", error), ("Description", body.get("error_description")))
    else:
        fields = ()
    for label, value in fields:
        if isinstance(value, str) and value.strip():
            for sensitive in sensitive_values:
                if isinstance(sensitive, str) and sensitive:
                    value = value.replace(sensitive, "[redacted]")
            message += f"; {label}: {' '.join(value.split())}"

    if response.status_code != HTTPStatus.TOO_MANY_REQUESTS:
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

    if len(value) != SPOTIFY_ID_LENGTH or not value.isascii() or not value.isalnum():
        raise ValueError(f"Invalid Spotify {kind} URL or ID")
    return value


def validate_market(value):
    if not isinstance(value, str):
        raise ValueError("Spotify market must be a two-letter country code")
    value = value.strip()
    if len(value) != SPOTIFY_MARKET_CODE_LENGTH or not value.isascii() or not value.isalpha():
        raise ValueError("Spotify market must be a two-letter country code")
    return value.upper()


def playlist_id(value):
    return _spotify_id(value, "playlist")


def show_id(value):
    return _spotify_id(value, "show")


def authorization(client_id):
    # PKCE binds the authorization code to this request without a client secret.
    verifier = secrets.token_urlsafe(PKCE_VERIFIER_ENTROPY_BYTES)
    state = secrets.token_urlsafe(SECURE_TOKEN_ENTROPY_BYTES)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
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
    callback_address = parsed.scheme, parsed.netloc, parsed.path
    expected_address = expected.scheme, expected.netloc, expected.path
    if callback_address != expected_address:
        raise ValueError("The callback URL does not match the expected address")

    params = parse_qs(parsed.query)
    if len(params.get("state", [])) != 1 or not secrets.compare_digest(params["state"][0], state):
        raise ValueError("Spotify state does not match; start again")
    if params.get("error"):
        raise ValueError("Spotify authorization denied: " + params["error"][0])
    if len(params.get("code", [])) != 1:
        raise ValueError("Authorization code is missing")
    return params["code"][0]


def token_request(data, store):
    grant_type = data.get("grant_type")
    detail = {
        "refresh_token": "Token refresh",
        "authorization_code": "Authorization code exchange",
    }.get(grant_type, "Token request")
    with _tracked_request(store, REQUEST_CATEGORY_TOKEN, detail) as outcome:
        response = requests.post(TOKEN_URL, data=data, timeout=TOKEN_REQUEST_TIMEOUT_SECONDS)
        if not response.ok:
            outcome["quota"] = _is_quota_response(response)
            sensitive = [
                data.get(key) for key in ("code", "code_verifier", "refresh_token", "client_secret")
            ]
            raise _request_error(
                response, f"Spotify token request failed (HTTP {response.status_code})", sensitive
            )
        return response.json()


class Spotify:
    market = DEFAULT_SPOTIFY_MARKET

    def __init__(self, store):
        self.store = store
        self.market = validate_market(store.get("market", DEFAULT_SPOTIFY_MARKET))
        client_id = store.get("client_id")
        refresh = store.get("refresh_token")
        if not client_id or not refresh:
            raise SpotifyError("Connect Spotify in the settings first")

        # Refresh tokens may rotate; save a replacement without clearing the cache.
        token = token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id},
            self.store,
        )
        if token.get("refresh_token"):
            store.set_many({"refresh_token": token["refresh_token"]})
        self.token = token["access_token"]

    def request(self, method, path, **kwargs):
        params = kwargs.get("params")
        category = _request_category(method, path, params)
        detail = _request_detail(method, path, params, category)
        with _tracked_request(self.store, category, detail) as outcome:
            response = requests.request(
                method, API + path, headers={"Authorization": "Bearer " + self.token},
                timeout=API_REQUEST_TIMEOUT_SECONDS, **kwargs,
            )
            if not response.ok:
                outcome["quota"] = _is_quota_response(response)
                if path == SPOTIFY_LIKED_TRACKS_PATH and response.status_code == HTTPStatus.FORBIDDEN:
                    raise _request_error(
                        response,
                        f"Liked tracks are inaccessible (HTTP {HTTPStatus.FORBIDDEN}); "
                        "reconnect Spotify and grant library access",
                        (self.token,),
                    )
                if path == SPOTIFY_FOLLOWED_ARTISTS_PATH and response.status_code in (
                    HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN
                ):
                    raise _request_error(
                        response,
                        f"Followed artists are inaccessible (HTTP {response.status_code}); "
                        "reconnect Spotify and grant access to followed artists",
                        (self.token,),
                    )
                raise _request_error(
                    response,
                    f"Spotify {method} {path} failed (HTTP {response.status_code})",
                    (self.token,),
                )
            return response.json() if response.content else {}

    def get(self, path, **params):
        return self.request("GET", path, params=params)

    def cached_today(self, key):
        day = current_time(self.store, datetime).date().isoformat()
        return self.store.get_cached(key, day)

    def cache_today(self, key, value):
        day = current_time(self.store, datetime).date().isoformat()
        self.store.set_cached(key, day, value)

    def cached_previous(self, key):
        day = current_time(self.store, datetime).date().isoformat()
        return self.store.get_previous_cached(key, day)

    def daily_cached(self, key, fetch, trace=None):
        day = current_time(self.store, datetime).date().isoformat()
        cached = self.store.get_cached(key, day)
        label = key.removesuffix(CACHE_KEY_SUFFIX).replace("_", " ")
        label = label[:1].upper() + label[1:]

        # An empty list is a valid hit; only cache after a complete fetch.
        if cached is not None:
            if key != TOP_ARTISTS_TIME_RANGE_CACHE_KEY:
                record_cache_hit()
            if trace is not None:
                trace.append(f"{label}: retrieved from today's cache.")
            return cached

        try:
            result = fetch()
        except (SpotifyError, requests.RequestException) as exc:
            partial = exc.items if isinstance(exc, SpotifyPartialError) else []
            previous = self.store.get_previous_cached(key, day)
            if previous is not None:
                cached_day, cached = previous
                if not isinstance(cached, list):
                    raise SpotifyError(f"Invalid cached data for {label}") from exc
                fresh_uris = {source_item_uri(item) for item in partial}
                extra = [
                    item for item in cached
                    if item not in partial and (
                        source_item_uri(item) is None or source_item_uri(item) not in fresh_uris
                    )
                ]
                if extra or not partial:
                    if extra:
                        record_cache_hit()
                    if partial:
                        self.store.set_fallback(key, day, partial + extra)
                    if trace is not None:
                        empty_detail = f"; {ZERO_FETCH_RESULTS}" if not (partial or extra) else ""
                        trace.append(
                            f"{label}: Spotify fetch failed; reused cache from {cached_day}"
                            f"{empty_detail}."
                        )
                    raise SpotifyCachedError(exc, partial + extra, extra) from exc

            if isinstance(exc, SpotifyPartialError) and exc.items:
                self.store.set_fallback(key, day, partial)
                if trace is not None:
                    trace.append(
                        f"{label}: partially retrieved from Spotify during this refresh; saved as fallback."
                    )
                raise

            if trace is not None:
                trace.append(
                    f"{label}: Spotify fetch failed; no data retrieved from cache; "
                    f"{ZERO_FETCH_RESULTS}."
                )
            raise

        self.store.set_cached(key, day, result)
        if trace is not None:
            trace.append(f"{label}: retrieved from Spotify and saved in today's cache.")
        return result

    def liked_tracks(self, rng):
        # Fetch the first page with the total so it can also be used if randomly chosen.
        first = self.get(
            SPOTIFY_LIKED_TRACKS_PATH,
            market=self.market,
            limit=SPOTIFY_PAGE_SIZE,
            offset=0,
        )
        if not isinstance(first, dict):
            raise SpotifyError("Spotify returned an invalid liked-track page")
        total = first.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise SpotifyError("Spotify returned an invalid liked-track count")
        if (
            not isinstance(first.get("items"), list)
            or len(first["items"]) != min(total, SPOTIFY_PAGE_SIZE)
        ):
            raise SpotifyError("Spotify returned an inconsistent liked-track count")
        if total <= 1:
            return first["items"]

        page_count = (total + SPOTIFY_PAGE_SIZE - 1) // SPOTIFY_PAGE_SIZE
        pages = rng.sample(range(page_count), min(MAX_LIKED_TRACK_PAGES_PER_REFRESH, page_count))
        result = []
        try:
            for page in pages:
                response = first if page == 0 else self.get(
                    SPOTIFY_LIKED_TRACKS_PATH,
                    market=self.market,
                    limit=SPOTIFY_PAGE_SIZE,
                    offset=page * SPOTIFY_PAGE_SIZE,
                )
                if (
                    not isinstance(response, dict)
                    or not isinstance(response.get("items"), list)
                    or not response["items"]
                ):
                    raise SpotifyError(
                        "Spotify returned an invalid randomly chosen liked-track page"
                    )
                result.extend(response["items"])
        except (SpotifyError, requests.RequestException) as exc:
            if result:
                raise SpotifyPartialError(exc, result) from exc
            raise
        return result

    def pages(self, path, params=None, max_items=MAX_PAGINATED_ITEMS):
        # Follow offset-based pages in order; an incomplete read is not a full result.
        params = dict(params or {})
        params["limit"] = SPOTIFY_PAGE_SIZE
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
        day = current_time(self.store, datetime).date().isoformat()
        key = FOLLOWED_ARTISTS_CACHE_KEY

        # Keep the collected artists and scan position between refreshes and restarts.
        state = self.store.get(
            key,
            {
                "artists": {},
                "cursor": None,
                "seen_ids": [],
                "seen_cursors": [],
                "day": None,
                "requests": 0,
                "completed_day": None,
            },
        )
        cached_ids = set(state["artists"])
        fetched_ids = set()
        attempted = False

        def report_cache(daily_limit_reached=False, request_failed=False):
            result_ids = set(state["artists"])
            reused = len(result_ids & cached_ids - fetched_ids)
            if reused:
                record_cache_hit()
            if trace is not None:
                refreshed = len(result_ids & fetched_ids)
                details = []
                if daily_limit_reached:
                    details.append(
                        f"daily request limit of {MAX_FOLLOWED_ARTISTS_REQUESTS_PER_DAY} "
                        "reached; no new Spotify request"
                    )
                if not result_ids:
                    origin = "Spotify was queried" if attempted else "no Spotify request was made"
                    cause = " because a Spotify request failed" if request_failed else ""
                    details.append(f"no artists available{cause}; {origin} during this refresh")
                suffix = f"; {'; '.join(details)}" if details else ""
                trace.append(
                    f"{SOURCE_FOLLOWED_ARTISTS.capitalize()}: {reused} reused from cache; "
                    f"{refreshed} retrieved from Spotify during this refresh{suffix}."
                )

        if state["day"] != day:
            state["day"] = day
            state["requests"] = 0

        if state["requests"] >= MAX_FOLLOWED_ARTISTS_REQUESTS_PER_DAY and state["completed_day"] != day:
            report_cache(daily_limit_reached=True)
            return list(state["artists"].values())

        # Spotify gives us a cursor for the next page, not a random page number.
        # Continue from that cursor within the configured daily request limit.
        try:
            while (
                state["requests"] < MAX_FOLLOWED_ARTISTS_REQUESTS_PER_DAY
                and state["completed_day"] != day
            ):
                params = {"type": "artist", "limit": SPOTIFY_PAGE_SIZE}
                if state["cursor"] is not None:
                    params["after"] = state["cursor"]

                # Count the attempt before sending it, including failed requests.
                state["requests"] += 1
                self.store.set_many({key: state})
                attempted = True
                response = self.get(SPOTIFY_FOLLOWED_ARTISTS_PATH, **params)
                if (
                    not isinstance(response, dict)
                    or not isinstance(response.get("artists"), dict)
                ):
                    raise SpotifyError("Spotify returned an invalid followed-artist page")
                page = response["artists"]
                if (
                    not isinstance(page.get("items"), list)
                    or "next" not in page
                    or not all(
                        isinstance(artist, dict)
                        and isinstance(artist.get("id"), str)
                        and artist["id"]
                        for artist in page["items"]
                    )
                ):
                    raise SpotifyError("Spotify returned an invalid followed-artist page")
                if page["next"]:
                    if not page["items"]:
                        raise SpotifyError("Empty followed-artist page with a next page")
                    cursors = page.get("cursors")
                    cursor = cursors.get("after") if isinstance(cursors, dict) else None
                    # A missing or repeated cursor cannot safely advance the scan.
                    if (
                        not isinstance(cursor, str)
                        or not cursor
                        or cursor in state["seen_cursors"]
                    ):
                        raise SpotifyError(
                            "Spotify returned an invalid followed-artist pagination cursor"
                        )
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
                        artist_id: artist
                        for artist_id, artist in state["artists"].items()
                        if artist_id in seen
                    }
                    state["seen_ids"] = []
                    state["seen_cursors"] = []
                    state["completed_day"] = day
                self.store.set_many({key: state})
        except (SpotifyError, requests.RequestException) as exc:
            # Report the error but let selection use any artists already in the cache.
            report_cache(request_failed=True)
            if state["artists"]:
                raise SpotifyPartialError(exc, list(state["artists"].values())) from exc
            raise
        report_cache()
        return list(state["artists"].values())

    def playlist_items(self, pid):
        entries = self.pages(
            SPOTIFY_PLAYLIST_ITEMS_PATH.format(playlist_id=pid),
            {"additional_types": "track,episode", "market": self.market},
        )
        result = []

        # Unreadable items prevent a reliable before/after comparison after a timeout.
        for entry in entries:
            item = entry.get("item")
            if not item or entry.get("is_local") or not item.get("uri"):
                raise SpotifyError(
                    "Playlist contains local or unreadable items; "
                    "safe replacement is not possible"
                )
            result.append(item["uri"])
        return result

    def replace(self, pid, uris):
        path = SPOTIFY_PLAYLIST_ITEMS_PATH.format(playlist_id=pid)
        return self.request("PUT", path, json={"uris": uris})
