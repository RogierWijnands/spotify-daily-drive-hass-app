import argparse
import json
import logging
import sqlite3
import sys
from getpass import getpass
from pathlib import Path

from requests import RequestException

from .refresh import Refresh
from .selection import build_items
from .shows import validate_shows
from .spotify import REDIRECT_URI, Spotify, SpotifyError, authorization, callback_code, playlist_id, token_request
from .store import Store


def read_config(path):
    with path.open(encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    client_id = config.get("client_id")
    if not isinstance(client_id, str) or len(client_id) != 32 or not client_id.isascii() or not client_id.isalnum():
        raise ValueError("client_id must be a Spotify client ID of 32 letters or digits")
    target = config.get("playlist")
    sources = config.get("source_playlists", [])
    if not isinstance(target, str) or not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ValueError("playlist must be a URL or ID and source_playlists a list of URLs or IDs")
    target_id = playlist_id(target)
    source_ids = list(dict.fromkeys(playlist_id(source) for source in sources))
    if target_id in source_ids:
        raise ValueError("Daily Drive cannot also be a music source")
    news, podcasts = validate_shows(config.get("news_shows", []), config.get("podcast_shows", []))
    return client_id, target_id, source_ids, news, podcasts


def connect_spotify(store, client_id):
    url, verifier, state = authorization(client_id)
    print("Open this link and grant access:\n" + url)
    print("Then copy the full URL from the address bar, even if an error page appears.")
    callback = getpass("Callback URL (input hidden): ")
    code = callback_code(callback, state)
    token = token_request({"client_id": client_id, "grant_type": "authorization_code",
                           "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
    if not token.get("refresh_token"):
        raise SpotifyError("Spotify did not return a refresh token")
    store.set_many({"refresh_token": token["refresh_token"]})
    print("Spotify is connected.")


def preview(store):
    api = Spotify(store)
    api.playlist_items(store.get("playlist_id"))
    items, _, detail = build_items(api, store.get("source_ids", []), store.get("news_shows", []),
                                   store.get("podcast_shows", []))
    if len(items) > 100:
        raise SpotifyError("Spotify allows at most 100 items per replacement")
    print("Dry run: the Spotify playlist was not changed.")
    print(detail)
    for index, item in enumerate(items, 1):
        artists = ", ".join(artist["name"] for artist in item.get("artists", []) if artist.get("name"))
        title = item.get("name") or item["uri"]
        label = f"{artists} - {title}" if artists else title
        duration = item["duration_ms"] // 1000
        print(f"{index:2}. [{duration // 60}:{duration % 60:02}] {label} ({item['uri']})")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Test or refresh Daily Drive without Home Assistant.")
    parser.add_argument("--config", type=Path, default=Path("local-config.json"),
                        help="JSON configuration (default: local-config.json)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--authorize", action="store_true", help="Connect Spotify and exit")
    mode.add_argument("--apply", action="store_true", help="Replace the actual Spotify playlist")
    mode.add_argument("--pending", action="store_true", help="Show an uncertain playlist change")
    mode.add_argument("--release-pending", action="store_true", help="Manually release an uncertain change")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        config_path = args.config.expanduser().resolve()
        client_id, target, sources, news, podcasts = read_config(config_path)
        store = Store(str(config_path.parent / ".daily-drive.sqlite3"))
        if client_id != store.get("client_id") and store.pending():
            raise SpotifyError("An uncertain playlist change is still pending; use the original client ID first")
        values = {"client_id": client_id, "playlist_id": target, "source_ids": sources,
                  "news_shows": news, "podcast_shows": podcasts}
        if client_id != store.get("client_id"):
            values["refresh_token"] = None
        store.set_many(values)
        if args.authorize:
            connect_spotify(store, client_id)
        elif args.pending:
            pending = store.pending()
            if not pending:
                print("No uncertain playlist change.")
            else:
                print(f"Playlist: {pending['playlist_id']}\nContents before refresh:")
                print("\n".join(pending["before_items"]) or "(empty)")
                print("\nProposed contents:")
                print("\n".join(pending["after_items"]) or "(empty)")
        elif args.release_pending:
            if not store.pending():
                raise ValueError("There is no uncertain change")
            print("Check the playlist in Spotify. The previous outcome remains unknown.")
            if input("Type 'CONFIRM' to release the block: ") != "CONFIRM":
                raise ValueError("Block not released")
            store.release_pending()
            print("Block released; the previous outcome remains unknown.")
        elif args.apply:
            Refresh(store).run()
        else:
            preview(store)
        return 0
    except (ValueError, OSError, sqlite3.Error, SpotifyError, RequestException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
