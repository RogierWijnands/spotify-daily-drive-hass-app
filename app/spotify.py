import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

import requests

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-top-read user-read-recently-played user-library-read playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyError(RuntimeError):
    pass


def playlist_id(value):
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc == "open.spotify.com":
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "playlist":
            value = parts[1]
    elif value.startswith("spotify:playlist:"):
        value = value.removeprefix("spotify:playlist:")
    if len(value) != 22 or not value.isascii() or not value.isalnum():
        raise ValueError("Ongeldige Spotify-playlist-URL of ID")
    return value


def authorization(client_id):
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = "https://accounts.spotify.com/authorize?" + urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPES, "state": state, "code_challenge_method": "S256",
        "code_challenge": challenge,
    })
    return url, verifier, state


def callback_code(url, state):
    parsed = urlparse(url.strip())
    expected = urlparse(REDIRECT_URI)
    if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path):
        raise ValueError("De terugkeer-URL heeft niet het verwachte adres")
    params = parse_qs(parsed.query)
    if len(params.get("state", [])) != 1 or not secrets.compare_digest(params["state"][0], state):
        raise ValueError("Spotify state komt niet overeen; start opnieuw")
    if params.get("error"):
        raise ValueError("Spotify-autorisatie geweigerd: " + params["error"][0])
    if len(params.get("code", [])) != 1:
        raise ValueError("Autorisatiecode ontbreekt")
    return params["code"][0]


def token_request(data):
    response = requests.post(TOKEN_URL, data=data, timeout=20)
    if not response.ok:
        raise SpotifyError(f"Spotify-tokenaanvraag mislukt (HTTP {response.status_code})")
    return response.json()


class Spotify:
    def __init__(self, store):
        self.store = store
        client_id = store.get("client_id")
        refresh = store.get("refresh_token")
        if not client_id or not refresh:
            raise SpotifyError("Koppel eerst Spotify in de instellingen")
        token = token_request({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
        if token.get("refresh_token"):
            store.set_many({"refresh_token": token["refresh_token"]})
        self.token = token["access_token"]

    def request(self, method, path, **kwargs):
        response = requests.request(method, API + path, headers={"Authorization": "Bearer " + self.token},
                                    timeout=30, **kwargs)
        if not response.ok:
            if path == "/me/tracks" and response.status_code == 403:
                raise SpotifyError("Gelikete nummers niet toegankelijk (HTTP 403); koppel Spotify opnieuw en geef toestemming voor je bibliotheek")
            raise SpotifyError(f"Spotify {method} {path} mislukt (HTTP {response.status_code})")
        return response.json() if response.content else {}

    def get(self, path, **params):
        return self.request("GET", path, params=params)

    def pages(self, path, params=None, max_items=1000):
        params = dict(params or {})
        params["limit"] = 50
        offset = 0
        result = []
        while True:
            page = self.get(path, **params, offset=offset)
            items = page["items"]
            result.extend(items)
            if not page.get("next") or not items:
                return result
            offset += len(items)
            if max_items is not None and offset >= max_items:
                raise SpotifyError(f"Te veel items in {path}; wijziging afgebroken")

    def playlist_items(self, pid):
        entries = self.pages(f"/playlists/{pid}/items", {"additional_types": "track,episode", "market": "NL"})
        result = []
        for entry in entries:
            item = entry.get("item")
            if not item or entry.get("is_local") or not item.get("uri"):
                raise SpotifyError("Playlist bevat lokale of onleesbare items; veilig vervangen is niet mogelijk")
            result.append(item["uri"])
        return result

    def replace(self, pid, uris):
        return self.request("PUT", f"/playlists/{pid}/items", json={"uris": uris})
