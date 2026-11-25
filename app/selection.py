import logging
import random
from itertools import zip_longest

from requests import RequestException

from .spotify import SpotifyError, SpotifyPartialError

log = logging.getLogger(__name__)

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


def api_warning(issues, message, *args):
    # Log the problem and save it for the refresh error if selection later fails.
    log.warning(message, *args)
    if issues is not None:
        issues.append(message % args)


def get_api_items(source, fetch, issues=None):
    # Fetch items from one source, whether the result is a list or an API page.
    try:
        result = fetch()
        if isinstance(result, dict):
            result = result.get("items")
        if not isinstance(result, list):
            raise SpotifyError("Spotify returned an invalid item list")
        return result
    except SpotifyPartialError as exc:
        # A later page failed, but the items from earlier pages can still be used.
        api_warning(issues, "Using partial %s after an error: %s", source, exc)
        return exc.items
    except (SpotifyError, RequestException) as exc:
        # Skip this source so the remaining sources can still build the playlist.
        api_warning(issues, "Skipping %s: %s", source, exc)
        return []


def podcast_candidates(api, news_shows, podcast_shows, issues=None):
    news = []
    other_podcasts = []
    seen = set()

    # Read each show until its requested count is reached or there are no more pages.
    # Keep the episodes already found if a later page fails.
    for shows, episodes in ((news_shows, news), (podcast_shows, other_podcasts)):
        for entry in shows:
            show, count = entry["show"], entry["count"]
            current = []
            offset = 0

            while len(current) < count:
                try:
                    page = api.get(f"/shows/{show}/episodes", market="NL", limit=50, offset=offset)
                    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                        raise SpotifyError("Spotify returned an invalid episode page")
                except (SpotifyError, RequestException) as exc:
                    api_warning(issues, "Skipping remaining episodes for show %s: %s", show, exc)
                    break
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
                api_warning(issues, "Show %s has %s of %s requested playable episodes", show, len(current), count)
            episodes.extend(current)

    # Alternate news and other podcasts; keep the remaining episodes from either list.
    return [
        episode
        for pair in zip_longest(news, other_podcasts)
        for episode in pair
        if episode is not None
    ]


