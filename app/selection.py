import logging
import random
from itertools import islice, zip_longest

from requests import RequestException

from .constants import (
    ARTIST_DISCOVERY_RELEASE_GROUPS, ARTIST_RELEASE_ERROR_PREFIXES, ARTIST_RELEASE_PAGE_SIZE,
    CACHE_KEY_SUFFIX, COLLABORATOR_RELEASE_GROUPS, COMBINED_ARTIST_RELEASE_LOOKUP_PREFIX,
    DURATION_TOLERANCE_MINUTES, EMPTY_ARTIST_POOL_ERROR, EMPTY_GROUP_ERROR, EPISODE_TYPE,
    HISTORY_TOP_ARTISTS_PREFIX, ITEMS_DETAIL_SEPARATOR, LIKED_TRACKS_CACHE_KEY, LONG_TERM,
    MAX_ARTISTS_FOR_DISCOVERY, MAX_COLLABORATORS_FOR_DISCOVERY, MAX_RELEASES_PER_ARTIST,
    MAX_RELEASES_PER_ARTIST_TEXT, MAX_SPOTIFY_PLAYLIST_ITEMS, MAX_TRACKS_PER_CANDIDATE_GROUP,
    MEDIUM_TERM, MILLISECONDS_PER_MINUTE, MILLISECONDS_PER_SECOND, MUSIC_GROUP_NAMES,
    PERCENTAGE_BASE, RESERVED_FAVORITE_GROUP_COUNT,
    PLAYLIST_SOURCE_ERROR_PREFIXES, RECENT_TRACKS_CACHE_KEY, SELECTION_DETAIL_SEPARATOR,
    SHOW_EPISODE_CACHE_PREFIX, SHOW_EPISODE_FETCH_FAILURE_PREFIX, SHOW_HISTORY_SEPARATOR,
    SHORT_TERM, SKIP_ARTIST_RELEASES_PREFIX, SKIP_RELEASE_POSITION_PREFIX, SOURCE_CACHED_PREFIX,
    SOURCE_FOLLOWED_ARTISTS, SOURCE_LIKED_TRACKS, SOURCE_LONG_TERM_TOP_TRACKS,
    SOURCE_MEDIUM_TERM_TOP_TRACKS, SOURCE_PARTIAL_PREFIX, SOURCE_PLAYLISTS,
    SOURCE_RECENT_TRACKS,
    SOURCE_SHORT_TERM_TOP_TRACKS, SOURCE_SKIP_PREFIX, SOURCE_TOP_ARTISTS, SOURCE_WARNING_PREFIX,
    SPOTIFY_ALBUM_TRACKS_PATH, SPOTIFY_ARTIST_ALBUMS_PATH, SPOTIFY_PAGE_SIZE,
    SPOTIFY_PLAYLIST_ITEMS_PATH, SPOTIFY_RECENT_TRACKS_PATH, SPOTIFY_SHOW_EPISODES_PATH,
    SPOTIFY_TOP_ITEMS_PATH, TARGET_DURATION_MINUTES,
    TOP_ARTISTS_TIME_RANGE_CACHE_KEY, TOP_ITEMS_CACHE_KEY_PREFIX, TOP_TIME_RANGE_LABELS,
    TOP_TIME_RANGES, TRACK_TYPE,
    ZERO_RELEASE_LISTS_ERROR, ZERO_SHOW_EPISODES_ERROR,
)
from .spotify import SpotifyCachedError, SpotifyError, SpotifyPartialError

log = logging.getLogger(__name__)

TARGET = TARGET_DURATION_MINUTES * MILLISECONDS_PER_MINUTE
TOLERANCE = DURATION_TOLERANCE_MINUTES * MILLISECONDS_PER_MINUTE


