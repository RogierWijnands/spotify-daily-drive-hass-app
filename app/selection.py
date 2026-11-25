import random
from datetime import date, timedelta
from itertools import zip_longest

from .spotify import SpotifyError

TARGET = 90 * 60 * 1000
TOLERANCE = 2 * 60 * 1000


def valid(item, kind):
    return (
        isinstance(item, dict)
        and item.get("type") == kind
        and item.get("uri")
        and isinstance(item.get("duration_ms"), int) and item["duration_ms"] > 0
        and item.get("is_playable") is True
        and not item.get("is_local", False)
        and not item.get("restrictions")
    )


def podcast_candidates(api, news_shows, podcast_shows):
    news = []
    other_podcasts = []
    seen = set()
    for shows, episodes in ((news_shows, news), (podcast_shows, other_podcasts)):
        for entry in shows:
            show, count = entry["show"], entry["count"]
            current = []
            offset = 0

            while len(current) < count:
                page = api.get(f"/shows/{show}/episodes", market="NL", limit=50, offset=offset)
                for item in page["items"]:
                    if valid(item, "episode") and item["uri"] not in seen:
                        current.append(item)
                        seen.add(item["uri"])
                        if len(current) == count:
                            break
                if not page.get("next") or not page["items"]:
                    break
                offset += len(page["items"])

            if len(current) != count:
                raise SpotifyError(f"Not enough playable episodes are available for show {show}")
            episodes.extend(current)

    return [
        episode
        for pair in zip_longest(news, other_podcasts)
        for episode in pair
        if episode is not None
    ]


def music_candidates(api, playlist_ids, rng):
    short = api.get("/me/top/tracks", time_range="short_term", limit=50)["items"]
    medium = api.get("/me/top/tracks", time_range="medium_term", limit=50)["items"]
    long = api.get("/me/top/tracks", time_range="long_term", limit=50)["items"]
    recent = [item["track"] for item in api.get("/me/player/recently-played", limit=50)["items"]]
    saved = [entry.get("track") for entry in api.pages("/me/tracks", {"market": "NL"}, max_items=None)]

    current_ids = {t.get("id") for t in short + recent}
    old = [t for t in long if t.get("id") not in current_ids]
    favorites = short + medium + recent

    top_artists = api.pages("/me/top/artists", {"time_range": "long_term"}, max_items=None)
    artists = list({artist["id"]: artist for artist in top_artists + api.followed_artists()}.values())
    rng.shuffle(artists)
    discoveries = []
    cutoff = (date.today() - timedelta(days=180)).isoformat()

    def artist_tracks(artist_id, include_groups):
        albums = api.get(
            f"/artists/{artist_id}/albums",
            market="NL",
            include_groups=include_groups,
            limit=10,
        )["items"]
        rng.shuffle(albums)
        albums.sort(key=lambda album: album.get("release_date", "") >= cutoff, reverse=True)

        tracks = []
        for album in albums[:2]:
            tracks.extend(api.get(f"/albums/{album['id']}/tracks", market="NL", limit=50)["items"])
        return tracks

    favorite_ids = {artist["id"] for artist in artists}
    collaborators = set()

    for artist in artists[:8]:
        tracks = artist_tracks(artist["id"], "album,single,appears_on")
        discoveries.extend(tracks)

        # Album track lists can include unrelated artists; require a shared credit.
        for item in tracks:
            credited = {credit["id"] for credit in item.get("artists", []) if credit.get("id")}
            if artist["id"] in credited:
                collaborators.update(credited - favorite_ids)

    collaborator_ids = sorted(collaborators)
    rng.shuffle(collaborator_ids)
    collaborator_tracks = []

    for artist_id in collaborator_ids[:4]:
        tracks = artist_tracks(artist_id, "album,single")
        collaborator_tracks.extend(
            item for item in tracks
            if any(credit.get("id") == artist_id for credit in item.get("artists", []))
        )

    configured = []
    for pid in playlist_ids:
        entries = api.pages(f"/playlists/{pid}/items", {"additional_types": "track", "market": "NL"})
        configured.extend(entry.get("item") for entry in entries if not entry.get("is_local"))

    # Top and recently played endpoints do not support market. Resolve candidates with
    # uncertain playability against the account's market before selection.
    cache = {}

    def playable(items):
        result = []
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "track" or not item.get("id"):
                continue
            if item.get("is_playable") is None:
                tid = item["id"]
                if tid not in cache:
                    cache[tid] = api.get(f"/tracks/{tid}", market="NL")
                item = cache[tid]
            if valid(item, "track"):
                result.append(item)
        return result

    groups = (favorites, old, discoveries, configured, saved, collaborator_tracks)
    for group in groups:
        rng.shuffle(group)
    return [playable(group[:80]) for group in groups]


