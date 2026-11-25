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
from .spotify import REDIRECT_URI, Spotify, SpotifyError, authorization, callback_code, playlist_id, token_request
from .store import Store


def read_config(path):
    with path.open(encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError("Configuratie moet een JSON-object zijn")
    client_id = config.get("client_id")
    if not isinstance(client_id, str) or len(client_id) != 32 or not client_id.isascii() or not client_id.isalnum():
        raise ValueError("client_id moet een Spotify client-ID van 32 letters/cijfers zijn")
    target = config.get("playlist")
    sources = config.get("source_playlists", [])
    if not isinstance(target, str) or not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ValueError("playlist moet een URL of ID zijn en source_playlists een lijst met URL's of ID's")
    target_id = playlist_id(target)
    source_ids = list(dict.fromkeys(playlist_id(source) for source in sources))
    if target_id in source_ids:
        raise ValueError("Daily Drive mag niet ook een muziekbron zijn")
    return client_id, target_id, source_ids


def connect_spotify(store, client_id):
    url, verifier, state = authorization(client_id)
    print("Open deze link en geef toestemming:\n" + url)
    print("Kopieer daarna de volledige URL uit de adresbalk, ook als een foutpagina verschijnt.")
    callback = getpass("Terugkeer-URL (invoer niet zichtbaar): ")
    code = callback_code(callback, state)
    token = token_request({"client_id": client_id, "grant_type": "authorization_code",
                           "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
    if not token.get("refresh_token"):
        raise SpotifyError("Spotify gaf geen refresh-token terug")
    store.set_many({"refresh_token": token["refresh_token"]})
    print("Spotify is gekoppeld.")


def preview(store):
    api = Spotify(store)
    api.playlist_items(store.get("playlist_id"))
    items, _, detail = build_items(api, store.get("source_ids", []))
    if len(items) > 100:
        raise SpotifyError("Spotify staat maximaal 100 items per vervanging toe")
    print("Proefrun: de Spotify-playlist is niet gewijzigd.")
    print(detail)
    for index, item in enumerate(items, 1):
        artists = ", ".join(artist["name"] for artist in item.get("artists", []) if artist.get("name"))
        title = item.get("name") or item["uri"]
        label = f"{artists} - {title}" if artists else title
        duration = item["duration_ms"] // 1000
        print(f"{index:2}. [{duration // 60}:{duration % 60:02}] {label} ({item['uri']})")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Test of ververs Daily Drive zonder Home Assistant.")
    parser.add_argument("--config", type=Path, default=Path("local-config.json"),
                        help="JSON-configuratie (standaard: local-config.json)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--authorize", action="store_true", help="Koppel Spotify en stop")
    mode.add_argument("--apply", action="store_true", help="Vervang de echte Spotify-playlist")
    mode.add_argument("--pending", action="store_true", help="Toon een onzekere playlistwijziging")
    mode.add_argument("--release-pending", action="store_true", help="Hef een onzekere wijziging handmatig op")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        config_path = args.config.expanduser().resolve()
        client_id, target, sources = read_config(config_path)
        store = Store(str(config_path.parent / ".daily-drive.sqlite3"))
        if client_id != store.get("client_id") and store.pending():
            raise SpotifyError("Een onzekere playlistwijziging is nog niet afgehandeld; gebruik eerst de oorspronkelijke client-ID")
        values = {"client_id": client_id, "playlist_id": target, "source_ids": sources}
        if client_id != store.get("client_id"):
            values["refresh_token"] = None
        store.set_many(values)
        if args.authorize:
            connect_spotify(store, client_id)
        elif args.pending:
            pending = store.pending()
            if not pending:
                print("Geen onzekere playlistwijziging.")
            else:
                print(f"Playlist: {pending['playlist_id']}\nInhoud vóór verversen:")
                print("\n".join(pending["before_items"]) or "(leeg)")
                print("\nVoorgestelde inhoud:")
                print("\n".join(pending["after_items"]) or "(leeg)")
        elif args.release_pending:
            if not store.pending():
                raise ValueError("Er is geen onzekere wijziging")
            print("Controleer de playlist in Spotify. De eerdere uitkomst blijft onbekend.")
            if input("Typ 'BEVESTIG' om de blokkade op te heffen: ") != "BEVESTIG":
                raise ValueError("Blokkade niet opgeheven")
            store.release_pending()
            print("Blokkade opgeheven; de eerdere uitkomst blijft onbekend.")
        elif args.apply:
            Refresh(store).run()
        else:
            preview(store)
        return 0
    except (ValueError, OSError, sqlite3.Error, SpotifyError, RequestException) as exc:
        print(f"Fout: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