def valid(item, kind):
    return (
        isinstance(item, dict)
        and item.get("type") == kind
        and item.get("uri")
        and isinstance(item.get("duration_ms"), int) and item["duration_ms"] > 0
        and not item.get("is_local", False)
        and (kind == TRACK_TYPE or (item.get("is_playable") is True and not item.get("restrictions")))
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
    except SpotifyCachedError as exc:
        api_warning(issues, SOURCE_CACHED_PREFIX + "%s after an error: %s", source, exc)
        return exc.items
    except SpotifyPartialError as exc:
        # A later page failed, but the items from earlier pages can still be used.
        api_warning(issues, SOURCE_PARTIAL_PREFIX + "%s after an error: %s", source, exc)
        return exc.items
    except (SpotifyError, RequestException) as exc:
        # Skip this source so the remaining sources can still build the playlist.
        api_warning(issues, SOURCE_SKIP_PREFIX + "%s: %s", source, exc)
        return []


def source_error(issues, source):
    prefixes = (
        f"{SOURCE_SKIP_PREFIX}{source}:",
        f"{SOURCE_CACHED_PREFIX}{source} after an error:",
        f"{SOURCE_PARTIAL_PREFIX}{source} after an error:",
    )
    return any(issue.startswith(prefixes) for issue in (issues or []))


def podcast_candidates(api, news_shows, podcast_shows, issues=None, trace=None):
    news = []
    other_podcasts = []
    seen = set()

    # Read each show until its requested count is reached or there are no more pages.
    # Keep the episodes already found if a later page fails.
    show_groups = (
        ("News", news_shows, news),
        ("Podcast", podcast_shows, other_podcasts),
    )
    for category, shows, episodes in show_groups:
        for entry in shows:
            show, count = entry["show"], entry["count"]
            current = []
            offset = 0
            failed = False

            while len(current) < count:
                try:
                    page = api.get(
                        SPOTIFY_SHOW_EPISODES_PATH.format(show_id=show),
                        market=api.market,
                        limit=SPOTIFY_PAGE_SIZE,
                        offset=offset,
                    )
                    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                        raise SpotifyError("Spotify returned an invalid episode page")
                except (SpotifyError, RequestException) as exc:
                    api_warning(
                        issues, SHOW_EPISODE_FETCH_FAILURE_PREFIX + "%s: %s", show, exc
                    )
                    failed = True
                    break
                for item in page["items"]:
                    if valid(item, EPISODE_TYPE) and item["uri"] not in seen:
                        current.append(item)
                        seen.add(item["uri"])
                        if len(current) == count:
                            break
                if not page.get("next") or not page["items"]:
                    break
                offset += len(page["items"])

            # Use saved episodes only after an error; fresh results always come first.
            fetched = list(current)
            cache_key = f"{SHOW_EPISODE_CACHE_PREFIX}{show}{CACHE_KEY_SUFFIX}"
            cached = api.cached_today(cache_key)
            if cached is None:
                cached = []
            if not isinstance(cached, list):
                raise SpotifyError(f"Invalid cached episodes for show {show}")

            reused = 0
            if failed:
                for item in cached:
                    if valid(item, EPISODE_TYPE) and item["uri"] not in seen:
                        current.append(item)
                        seen.add(item["uri"])
                        reused += 1
                        if len(current) == count:
                            break

            # Keep today's newest fetched episodes, including successful pages before an error.
            if fetched:
                saved = []
                saved_uris = set()
                for item in fetched + cached:
                    if valid(item, EPISODE_TYPE) and item["uri"] not in saved_uris:
                        saved.append(item)
                        saved_uris.add(item["uri"])
                        if len(saved) >= max(count, len(cached)):
                            break
                api.cache_today(cache_key, saved)

            if len(current) != count:
                api_warning(
                    issues,
                    "Show %s has %s of %s requested playable episodes",
                    show,
                    len(current),
                    count,
                )
            episodes.extend(current)
            if trace is not None:
                titles = [
                    item["name"] if isinstance(item.get("name"), str) and item["name"].strip()
                    else item["uri"]
                    for item in current
                ]
                selection = (
                    "using available playable episodes"
                    if failed else "using the latest playable episodes"
                )
                cache_detail = ""
                if failed:
                    if reused:
                        noun = "episode" if reused == 1 else "episodes"
                        cache_detail = (
                            f" {reused} previously fetched {noun} reused from today's cache "
                            "after a Spotify error."
                        )
                    else:
                        cache_detail = " No episodes from today's cache were available."
                failure_detail = (
                    f" {ZERO_SHOW_EPISODES_ERROR}." if failed and not current else ""
                )
                trace.append(
                    f"{category}{SHOW_HISTORY_SEPARATOR}{show}: "
                    f"{len(current)}/{count} requested episodes selected, "
                    f"{selection} and excluding duplicates."
                    + failure_detail
                    + cache_detail
                    + (f" Selected: {', '.join(titles)}." if titles else "")
                )

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
    def cached_items(key, path, trace_target=None, **params):
        def fetch():
            result = api.get(path, **params)
            if isinstance(result, dict):
                result = result.get("items")
            if not isinstance(result, list):
                raise SpotifyError("Spotify returned an invalid item list")
            return result

        # Cache only successful, valid responses for the current local day.
        return api.daily_cached(key, fetch, trace=trace if trace_target is None else trace_target)

    def cached_top_items(kind, time_range, trace_target=None):
        # Keep each top-list time range in a separate cache entry.
        return cached_items(
            f"{TOP_ITEMS_CACHE_KEY_PREFIX}{kind}_{time_range}{CACHE_KEY_SUFFIX}",
            SPOTIFY_TOP_ITEMS_PATH.format(kind=kind),
            trace_target=trace_target,
            time_range=time_range,
            limit=SPOTIFY_PAGE_SIZE,
        )

    # Each source can fail independently without blocking the other sources.
    short_term_top_tracks = get_api_items(
        SOURCE_SHORT_TERM_TOP_TRACKS, lambda: cached_top_items("tracks", SHORT_TERM), issues
    )
    medium_term_top_tracks = get_api_items(
        SOURCE_MEDIUM_TERM_TOP_TRACKS, lambda: cached_top_items("tracks", MEDIUM_TERM), issues
    )
    long_term_top_tracks = get_api_items(
        SOURCE_LONG_TERM_TOP_TRACKS, lambda: cached_top_items("tracks", LONG_TERM), issues
    )
    recent_entries = get_api_items(
        SOURCE_RECENT_TRACKS,
        lambda: cached_items(
            RECENT_TRACKS_CACHE_KEY, SPOTIFY_RECENT_TRACKS_PATH, limit=SPOTIFY_PAGE_SIZE
        ),
        issues,
    )
    recently_played_tracks = [
        entry.get("track") for entry in recent_entries if isinstance(entry, dict)
    ]

    def fetch_liked_tracks():
        def fetch():
            return api.liked_tracks(rng)

        if trace is None:
            return api.daily_cached(LIKED_TRACKS_CACHE_KEY, fetch)
        return api.daily_cached(LIKED_TRACKS_CACHE_KEY, fetch, trace=trace)

    liked_track_cache = get_api_items(SOURCE_LIKED_TRACKS, fetch_liked_tracks, issues)
    liked_tracks = [
        entry.get("track") for entry in liked_track_cache if isinstance(entry, dict)
    ]
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
        return rng.choice(TOP_TIME_RANGES)

    random_time_range = api.daily_cached(TOP_ARTISTS_TIME_RANGE_CACHE_KEY, choose_time_range)
    top_artist_trace = [] if trace is not None else None
    top_artists = get_api_items(
        SOURCE_TOP_ARTISTS,
        lambda: cached_top_items("artists", random_time_range, top_artist_trace),
        issues,
    )
    if trace is None:
        fetch_followed = api.followed_artists
    else:
        fetch_followed = lambda: api.followed_artists(trace=trace)
    followed_artists = get_api_items(SOURCE_FOLLOWED_ARTISTS, fetch_followed, issues)

    # Load configured playlists before building the artist pool so their artists
    # can also contribute discoveries.
    configured = []
    for pid in playlist_ids:
        entries = get_api_items(
            f"{SOURCE_PLAYLISTS} {pid}",
            lambda: api.pages(
                SPOTIFY_PLAYLIST_ITEMS_PATH.format(playlist_id=pid),
                {"additional_types": TRACK_TYPE, "market": api.market},
            ),
            issues,
        )
        configured.extend(
            entry.get("item")
            for entry in entries
            if isinstance(entry, dict) and not entry.get("is_local")
        )

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
    artists = list(
        {
            artist["id"]: artist
            for artist in top_artists + followed_artists + artists_from_retrieved_tracks
            if isinstance(artist, dict)
            and isinstance(artist.get("id"), str)
            and artist["id"]
        }.values()
    )
    rng.shuffle(artists)
    discoveries = []

    if trace is not None:
        selected_names = [
            artist["name"]
            if isinstance(artist.get("name"), str) and artist["name"].strip()
            else artist["id"]
            for artist in artists[:MAX_ARTISTS_FOR_DISCOVERY]
        ]
        fetch_detail = "; ".join(
            line.partition(": ")[2].removesuffix(".") for line in top_artist_trace
        )
        range_origin = "chosen randomly for today" if range_chosen else "reused from today's cache"
        artist_sources = (
            SOURCE_SHORT_TERM_TOP_TRACKS,
            SOURCE_MEDIUM_TERM_TOP_TRACKS,
            SOURCE_LONG_TERM_TOP_TRACKS,
            SOURCE_RECENT_TRACKS,
            SOURCE_LIKED_TRACKS,
            SOURCE_TOP_ARTISTS,
            SOURCE_FOLLOWED_ARTISTS,
        )
        artist_source_failed = (
            any(source_error(issues, source) for source in artist_sources)
            or any(issue.startswith(PLAYLIST_SOURCE_ERROR_PREFIXES) for issue in (issues or []))
        )
        artist_pool_detail = (
            f"{len(artists)} unique artists from top, followed and retrieved tracks, "
            "including source playlists"
        )
        if not artists and artist_source_failed:
            artist_pool_detail += f"; {EMPTY_ARTIST_POOL_ERROR}"
        else:
            artist_pool_detail += " were shuffled"
        trace.append(
            f"{HISTORY_TOP_ARTISTS_PREFIX}{TOP_TIME_RANGE_LABELS[random_time_range]}: "
            f"{fetch_detail}; time range {range_origin}; {artist_pool_detail}."
        )
        trace.append("Artists searched for discovery (not necessarily selected for the playlist): "
                     + (", ".join(selected_names) if selected_names else "none") + ".")

    release_track_cache = {}
    release_tracks_reused = 0
    release_tracks_fetched = 0

    def artist_tracks(artist_id, include_groups):
        nonlocal release_tracks_reused, release_tracks_fetched
        # Find music from one artist. include_groups determines which kinds of
        # releases to search, such as albums, singles, or appearances.
        path = SPOTIFY_ARTIST_ALBUMS_PATH.format(artist_id=artist_id)
        params = {"market": api.market, "include_groups": include_groups}

        # Spotify returns at most ARTIST_RELEASE_PAGE_SIZE releases per request. This first request
        # also tells us how many releases are available in total.
        try:
            page = api.get(path, **params, limit=ARTIST_RELEASE_PAGE_SIZE)
            if not isinstance(page, dict):
                raise SpotifyError("Spotify returned an invalid artist release page")
            total = page.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise SpotifyError("Spotify returned an invalid artist release count")
            if (
                not isinstance(page.get("items"), list)
                or len(page["items"]) != min(total, ARTIST_RELEASE_PAGE_SIZE)
            ):
                raise SpotifyError("Spotify returned an inconsistent artist release count")
        except (SpotifyError, RequestException) as exc:
            api_warning(
                issues, SKIP_ARTIST_RELEASES_PREFIX + "%s: %s", artist_id, exc
            )
            return []

        # Pick random positions from the full list so old and new releases have equal odds.
        indices = rng.sample(range(total), min(total, MAX_RELEASES_PER_ARTIST))
        combined = {}

        # Fetch nearby randomly chosen positions together without changing the selection.
        if (
            len(indices) == MAX_RELEASES_PER_ARTIST
            and min(indices) >= len(page["items"])
            and max(indices) - min(indices) < ARTIST_RELEASE_PAGE_SIZE
        ):
            offset = min(indices)
            limit = max(indices) - offset + 1
            try:
                selected = api.get(path, **params, limit=limit, offset=offset)
                if (
                    not isinstance(selected, dict)
                    or not isinstance(selected.get("items"), list)
                    or len(selected["items"]) != limit
                ):
                    raise SpotifyError("Spotify returned invalid combined artist releases")
                combined = {index: selected["items"][index - offset] for index in indices}
            except (SpotifyError, RequestException) as exc:
                api_warning(
                    issues,
                    COMBINED_ARTIST_RELEASE_LOOKUP_PREFIX
                    + "%s failed: %s; fetching separately",
                    artist_id,
                    exc,
                )

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
                    if (
                        not isinstance(selected, dict)
                        or not isinstance(selected.get("items"), list)
                        or len(selected["items"]) != 1
                    ):
                        raise SpotifyError("Spotify returned an invalid randomly chosen artist release")
                    album = selected["items"][0]

                if (
                    not isinstance(album, dict)
                    or not isinstance(album.get("id"), str)
                    or not album["id"]
                ):
                    raise SpotifyError("Spotify returned an artist release without an ID")

                # Reuse a release only within this refresh; failed requests are never cached.
                album_id = album["id"]
                if album_id in release_track_cache:
                    release_tracks_reused += 1
                else:
                    release = api.get(
                        SPOTIFY_ALBUM_TRACKS_PATH.format(album_id=album_id),
                        market=api.market,
                        limit=SPOTIFY_PAGE_SIZE,
                    )
                    if (
                        not isinstance(release, dict)
                        or not isinstance(release.get("items"), list)
                        or not all(
                            isinstance(item, dict)
                            and (item.get("artists") is None or isinstance(item["artists"], list))
                            for item in release["items"]
                        )
                    ):
                        raise SpotifyError("Spotify returned invalid artist release tracks")
                    release_track_cache[album_id] = release["items"]
                    release_tracks_fetched += 1
                tracks.extend(release_track_cache[album_id])
            except (SpotifyError, RequestException) as exc:
                api_warning(
                    issues,
                    SKIP_RELEASE_POSITION_PREFIX + "%s for artist %s: %s",
                    index,
                    artist_id,
                    exc,
                )
        return tracks

    artist_ids = {artist["id"] for artist in artists}
    collaborators = set()
    collaborator_names = {}

    # Search the selected artists and collect collaborators from their shared tracks.
    for artist in artists[:MAX_ARTISTS_FOR_DISCOVERY]:
        tracks = artist_tracks(artist["id"], ARTIST_DISCOVERY_RELEASE_GROUPS)
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
                        if (
                            isinstance(credit, dict)
                            and credit.get("id")
                            and credit["id"] in collaborators
                        ):
                            name = credit.get("name")
                            if isinstance(name, str) and name.strip():
                                collaborator_names[credit["id"]] = name

    collaborator_ids = sorted(collaborators)
    rng.shuffle(collaborator_ids)
    collaborator_tracks = []

    if trace is not None:
        labels = [
            f"{collaborator_names[artist_id]} ({artist_id})"
            if artist_id in collaborator_names else str(artist_id)
            for artist_id in collaborator_ids[:MAX_COLLABORATORS_FOR_DISCOVERY]
        ]
        trace.append(
            "Collaborators searched: "
            + (", ".join(labels) if labels else "none")
            + f"; up to {MAX_RELEASES_PER_ARTIST_TEXT} available releases per searched artist "
            "were randomly chosen."
        )

    # Search collaborators once; do not recursively follow their collaborators.
    for artist_id in collaborator_ids[:MAX_COLLABORATORS_FOR_DISCOVERY]:
        tracks = artist_tracks(artist_id, COLLABORATOR_RELEASE_GROUPS)
        collaborator_tracks.extend(
            item for item in tracks
            if any(isinstance(credit, dict) and credit.get("id") == artist_id
                   for credit in (item.get("artists") or []))
        )

    if trace is not None:
        release_request_failed = any(
            issue.startswith(ARTIST_RELEASE_ERROR_PREFIXES) for issue in (issues or [])
        )
        release_failure_detail = (
            f"; {ZERO_RELEASE_LISTS_ERROR}"
            if release_tracks_fetched == 0 and release_request_failed else ""
        )
        trace.append(
            f"Release track lists: {release_tracks_reused} reused from this refresh's cache; "
            f"{release_tracks_fetched} retrieved from Spotify during this refresh"
            f"{release_failure_detail}."
        )

    groups = (
        top_and_recent_tracks, older_top_tracks, discoveries,
        configured, liked_tracks, collaborator_tracks,
    )

    # Shuffle each group before selecting its valid tracks.
    for group in groups:
        rng.shuffle(group)
    return [
        list(
            islice(
                (item for item in group if valid(item, TRACK_TYPE) and item.get("id")),
                MAX_TRACKS_PER_CANDIDATE_GROUP,
            )
        )
        for group in groups
    ]


def select_music(groups, podcasts, rng, trace=None, issues=None):
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
    for pool in normalized[:RESERVED_FAVORITE_GROUP_COUNT]:
        if pool and len(chosen) < MAX_SPOTIFY_PLAYLIST_ITEMS - len(podcasts):
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
        capacity = (TARGET + TOLERANCE - base) // MILLISECONDS_PER_SECOND
        states = {0: ()}

        for index, item in enumerate(candidates):
            seconds = max(1, item["duration_ms"] // MILLISECONDS_PER_SECOND)
            for total, indices in list(states.items()):
                new = total + seconds
                if (
                    new <= capacity
                    and new not in states
                    and len(indices) + len(reserved) + len(podcasts)
                    < MAX_SPOTIFY_PLAYLIST_ITEMS
                ):
                    states[new] = indices + (index,)

        # Pick the combination closest to the target and check its exact millisecond duration.
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
        raise SpotifyError(
            f"Not enough suitable content to stay within "
            f"{TARGET_DURATION_MINUTES - DURATION_TOLERANCE_MINUTES}-"
            f"{TARGET_DURATION_MINUTES + DURATION_TOLERANCE_MINUTES} minutes"
        )
    if trace is not None:
        group_names = MUSIC_GROUP_NAMES
        counts = [0] * len(groups)
        for item in result:
            counts[sources[item["uri"]]] += 1
        artist_release_failed = any(
            issue.startswith(ARTIST_RELEASE_ERROR_PREFIXES) for issue in (issues or [])
        )
        group_errors = (
            (
                source_error(issues, SOURCE_SHORT_TERM_TOP_TRACKS)
                or source_error(issues, SOURCE_MEDIUM_TERM_TOP_TRACKS)
                or source_error(issues, SOURCE_RECENT_TRACKS)
            ),
            source_error(issues, SOURCE_LONG_TERM_TOP_TRACKS),
            (
                source_error(issues, SOURCE_TOP_ARTISTS)
                or source_error(issues, SOURCE_FOLLOWED_ARTISTS)
                or artist_release_failed
            ),
            any(issue.startswith(PLAYLIST_SOURCE_ERROR_PREFIXES) for issue in (issues or [])),
            source_error(issues, SOURCE_LIKED_TRACKS),
            artist_release_failed,
        )
        breakdown = ", ".join(
            f"{name}: {count}"
            + (f" ({EMPTY_GROUP_ERROR})" if not groups[index] and group_errors[index] else "")
            for index, (name, count) in enumerate(zip(group_names, counts))
        )
        trace.append(
            "Selected music by candidate group (duplicates credited to the first group): "
            + breakdown
            + "."
        )
        if used_fallback:
            trace.append(
                "The first duration fit with reserved favorites failed; "
                "selection was retried without them."
            )
        else:
            trace.append(
                f"{len(chosen)} available current/older favorites were reserved before duration fitting."
            )
        trace.append(
            f"Music was selected with the episodes to fit {TARGET_DURATION_MINUTES} minutes within "
            f"{DURATION_TOLERANCE_MINUTES} minutes and {MAX_SPOTIFY_PLAYLIST_ITEMS} items; "
            "episodes were spread through the music."
        )
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
        music = select_music(groups, podcasts, rng, trace, issues)
    except SpotifyError as exc:
        message = str(exc)
        if issues:
            # Include a rate limit and a few source errors when the final selection fails.
            rate_limits = [issue for issue in issues if "Retry-After" in issue]
            details = list(dict.fromkeys(rate_limits[:1] + issues[:3]))
            message += f"; source errors: {'; '.join(details)}"
        if trace is not None:
            trace.extend(f"{SOURCE_WARNING_PREFIX}{issue}" for issue in issues)
            if trace:
                message += (
                    SELECTION_DETAIL_SEPARATOR
                    + "\n".join(" ".join(line.split()) for line in trace)
                )
        raise SpotifyError(message) from exc
    result = interleave(music, podcasts, rng)
    total = sum(item["duration_ms"] for item in result)
    podcast_total = sum(item["duration_ms"] for item in podcasts)
    podcast_percentage = podcast_total / total * PERCENTAGE_BASE
    detail = (
        f"{total / MILLISECONDS_PER_MINUTE:.1f} minutes; "
        f"podcasts {podcast_percentage:.1f}%"
    )
    if trace is not None:
        trace.extend(f"{SOURCE_WARNING_PREFIX}{issue}" for issue in issues)
    return result, [item["uri"] for item in podcasts], detail


def build(api, playlist_ids, news_shows, podcast_shows, rng=None):
    trace = []
    items, episodes, detail = build_items(api, playlist_ids, news_shows, podcast_shows, rng, trace)
    episode_uris = set(episodes)
    detail += f"; {sum(item['uri'] not in episode_uris for item in items)} music tracks"
    if trace:
        detail += SELECTION_DETAIL_SEPARATOR + "\n".join(" ".join(line.split()) for line in trace)
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
        detail += ITEMS_DETAIL_SEPARATOR + "\n".join(labels)
    return [item["uri"] for item in items], episodes, detail