def select_music(groups, podcasts, rng):
    chosen = []
    seen = {p["uri"] for p in podcasts}
    normalized = []

    # Deduplicate across groups before choosing so a track cannot occupy two slots.
    for group in groups:
        pool = []
        for item in group:
            if item["uri"] not in seen:
                seen.add(item["uri"])
                pool.append(item)
        rng.shuffle(pool)
        normalized.append(pool)

    # Reserve one current and one older favorite if each exists.
    for pool in normalized[:2]:
        if pool and len(chosen) < 100 - len(podcasts):
            chosen.append(pool.pop())

    remaining = [item for pool in normalized for item in pool]
    rng.shuffle(remaining)
    podcast_ms = sum(p["duration_ms"] for p in podcasts)

    if not chosen and remaining:
        chosen.append(remaining.pop())
    if not chosen:
        raise SpotifyError("No playable music found")

    base = podcast_ms + sum(item["duration_ms"] for item in chosen)
    if podcast_ms >= TARGET - TOLERANCE:
        return chosen
    if base > TARGET + TOLERANCE:
        raise SpotifyError("Selected favorites do not fit within the target duration")

    # Subset sum in seconds: keep exact durations for the final tolerance check.
    capacity = (TARGET + TOLERANCE - base) // 1000
    states = {0: ()}

    for index, item in enumerate(remaining):
        seconds = max(1, item["duration_ms"] // 1000)
        for total, indices in list(states.items()):
            new = total + seconds
            if new <= capacity and new not in states and len(indices) + len(chosen) + len(podcasts) < 100:
                states[new] = indices + (index,)

    best = min(
        states.values(),
        key=lambda ids: abs(TARGET - base - sum(remaining[i]["duration_ms"] for i in ids)),
    )
    chosen.extend(remaining[i] for i in best)
    duration = podcast_ms + sum(item["duration_ms"] for item in chosen)

    if abs(duration - TARGET) > TOLERANCE:
        raise SpotifyError("Not enough suitable music to stay within 88-92 minutes")
    return chosen


def interleave(music, podcasts, rng):
    rng.shuffle(music)

    # Spread the episodes evenly through the music, preserving the news order.
    slots = [[] for _ in range(len(music) + 1)]
    for index, episode in enumerate(podcasts):
        slot = round((index + 1) * len(music) / (len(podcasts) + 1))
        slots[slot].append(episode)

    output = []
    for index, track in enumerate(music):
        output.extend(slots[index])
        output.append(track)
    output.extend(slots[-1])
    return output


def build_items(api, playlist_ids, news_shows, podcast_shows, rng=None):
    rng = rng or random.SystemRandom()
    podcasts = podcast_candidates(api, news_shows, podcast_shows)
    groups = music_candidates(api, playlist_ids, rng)
    music = select_music(groups, podcasts, rng)
    result = interleave(music, podcasts, rng)
    total = sum(item["duration_ms"] for item in result)
    podcast_total = sum(item["duration_ms"] for item in podcasts)
    detail = f"{total / 60000:.1f} minutes; podcasts {podcast_total / total * 100:.1f}%"
    return result, [item["uri"] for item in podcasts], detail


def build(api, playlist_ids, news_shows, podcast_shows, rng=None):
    items, episodes, detail = build_items(api, playlist_ids, news_shows, podcast_shows, rng)
    return [item["uri"] for item in items], episodes, detail
