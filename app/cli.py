import argparse
import json
import logging
import sqlite3
import sys
from getpass import getpass
from pathlib import Path

from requests import RequestException

from .constants import (
    DAILY_DRIVE_SOURCE_PLAYLIST_ERROR, DEFAULT_SPOTIFY_MARKET, DEFAULT_TIMEZONE, MAX_PLAYLIST_ITEMS_ERROR,
    MAX_SPOTIFY_PLAYLIST_ITEMS, MILLISECONDS_PER_SECOND, MISSING_SPOTIFY_REFRESH_TOKEN_ERROR,
    NO_UNCERTAIN_CHANGE_ERROR, PREVIOUS_CHANGE_OUTCOME_UNKNOWN_MESSAGE, SECONDS_PER_MINUTE,
    SPOTIFY_CLIENT_ID_LENGTH,
)
from .refresh import Refresh
from .selection import build_items
from .shows import validate_shows
from .spotify import (
    REDIRECT_URI, Spotify, SpotifyError, authorization, callback_code, playlist_id, token_request,
    validate_market,
)
from .store import Store
from .timezones import validate_timezone


def read_config(path):
    with path.open(encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")

    client_id = config.get("client_id")
    if (
        not isinstance(client_id, str)
        or len(client_id) != SPOTIFY_CLIENT_ID_LENGTH
        or not client_id.isascii()
        or not client_id.isalnum()
    ):
        raise ValueError(
            f"client_id must be a Spotify client ID of {SPOTIFY_CLIENT_ID_LENGTH} letters or digits"
        )
    target = config.get("playlist")
    sources = config.get("source_playlists", [])
    if (
        not isinstance(target, str)
        or not isinstance(sources, list)
        or not all(isinstance(source, str) for source in sources)
    ):
        raise ValueError("playlist must be a URL or ID and source_playlists a list of URLs or IDs")

    # Normalize URLs to IDs and remove duplicate source playlists.
    target_id = playlist_id(target)
    source_ids = list(dict.fromkeys(playlist_id(source) for source in sources))
    if target_id in source_ids:
        raise ValueError(DAILY_DRIVE_SOURCE_PLAYLIST_ERROR)
    news, podcasts = validate_shows(config.get("news_shows", []), config.get("podcast_shows", []))
    market = validate_market(config.get("market", DEFAULT_SPOTIFY_MARKET))
    timezone = validate_timezone(config.get("timezone", DEFAULT_TIMEZONE))
    return client_id, target_id, source_ids, news, podcasts, market, timezone


def connect_spotify(store, client_id):
    # The callback is entered privately because it contains an authorization code.
    url, verifier, state = authorization(client_id)
    print("Open this link and grant access:\n" + url)
    print("Then copy the full URL from the address bar, even if an error page appears.")
    callback = getpass("Callback URL (input hidden): ")
    code = callback_code(callback, state)
    token = token_request({
        "client_id": client_id, "grant_type": "authorization_code",
        "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
    }, store)
    if not token.get("refresh_token"):
        raise SpotifyError(MISSING_SPOTIFY_REFRESH_TOKEN_ERROR)
    store.set_many({"refresh_token": token["refresh_token"]}, clear_cache=True)
    print("Spotify is connected.")


def preview(store):
    api = Spotify(store)
    # Match the real refresh's safety check without replacing the playlist.
    api.playlist_items(store.get("playlist_id"))
    items, _, detail = build_items(
        api, store.get("source_ids", []), store.get("news_shows", []),
        store.get("podcast_shows", []),
    )
    if len(items) > MAX_SPOTIFY_PLAYLIST_ITEMS:
        raise SpotifyError(
            MAX_PLAYLIST_ITEMS_ERROR.format(max_items=MAX_SPOTIFY_PLAYLIST_ITEMS)
        )

    print("Dry run: the Spotify playlist was not changed.")
    print(detail)

    for index, item in enumerate(items, 1):
        artists = ", ".join(
            artist["name"] for artist in item.get("artists", []) if artist.get("name")
        )
        title = item.get("name") or item["uri"]
        label = f"{artists} - {title}" if artists else title
        duration = item["duration_ms"] // MILLISECONDS_PER_SECOND
        print(
            f"{index:2}. "
            f"[{duration // SECONDS_PER_MINUTE}:{duration % SECONDS_PER_MINUTE:02}] "
            f"{label} ({item['uri']})"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Test or refresh Daily Drive without Home Assistant."
    )
    parser.add_argument(
        "--config", type=Path, default=Path("local-config.json"),
        help="JSON configuration (default: local-config.json)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--authorize", action="store_true", help="Connect Spotify and exit")
    mode.add_argument("--apply", action="store_true", help="Replace the actual Spotify playlist")
    mode.add_argument("--pending", action="store_true", help="Show an uncertain playlist change")
    mode.add_argument(
        "--release-pending", action="store_true", help="Manually release an uncertain change"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        config_path = args.config.expanduser().resolve()
        client_id, target, sources, news, podcasts, market, timezone = read_config(config_path)
        store = Store(str(config_path.parent / ".daily-drive.sqlite3"))

        # A pending write must be checked using its original account and market.
        client_changed = client_id != store.get("client_id")
        market_changed = market != store.get("market", DEFAULT_SPOTIFY_MARKET)
        timezone_changed = timezone != store.get("timezone", DEFAULT_TIMEZONE)
        if store.pending():
            if client_changed:
                raise SpotifyError(
                    "An uncertain playlist change is still pending; use the original client ID first"
                )
            if market_changed:
                raise SpotifyError(
                    "An uncertain playlist change is still pending; use the original market first"
                )

        # A new client ID requires a fresh Spotify connection and library cache.
        values = {
            "client_id": client_id, "playlist_id": target, "source_ids": sources,
            "news_shows": news, "podcast_shows": podcasts, "market": market, "timezone": timezone,
        }
        if client_changed:
            values["refresh_token"] = None
        store.set_many(
            values, clear_cache=client_changed, clear_daily_cache=market_changed or timezone_changed,
            clear_request_stats=timezone_changed,
        )

        # Preview is the default; only --apply writes to Spotify.
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
                raise ValueError(NO_UNCERTAIN_CHANGE_ERROR)
            print("Check the playlist in Spotify. The previous outcome remains unknown.")
            if input("Type 'CONFIRM' to release the block: ") != "CONFIRM":
                raise ValueError("Block not released")
            store.release_pending()
            print(PREVIOUS_CHANGE_OUTCOME_UNKNOWN_MESSAGE)
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
