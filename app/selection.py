import logging
import random
from itertools import islice, zip_longest

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
        and not item.get("is_local", False)
        and (kind == "track" or (item.get("is_playable") is True and not item.get("restrictions")))
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


def podcast_candidates(api, news_shows, podcast_shows, issues=None, trace=None):
    news = []
    other_podcasts = []
    seen = set()

    # Read each show until its requested count is reached or there are no more pages.
    # Keep the episodes already found if a later page fails.
    for category, shows, episodes in (("News", news_shows, news), ("Podcast", podcast_shows, other_podcasts)):
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
            if trace is not None:
                titles = [item["name"] if isinstance(item.get("name"), str) and item["name"].strip()
                          else item["uri"] for item in current]
                trace.append(f"{category} show {show}: {len(current)}/{count} requested episodes selected, "
                             "using the latest playable episodes and excluding duplicates."
                             + (f" Selected: {', '.join(titles)}." if titles else ""))

    if trace is not None and not news_shows and not podcast_shows:
        trace.append("No news or podcast shows configured.")

    # Alternate news and other podcasts; keep the remaining episodes from either list.
    return [
        episode
        for pair in zip_longest(news, other_podcasts)
        for episode in pair
        if episode is not None
    ]


def music_candidates(api, playlist_ids, rng, issues=None, trace=None):
    def cached_items(key, path, **params):
        def fetch():
            result = api.get(path, **params)
            if isinstance(result, dict):
                result = result.get("items")
            if not isinstance(result, list):
                raise SpotifyError("Spotify returned an invalid item list")
            return result

        # Cache only successful, valid responses for the current Amsterdam day.
        return api.daily_cached(key, fetch, trace=trace)

    def cached_top_items(kind, time_range):
        # Keep each top-list time range in a separate cache entry.
        return cached_items(f"top_{kind}_{time_range}_cache", f"/me/top/{kind}",
                            time_range=time_range, limit=50)

    # Each source can fail independently without blocking the other sources.
    short_term_top_tracks = get_api_items("short-term top tracks",
                                          lambda: cached_top_items("tracks", "short_term"), issues)
    medium_term_top_tracks = get_api_items("medium-term top tracks",
                                           lambda: cached_top_items("tracks", "medium_term"), issues)
    long_term_top_tracks = get_api_items("long-term top tracks",
                                         lambda: cached_top_items("tracks", "long_term"), issues)
    recent_entries = get_api_items(
        "recent tracks",
        lambda: cached_items("recently_played_tracks_cache", "/me/player/recently-played", limit=50),
        issues,
    )
    recently_played_tracks = [entry.get("track") for entry in recent_entries if isinstance(entry, dict)]

    def fetch_liked_tracks():
        def fetch():
            return api.liked_tracks(rng)

        if trace is None:
            return api.daily_cached("liked_tracks_cache", fetch)
        return api.daily_cached("liked_tracks_cache", fetch, trace=trace)

    liked_track_cache = get_api_items(
        "liked tracks", fetch_liked_tracks, issues
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

    # Keep the random time range for the whole day, even if its Spotify request fails.
    range_chosen = False

    def choose_time_range():
        nonlocal range_chosen
        range_chosen = True
        return rng.choice(("short_term", "medium_term", "long_term"))

    random_time_range = api.daily_cached("top_artists_time_range_cache", choose_time_range)
    if trace is not None:
        origin = "chosen randomly for today" if range_chosen else "reused from today's cache"
        trace.append(f"Top artist time range: {random_time_range}; {origin}.")

    top_artists = get_api_items("top artists",
                                lambda: cached_top_items("artists", random_time_range), issues)
    fetch_followed = api.followed_artists if trace is None else lambda: api.followed_artists(trace=trace)
    followed_artists = get_api_items("followed artists", fetch_followed, issues)

    # Load configured playlists before building the artist pool so their artists
    # can also contribute discoveries.
    configured = []
    for pid in playlist_ids:
        entries = get_api_items(
            f"playlist {pid}",
            lambda: api.pages(f"/playlists/{pid}/items", {"additional_types": "track", "market": "NL"}),
            issues,
        )
        configured.extend(entry.get("item") for entry in entries
                          if isinstance(entry, dict) and not entry.get("is_local"))

    # Include credited artists from every retrieved track source.
    artists_from_retrieved_tracks = [
        artist
        for item in (
            short_term_top_tracks + medium_term_top_tracks + long_term_top_tracks
            + recently_played_tracks + liked_tracks + configured
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

    if trace is not None:
        selected_names = [
            artist["name"] if isinstance(artist.get("name"), str) and artist["name"].strip() else artist["id"]
            for artist in artists[:8]
        ]
        trace.append(f"Top artist time range: {random_time_range}; {len(artists)} unique artists from top, "
                     "followed and retrieved tracks, including source playlists, were shuffled.")
        trace.append("Artists searched for discovery (not necessarily selected for the playlist): "
                     + (", ".join(selected_names) if selected_names else "none") + ".")

    release_track_cache = {}
    release_tracks_reused = 0
    release_tracks_fetched = 0

    def artist_tracks(artist_id, include_groups):
        nonlocal release_tracks_reused, release_tracks_fetched
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
        indices = rng.sample(range(total), min(total, 2))
        combined = {}

        # Fetch nearby randomly chosen positions together without changing the selection.
        if len(indices) == 2 and min(indices) >= len(page["items"]) and max(indices) - min(indices) < 10:
            offset = min(indices)
            limit = max(indices) - offset + 1
            try:
                selected = api.get(path, **params, limit=limit, offset=offset)
                if not isinstance(selected, dict) or not isinstance(selected.get("items"), list) \
                        or len(selected["items"]) != limit:
                    raise SpotifyError("Spotify returned invalid combined artist releases")
                combined = {index: selected["items"][index - offset] for index in indices}
            except (SpotifyError, RequestException) as exc:
                api_warning(issues, "Combined release lookup for artist %s failed: %s; fetching separately",
                            artist_id, exc)

        tracks = []
        for index in indices:
            try:
                if index < len(page["items"]):
                    # This release was included in the first request.
                    album = page["items"][index]
                elif index in combined:
                    album = combined[index]
                else:
                    # Fetch only the selected position, not every page in between.
                    selected = api.get(path, **params, limit=1, offset=index)
                    if not isinstance(selected, dict) or not isinstance(selected.get("items"), list) \
                            or len(selected["items"]) != 1:
                        raise SpotifyError("Spotify returned an invalid randomly chosen artist release")
                    album = selected["items"][0]

                if not isinstance(album, dict) or not isinstance(album.get("id"), str) or not album["id"]:
                    raise SpotifyError("Spotify returned an artist release without an ID")

                # Reuse a release only within this refresh; failed requests are never cached.
                album_id = album["id"]
                if album_id in release_track_cache:
                    release_tracks_reused += 1
                else:
                    release = api.get(f"/albums/{album_id}/tracks", market="NL", limit=50)
                    if not isinstance(release, dict) or not isinstance(release.get("items"), list) \
                            or not all(
                                isinstance(item, dict)
                                and (item.get("artists") is None or isinstance(item["artists"], list))
                                for item in release["items"]
                            ):
                        raise SpotifyError("Spotify returned invalid artist release tracks")
                    release_track_cache[album_id] = release["items"]
                    release_tracks_fetched += 1
                tracks.extend(release_track_cache[album_id])
            except (SpotifyError, RequestException) as exc:
                api_warning(issues, "Skipping release at position %s for artist %s: %s", index, artist_id, exc)
        return tracks

    artist_ids = {artist["id"] for artist in artists}
    collaborators = set()
    collaborator_names = {}

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
                if trace is not None:
                    for credit in item.get("artists") or []:
                        if isinstance(credit, dict) and credit.get("id") and credit["id"] in collaborators:
                            name = credit.get("name")
                            if isinstance(name, str) and name.strip():
                                collaborator_names[credit["id"]] = name

    collaborator_ids = sorted(collaborators)
    rng.shuffle(collaborator_ids)
    collaborator_tracks = []

    if trace is not None:
        labels = [f"{collaborator_names[artist_id]} ({artist_id})" if artist_id in collaborator_names
                  else str(artist_id) for artist_id in collaborator_ids[:4]]
        trace.append("Collaborators searched: "
                     + (", ".join(labels) if labels else "none")
                     + "; up to two available releases per searched artist were randomly chosen.")

    # Search up to four collaborators, without following further collaborations.
    for artist_id in collaborator_ids[:4]:
        tracks = artist_tracks(artist_id, "album,single")
        collaborator_tracks.extend(
            item for item in tracks
            if any(isinstance(credit, dict) and credit.get("id") == artist_id
                   for credit in (item.get("artists") or []))
        )

    if trace is not None:
        trace.append(f"Release track lists: {release_tracks_reused} reused from this refresh's cache; "
                     f"{release_tracks_fetched} retrieved from Spotify during this refresh.")

    groups = (top_and_recent_tracks, older_top_tracks, discoveries, configured, liked_tracks, collaborator_tracks)

    # Shuffle before selecting up to 80 valid tracks from each group.
    for group in groups:
        rng.shuffle(group)
    return [list(islice((item for item in group if valid(item, "track") and item.get("id")), 80)) for group in groups]


def select_music(groups, podcasts, rng, trace=None):
    chosen = []
    seen = {p["uri"] for p in podcasts}
    normalized = []
    sources = {}

    # Deduplicate across groups before choosing so a track cannot occupy two slots.
    for group in groups:
        pool = []
        for item in group:
            if item["uri"] not in seen:
                seen.add(item["uri"])
                sources[item["uri"]] = len(normalized)
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
        raise SpotifyError("No suitable music found")

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
    used_fallback = result is None and bool(chosen)
    if result is None and chosen:
        # The preferred current/older tracks must not block a viable playlist.
        result = fit([], chosen + remaining)
    if result is None:
        raise SpotifyError("Not enough suitable content to stay within 88-92 minutes")
    if trace is not None:
        group_names = ("current and recent", "older favorites", "artist discoveries",
                       "source playlists", "liked tracks", "collaborators")
        counts = [0] * len(groups)
        for item in result:
            counts[sources[item["uri"]]] += 1
        breakdown = ", ".join(f"{name}: {count}" for name, count in zip(group_names, counts))
        trace.append("Selected music by candidate group (duplicates credited to the first group): " + breakdown + ".")
        if used_fallback:
            trace.append("The first duration fit with reserved favorites failed; selection was retried without them.")
        else:
            trace.append(f"{len(chosen)} available current/older favorites were reserved before duration fitting.")
        trace.append("Music was selected with the episodes to fit 90 minutes within 2 minutes and 100 items; "
                     "episodes were spread through the music.")
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


def build_items(api, playlist_ids, news_shows, podcast_shows, rng=None, trace=None):
    rng = rng or random.SystemRandom()
    issues = []

    # Gather candidates before selecting anything or changing the Spotify playlist.
    podcasts = podcast_candidates(api, news_shows, podcast_shows, issues, trace)
    groups = music_candidates(api, playlist_ids, rng, issues, trace)
    try:
        music = select_music(groups, podcasts, rng, trace)
    except SpotifyError as exc:
        message = str(exc)
        if issues:
            # Include a rate limit and a few source errors when the final selection fails.
            rate_limits = [issue for issue in issues if "Retry-After" in issue]
            details = list(dict.fromkeys(rate_limits[:1] + issues[:3]))
            message += f"; source errors: {'; '.join(details)}"
        if trace is not None:
            trace.extend(f"Source warning: {issue}" for issue in issues)
            if trace:
                message += "\nSelection:\n" + "\n".join(" ".join(line.split()) for line in trace)
        raise SpotifyError(message) from exc
    result = interleave(music, podcasts, rng)
    total = sum(item["duration_ms"] for item in result)
    podcast_total = sum(item["duration_ms"] for item in podcasts)
    detail = f"{total / 60000:.1f} minutes; podcasts {podcast_total / total * 100:.1f}%"
    if trace is not None:
        trace.extend(f"Source warning: {issue}" for issue in issues)
    return result, [item["uri"] for item in podcasts], detail


def build(api, playlist_ids, news_shows, podcast_shows, rng=None):
    trace = []
    items, episodes, detail = build_items(api, playlist_ids, news_shows, podcast_shows, rng, trace)
    episode_uris = set(episodes)
    detail += f"; {sum(item['uri'] not in episode_uris for item in items)} music tracks"
    if trace:
        detail += "\nSelection:\n" + "\n".join(" ".join(line.split()) for line in trace)
    if items:
        labels = []
        for item in items:
            title = item.get("name")
            title = title if isinstance(title, str) and title.strip() else item["uri"]
            if item["uri"] in episode_uris:
                label = f"[Episode] {title}"
            else:
                artists = item.get("artists")
                artists = artists if isinstance(artists, list) else []
                names = [artist["name"] for artist in artists
                         if isinstance(artist, dict) and isinstance(artist.get("name"), str)
                         and artist["name"].strip()]
                label = f"{', '.join(names)} - {title}" if names else title
            labels.append(" ".join(label.split()))
        detail += "\nItems:\n" + "\n".join(labels)
    return [item["uri"] for item in items], episodes, detail