def music_candidates(api, playlist_ids, rng, issues=None):
    # Each source can fail independently without blocking the other sources.
    short_term_top_tracks = get_api_items("short-term top tracks",
                                          lambda: api.get("/me/top/tracks", time_range="short_term", limit=50), issues)
    medium_term_top_tracks = get_api_items("medium-term top tracks",
                                           lambda: api.get("/me/top/tracks", time_range="medium_term", limit=50), issues)
    long_term_top_tracks = get_api_items("long-term top tracks",
                                         lambda: api.get("/me/top/tracks", time_range="long_term", limit=50), issues)
    recent_entries = get_api_items("recent tracks", lambda: api.get("/me/player/recently-played", limit=50), issues)
    recently_played_tracks = [entry.get("track") for entry in recent_entries if isinstance(entry, dict)]
    liked_track_cache = get_api_items(
        "liked tracks", lambda: api.daily_cached("liked_tracks_cache", lambda: api.liked_tracks(rng)), issues
    )
    liked_tracks = [entry.get("track") for entry in liked_track_cache if isinstance(entry, dict)]
    short_term_top_tracks = [item for item in short_term_top_tracks if isinstance(item, dict)]
    medium_term_top_tracks = [item for item in medium_term_top_tracks if isinstance(item, dict)]
    long_term_top_tracks = [item for item in long_term_top_tracks if isinstance(item, dict)]
    recently_played_tracks = [item for item in recently_played_tracks if isinstance(item, dict)]

    # Older top tracks exclude the short-term and recently played tracks.
    current_ids = {t.get("id") for t in short_term_top_tracks + recently_played_tracks}
    older_top_tracks = [t for t in long_term_top_tracks if t.get("id") not in current_ids]
    top_and_recent_tracks = short_term_top_tracks + medium_term_top_tracks + recently_played_tracks

    # Get one random time range of top artists and add the persistent followed-artist cache.
    random_time_range = rng.choice(("short_term", "medium_term", "long_term"))
    top_artists = get_api_items("top artists",
                                lambda: api.get("/me/top/artists", time_range=random_time_range, limit=50), issues)
    followed_artists = get_api_items("followed artists", api.followed_artists, issues)

    # Include every credited artist, even from liked tracks outside the final track sample.
    artists_from_retrieved_tracks = [
        artist
        for item in (
            short_term_top_tracks + medium_term_top_tracks + long_term_top_tracks
            + recently_played_tracks + liked_tracks
        )
        if isinstance(item, dict) and isinstance(item.get("artists"), list)
        for artist in item["artists"]
        if isinstance(artist, dict) and artist.get("id")
    ]
    # Deduplicate artists from all sources, then shuffle before selecting any of them.
    artists = list({
        artist["id"]: artist
        for artist in top_artists + followed_artists + artists_from_retrieved_tracks
        if isinstance(artist, dict) and isinstance(artist.get("id"), str) and artist["id"]
    }.values())
    rng.shuffle(artists)
    discoveries = []

    def artist_tracks(artist_id, include_groups):
        # Find music from one artist. include_groups determines which kinds of
        # releases to search, such as albums, singles, or appearances.
        path = f"/artists/{artist_id}/albums"
        params = {"market": "NL", "include_groups": include_groups}

        # Spotify returns at most 10 releases per request. This first request
        # also tells us how many releases are available in total.
        try:
            page = api.get(path, **params, limit=10)
            if not isinstance(page, dict):
                raise SpotifyError("Spotify returned an invalid artist release page")
            total = page.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise SpotifyError("Spotify returned an invalid artist release count")
            if not isinstance(page.get("items"), list) or len(page["items"]) != min(total, 10):
                raise SpotifyError("Spotify returned an inconsistent artist release count")
        except (SpotifyError, RequestException) as exc:
            api_warning(issues, "Skipping releases for artist %s: %s", artist_id, exc)
            return []

        # Pick up to two different positions from all available releases.
        # Old and new releases have the same chance of being picked.
        tracks = []
        for index in rng.sample(range(total), min(total, 2)):
            try:
                if index < len(page["items"]):
                    # This release was included in the first request.
                    album = page["items"][index]
                else:
                    # Fetch only the selected position, not every page in between.
                    selected = api.get(path, **params, limit=1, offset=index)
                    if not isinstance(selected, dict) or not isinstance(selected.get("items"), list) \
                            or len(selected["items"]) != 1:
                        raise SpotifyError("Spotify returned an invalid sampled artist release")
                    album = selected["items"][0]

                if not isinstance(album, dict) or not isinstance(album.get("id"), str) or not album["id"]:
                    raise SpotifyError("Spotify returned an artist release without an ID")

                # The later selection decides which of these tracks enter the playlist.
                release = api.get(f"/albums/{album['id']}/tracks", market="NL", limit=50)
                if not isinstance(release, dict) or not isinstance(release.get("items"), list) \
                        or not all(
                            isinstance(item, dict)
                            and (item.get("artists") is None or isinstance(item["artists"], list))
                            for item in release["items"]
                        ):
                    raise SpotifyError("Spotify returned invalid artist release tracks")
                tracks.extend(release["items"])
            except (SpotifyError, RequestException) as exc:
                api_warning(issues, "Skipping release at position %s for artist %s: %s", index, artist_id, exc)
        return tracks

    artist_ids = {artist["id"] for artist in artists}
    collaborators = set()

    # Search up to eight artists and collect collaborators from their shared tracks.
    for artist in artists[:8]:
        tracks = artist_tracks(artist["id"], "album,single,appears_on")
        discoveries.extend(tracks)

        # Album track lists can include unrelated artists; require a shared credit.
        for item in tracks:
            credited = {
                credit["id"] for credit in (item.get("artists") or [])
                if isinstance(credit, dict) and credit.get("id")
            }
            if artist["id"] in credited:
                collaborators.update(credited - artist_ids)

    collaborator_ids = sorted(collaborators)
    rng.shuffle(collaborator_ids)
    collaborator_tracks = []

    # Search up to four collaborators, without following further collaborations.
    for artist_id in collaborator_ids[:4]:
        tracks = artist_tracks(artist_id, "album,single")
        collaborator_tracks.extend(
            item for item in tracks
            if any(isinstance(credit, dict) and credit.get("id") == artist_id
                   for credit in (item.get("artists") or []))
        )

    # Add tracks from each configured source playlist; local files cannot be used.
    configured = []
    for pid in playlist_ids:
        entries = get_api_items(
            f"playlist {pid}",
            lambda: api.pages(f"/playlists/{pid}/items", {"additional_types": "track", "market": "NL"}),
            issues,
        )
        configured.extend(entry.get("item") for entry in entries
                          if isinstance(entry, dict) and not entry.get("is_local"))

    # Top and recently played endpoints do not support market. Resolve candidates with
    # uncertain playability against the account's market before selection.
    cache = {}
    failed_tracks = set()

    def playable(items):
        result = []
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "track" or not item.get("id"):
                continue
            if item.get("is_playable") is None:
                tid = item["id"]
                # Do not request the same missing or failed track again in this refresh.
                if tid in failed_tracks:
                    continue
                if tid not in cache:
                    try:
                        cache[tid] = api.get(f"/tracks/{tid}", market="NL")
                    except (SpotifyError, RequestException) as exc:
                        failed_tracks.add(tid)
                        api_warning(issues, "Skipping track %s: %s", tid, exc)
                        continue
                item = cache[tid]
            if valid(item, "track"):
                result.append(item)
                if len(result) == 80:
                    break
        return result

    groups = (top_and_recent_tracks, older_top_tracks, discoveries, configured, liked_tracks, collaborator_tracks)

    # Shuffle before selecting up to 80 playable tracks from each group.
    for group in groups:
        rng.shuffle(group)
    return [playable(group) for group in groups]


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

    if not chosen and not remaining and not podcasts:
        raise SpotifyError("No playable music found")

    def fit(reserved, candidates):
        base = podcast_ms + sum(item["duration_ms"] for item in reserved)
        if base > TARGET + TOLERANCE:
            return None

        # Try different combinations of tracks using whole seconds to limit the search.
        # For each duration, keep one combination that fits within the item limit.
        capacity = (TARGET + TOLERANCE - base) // 1000
        states = {0: ()}

        for index, item in enumerate(candidates):
            seconds = max(1, item["duration_ms"] // 1000)
            for total, indices in list(states.items()):
                new = total + seconds
                if new <= capacity and new not in states and len(indices) + len(reserved) + len(podcasts) < 100:
                    states[new] = indices + (index,)

        # Pick the combination closest to 90 minutes and check its exact millisecond duration.
        best = min(
            states.values(),
            key=lambda ids: abs(TARGET - base - sum(candidates[i]["duration_ms"] for i in ids)),
        )
        result = reserved + [candidates[i] for i in best]
        duration = podcast_ms + sum(item["duration_ms"] for item in result)
        return result if abs(duration - TARGET) <= TOLERANCE else None

    result = fit(chosen, remaining)
    if result is None and chosen:
        # The preferred current/older tracks must not block a viable playlist.
        result = fit([], chosen + remaining)
    if result is None:
        raise SpotifyError("Not enough suitable content to stay within 88-92 minutes")
    return result


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
    issues = []

    # Gather candidates before selecting anything or changing the Spotify playlist.
    podcasts = podcast_candidates(api, news_shows, podcast_shows, issues)
    groups = music_candidates(api, playlist_ids, rng, issues)
    try:
        music = select_music(groups, podcasts, rng)
    except SpotifyError as exc:
        if issues:
            # Include a rate limit and a few source errors when the final selection fails.
            rate_limits = [issue for issue in issues if "Retry-After" in issue]
            details = list(dict.fromkeys(rate_limits[:1] + issues[:3]))
            raise SpotifyError(f"{exc}; source errors: {'; '.join(details)}") from exc
        raise
    result = interleave(music, podcasts, rng)
    total = sum(item["duration_ms"] for item in result)
    podcast_total = sum(item["duration_ms"] for item in podcasts)
    detail = f"{total / 60000:.1f} minutes; podcasts {podcast_total / total * 100:.1f}%"
    return result, [item["uri"] for item in podcasts], detail


def build(api, playlist_ids, news_shows, podcast_shows, rng=None):
    items, episodes, detail = build_items(api, playlist_ids, news_shows, podcast_shows, rng)
    return [item["uri"] for item in items], episodes, detail
