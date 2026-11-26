import os
import random
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from requests import RequestException

from app.refresh import Refresh
from app.selection import (
    build, build_items, interleave, music_candidates, select_music, podcast_candidates, get_api_items,
)
from app.spotify import (
    Spotify, SpotifyError, SpotifyRateLimitError, authorization, callback_code, playlist_id, show_id, token_request,
)
from app.store import Store

NEWS = "news"
PODCASTS = ["podcast1", "podcast2"]
NEWS_SHOWS = [{"show": NEWS, "count": 4}]
PODCAST_SHOWS = [{"show": show, "count": 1} for show in PODCASTS]


def track(number, minutes=4):
    return {"uri": f"spotify:track:{number}", "duration_ms": minutes * 60000}


class UncachedSpotify:
    # Selection fakes bypass SQLite; cache behavior is tested with the real Store.
    def daily_cached(self, key, fetch, trace=None):
        return fetch()

    def liked_tracks(self, rng):
        return self.pages("/me/tracks", {"market": "NL"}, max_items=None)


class SelectionTests(unittest.TestCase):
    def test_selects_current_and_old_and_meets_duration(self):
        groups = [[track(i) for i in range(10)],
                  [track(i) for i in range(10, 20)],
                  [track(i) for i in range(20, 45)], []]
        podcasts = [{"uri": f"spotify:episode:{i}", "duration_ms": 7 * 60000} for i in range(5)]
        music = select_music(groups, podcasts, random.Random(3))
        total = sum(x["duration_ms"] for x in music + podcasts)
        self.assertLessEqual(abs(total - 90 * 60000), 2 * 60000)
        self.assertTrue(any(x in groups[0] for x in music))
        self.assertTrue(any(x in groups[1] for x in music))
        self.assertEqual(len({x["uri"] for x in music}), len(music))
        self.assertLessEqual(len(music) + len(podcasts), 100)
        self.assertEqual(len(interleave(music, podcasts, random.Random(2))), len(music) + 5)

    def test_too_little_music_fails(self):
        with self.assertRaisesRegex(SpotifyError, "88-92 minutes"):
            select_music([[track(1)], [], [], []], [], random.Random(1))

    def test_long_podcasts_do_not_exceed_duration_limit(self):
        podcasts = [{"uri": "spotify:episode:1", "duration_ms": 91 * 60000}]
        self.assertEqual(select_music([[track(1)], [], [], []], podcasts, random.Random(1)), [])

    def test_duration_selection_can_skip_long_reserved_track(self):
        podcasts = [{"uri": "spotify:episode:1", "duration_ms": 89 * 60000}]
        trace = []
        music = select_music([[track(1, 4)], [], [track(2, 1)]], podcasts, random.Random(1), trace)
        self.assertEqual(music, [track(2, 1)])
        self.assertIn("The first duration fit with reserved favorites failed; selection was retried without them.",
                      trace)
        self.assertIn("artist discoveries: 1", " ".join(trace))

    def test_duration_accepts_both_tolerance_boundaries(self):
        for podcast_minutes, total_minutes in ((84, 88), (88, 92)):
            with self.subTest(total_minutes=total_minutes):
                podcasts = [{"uri": "spotify:episode:1", "duration_ms": podcast_minutes * 60000}]
                music = select_music([[track(1)], [], [], []], podcasts, random.Random(1))
                self.assertEqual(sum(item["duration_ms"] for item in music + podcasts), total_minutes * 60000)

    def test_fetches_and_mixes_four_news_and_two_other_podcasts(self):
        class Fake:
            def get(self, path, **params):
                show = path.split("/")[2]
                return {"next": None, "items": [
                    {"type": "episode", "uri": f"{show}:{i}", "duration_ms": 1000,
                     "is_playable": i != 0} for i in range(5)]}
        episodes = podcast_candidates(Fake(), NEWS_SHOWS, PODCAST_SHOWS)
        expected = [f"{NEWS}:1", f"{PODCASTS[0]}:1", f"{NEWS}:2",
                    f"{PODCASTS[1]}:1", f"{NEWS}:3", f"{NEWS}:4"]
        self.assertEqual([episode["uri"] for episode in episodes], expected)
        self.assertEqual(len({x["uri"] for x in episodes}), 6)
        self.assertTrue(all(not x["uri"].endswith(":0") for x in episodes))

        # Check both the episode order and the spacing between episodes.
        result = interleave([track(i) for i in range(12)], episodes, random.Random(1))
        positions = [i for i, item in enumerate(result) if item in episodes]
        self.assertTrue(all(b - a > 1 for a, b in zip(positions, positions[1:])))
        self.assertGreater(positions[0], 0)
        gaps = [positions[0]] + [b - a - 1 for a, b in zip(positions, positions[1:])]
        gaps.append(len(result) - positions[-1] - 1)
        self.assertLessEqual(max(gaps) - min(gaps), 1)

    def test_fourth_news_episode_can_come_from_next_page(self):
        class Fake:
            def get(self, path, **params):
                show = path.split("/")[2]
                offset = params["offset"]
                numbers = [0, 1, 2] if offset == 0 else [2, 3]
                return {"next": "next" if offset == 0 and show == NEWS else None,
                        "items": [{"type": "episode", "uri": f"{show}:{i}",
                                   "duration_ms": 1000, "is_playable": True} for i in numbers]}
        episodes = podcast_candidates(Fake(), NEWS_SHOWS, PODCAST_SHOWS)
        self.assertEqual([x["uri"] for x in episodes if x["uri"].startswith(NEWS)],
                         [f"{NEWS}:{i}" for i in range(4)])

    def test_empty_shows_do_not_fetch_episodes(self):
        class Fake:
            def get(self, *args, **kwargs):
                raise AssertionError("No shows should be fetched")
        self.assertEqual(podcast_candidates(Fake(), [], []), [])

    def test_build_without_shows_produces_music_only(self):
        songs = [track(i) for i in range(25)]
        with patch("app.selection.music_candidates", return_value=[songs]):
            items, episodes, detail = build_items(object(), [], [], [], random.Random(1))
        self.assertEqual(episodes, [])
        self.assertTrue(items)
        self.assertLessEqual(abs(sum(item["duration_ms"] for item in items) - 90 * 60000), 2 * 60000)
        self.assertIn("podcasts 0.0%", detail)

    def test_build_records_music_and_episode_names_in_playlist_order(self):
        items = [
            {"uri": "spotify:track:1", "name": "First\nSong",
             "artists": [{"name": "Artist"}, {"name": "Guest"}]},
            {"uri": "spotify:episode:1", "name": "Podcast"},
            {"uri": "spotify:track:2", "artists": None},
            {"uri": "spotify:episode:2", "name": "News"},
        ]
        episode_uris = ["spotify:episode:1", "spotify:episode:2"]
        with patch("app.selection.build_items", return_value=(items, episode_uris, "90 minutes")):
            uris, episodes, detail = build(object(), [], [], [])
        self.assertEqual(uris, [item["uri"] for item in items])
        self.assertEqual(episodes, episode_uris)
        self.assertEqual(detail, "90 minutes; 2 music tracks\nItems:\n"
                                 "Artist, Guest - First Song\n[Episode] Podcast\n"
                                 "spotify:track:2\n[Episode] News")

    def test_build_reports_zero_music_tracks(self):
        items = [{"uri": "spotify:episode:1"}]
        with patch("app.selection.build_items", return_value=(items, ["spotify:episode:1"], "90 minutes")):
            _, _, detail = build(object(), [], [], [])
        self.assertEqual(detail, "90 minutes; 0 music tracks\nItems:\n[Episode] spotify:episode:1")

    def test_unavailable_configured_episodes_are_logged_and_skipped(self):
        class Fake:
            def get(self, path, **params):
                return {"items": [], "next": None}
        with self.assertLogs("app.selection", level="WARNING") as logs:
            self.assertEqual(podcast_candidates(Fake(), NEWS_SHOWS, []), [])
        self.assertIn("news", "\n".join(logs.output))

    def test_podcast_failure_keeps_available_episodes_and_continues_other_shows(self):
        class Fake:
            def get(self, path, **params):
                if path == "/shows/first/episodes" and params["offset"]:
                    raise RequestException("page unavailable")
                show = path.split("/")[2]
                return {"items": [{"type": "episode", "uri": f"episode:{show}",
                                   "duration_ms": 60000, "is_playable": True}],
                        "next": "next" if show == "first" else None}

        with self.assertLogs("app.selection", level="WARNING") as logs:
            result = podcast_candidates(Fake(), [{"show": "first", "count": 2}],
                                        [{"show": "second", "count": 1}])
        self.assertEqual([item["uri"] for item in result], ["episode:first", "episode:second"])
        self.assertIn("page unavailable", "\n".join(logs.output))

    def test_failed_music_sources_leave_other_sources_available(self):
        class Fake(UncachedSpotify):
            def get(self, path, **params):
                if path == "/me/top/tracks" and params["time_range"] == "short_term":
                    return {"items": [{"type": "track", "id": "unavailable"}]}
                if path == "/tracks/unavailable":
                    raise SpotifyError("track inaccessible")
                if path == "/me/top/tracks":
                    raise SpotifyRateLimitError("Retry-After: '45'")
                if path == "/me/player/recently-played":
                    raise RequestException("recent inaccessible")
                if path == "/me/top/artists":
                    raise SpotifyError("artists inaccessible")
                if path == "/shows/bad/episodes":
                    raise SpotifyError("show inaccessible")
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                if path == "/me/tracks":
                    raise SpotifyError("likes inaccessible")
                if path == "/playlists/bad/items":
                    raise SpotifyError("playlist inaccessible")
                return [{"item": {"id": str(i), "type": "track", "uri": f"spotify:track:{i}",
                                   "duration_ms": 240000, "is_playable": True}} for i in range(25)]

            def followed_artists(self, trace=None):
                raise SpotifyError("followed inaccessible")

        trace = []
        with self.assertLogs("app.selection", level="WARNING") as logs:
            items, _, _ = build_items(Fake(), ["bad", "good"], [{"show": "bad", "count": 1}],
                                      [], random.Random(1), trace)
        self.assertLessEqual(abs(sum(item["duration_ms"] for item in items) - 90 * 60000), 2 * 60000)
        messages = "\n".join(logs.output)
        for expected in ("Retry-After: '45'", "recent inaccessible", "likes inaccessible",
                         "artists inaccessible", "followed inaccessible", "track inaccessible",
                         "playlist inaccessible", "show inaccessible"):
            self.assertIn(expected, messages)
            self.assertIn(expected, "\n".join(trace))

    def test_unavailable_candidates_do_not_hide_later_playable_tracks(self):
        class Fake(UncachedSpotify):
            def get(self, path, **params):
                if path.startswith("/tracks/"):
                    raise SpotifyError("track inaccessible")
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                if path == "/me/tracks":
                    return []
                return [
                    {"item": {"id": f"bad{i}", "type": "track"}} for i in range(80)
                ] + [
                    {"item": {"id": str(i), "type": "track", "uri": f"spotify:track:{i}",
                              "duration_ms": 240000, "is_playable": True}} for i in range(25)
                ]

            def followed_artists(self):
                return []

        class NoShuffle(random.Random):
            def shuffle(self, items):
                pass

        with self.assertLogs("app.selection", level="WARNING"):
            items, _, _ = build_items(Fake(), ["source"], [], [], NoShuffle(1))
        self.assertLessEqual(abs(sum(item["duration_ms"] for item in items) - 90 * 60000), 2 * 60000)

    def test_selects_configured_show_counts_and_interleaves_categories(self):
        class Fake:
            def get(self, path, **params):
                show = path.split("/")[2]
                return {"items": [{"type": "episode", "uri": f"{show}:{i}",
                                   "duration_ms": 1000, "is_playable": True}
                                  for i in range(3)], "next": None}
        news = [{"show": "first", "count": 1}, {"show": "second", "count": 2}]
        podcasts = [{"show": "other", "count": 2}]
        episodes = podcast_candidates(Fake(), news, podcasts)
        self.assertEqual([item["uri"] for item in episodes],
                         ["first:0", "other:0", "second:0", "other:1", "second:1"])

    def test_episode_duration_over_limit_fails(self):
        podcasts = [{"uri": f"spotify:episode:{i}", "duration_ms": 60000} for i in range(99)]
        with self.assertRaises(SpotifyError):
            select_music([[track(1)], [track(2)]], podcasts, random.Random(1))

    def test_build_uses_six_episodes_and_all_music_sources(self):
        def song(number):
            return {"type": "track", "id": str(number), "uri": f"spotify:track:{number}",
                    "duration_ms": 4 * 60000, "is_playable": True}

        class Fake(UncachedSpotify):
            def get(self, path, **params):
                if path.startswith("/shows/"):
                    show = path.split("/")[2]
                    return {"items": [{"type": "episode", "uri": f"spotify:episode:{show}-{i}",
                                       "duration_ms": 7 * 60000, "is_playable": True}
                                      for i in range(4)], "next": None}
                if path == "/me/top/tracks":
                    start = 10 if params["time_range"] == "long_term" else 0
                    return {"items": [song(i) for i in range(start, start + 10)]}
                if path == "/me/player/recently-played":
                    return {"items": [{"track": song(1)}]}
                if path == "/me/top/artists":
                    return {"items": [{"id": "artist"}]}
                if path == "/artists/artist/albums":
                    return {"items": [{"id": "album", "release_date": "2026-01-01"}], "total": 1}
                if path == "/albums/album/tracks":
                    return {"items": [song(i) for i in range(20, 45)]}
                raise AssertionError(path)

            def pages(self, path, params, max_items=1000):
                if path == "/me/tracks":
                    return [{"track": song(i)} for i in range(50, 55)]
                return [{"item": song(i)} for i in range(45, 50)]

            def followed_artists(self, trace=None):
                return []

        reference, _, _ = build_items(Fake(), ["source"], NEWS_SHOWS, PODCAST_SHOWS, random.Random(7))
        items, episodes, detail = build(Fake(), ["source"], NEWS_SHOWS, PODCAST_SHOWS, random.Random(7))
        self.assertEqual(items, [item["uri"] for item in reference])
        self.assertEqual(len(episodes), 6)
        self.assertEqual(len(items), len(set(items)))
        self.assertTrue(set(episodes).issubset(items))
        self.assertIn("minutes", detail)
        self.assertIn("News show news: 4/4 requested episodes selected", detail)
        self.assertIn("Podcast show podcast1: 1/1 requested episodes selected", detail)
        self.assertIn("Selected: spotify:episode:podcast1-0.", detail)
        self.assertIn("Artists searched for discovery (not necessarily selected for the playlist): artist.", detail)
        self.assertIn("Selected music by candidate group", detail)
        self.assertIn("\nSelection:\n", detail)
        self.assertIn("\nItems:\n", detail)
        self.assertLessEqual(abs((len(items) - 6) * 4 + 42 - 90), 2)

    def test_liked_tracks_are_filtered_and_can_fill_the_mix(self):
        def song(number, playable=True):
            return {"type": "track", "id": str(number), "uri": f"spotify:track:{number}",
                    "duration_ms": 4 * 60000, "is_playable": playable}

        class Fake(UncachedSpotify):
            def get(self, path, **params):
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                self.request = (path, params, max_items)
                return ([{"track": song(i)} for i in range(30)] +
                        [{"track": song(99, False)}, {"track": None}])

            def followed_artists(self):
                return []

        api = Fake()
        groups = music_candidates(api, [], random.Random(2))
        self.assertEqual(api.request, ("/me/tracks", {"market": "NL"}, None))
        self.assertEqual(len(groups[4]), 30)

        podcasts = [{"uri": f"spotify:episode:{i}", "duration_ms": 7 * 60000} for i in range(5)]
        selected = select_music(groups, podcasts, random.Random(3))
        self.assertTrue(selected)
        self.assertTrue(all(item in groups[4] for item in selected))
        self.assertLessEqual(abs(sum(i["duration_ms"] for i in selected + podcasts) - 90 * 60000), 2 * 60000)

    def test_collaborator_releases_are_playable_candidates_without_recursive_search(self):
        def song(number, artists, playable=True):
            return {"type": "track", "id": str(number), "uri": f"spotify:track:{number}",
                    "duration_ms": 4 * 60000, "is_playable": playable,
                    "artists": [{"id": artist} for artist in artists]}

        class Fake(UncachedSpotify):
            def __init__(self):
                self.requested_artists = []
                self.top_artist_params = None

            def get(self, path, **params):
                if path == "/me/top/artists":
                    self.top_artist_params = params
                    return {"items": [{"id": "favorite"}, {"id": "other-favorite"}]}
                if path.startswith("/artists/"):
                    artist = path.split("/")[2]
                    self.requested_artists.append((artist, params["include_groups"]))
                    return {"items": [{"id": artist, "release_date": "2026-01-01"}], "total": 1}
                if path == "/albums/favorite/tracks":
                    duet = song("duet", ["favorite", "collaborator", "other-favorite"])
                    duet["artists"][1]["name"] = "Guest Artist"
                    return {"items": [duet,
                                      song("duet-again", ["favorite", "collaborator"]),
                                      song("unrelated", ["not-a-collaborator"])]}
                if path == "/albums/other-favorite/tracks":
                    return {"items": []}
                if path == "/albums/collaborator/tracks":
                    return {"items": [song(i, ["collaborator", "second-degree"]) for i in range(25)]
                            + [song("unplayable", ["collaborator"], False),
                               song("unrelated-album-track", ["someone-else"])]}
                if path.startswith("/albums/"):
                    raise AssertionError(path)
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self, trace=None):
                return []

        api = Fake()
        trace = []
        groups = music_candidates(api, [], random.Random(2), trace=trace)
        self.assertIn(api.top_artist_params["time_range"], ("short_term", "medium_term", "long_term"))
        self.assertEqual(api.top_artist_params["limit"], 50)
        self.assertCountEqual(api.requested_artists, [
            ("favorite", "album,single,appears_on"),
            ("other-favorite", "album,single,appears_on"),
            ("collaborator", "album,single")])
        self.assertIn("Collaborators searched: Guest Artist (collaborator)", "\n".join(trace))
        self.assertEqual(len(groups[-1]), 25)
        self.assertTrue(all(item["is_playable"] for item in groups[-1]))

        selected = select_music([groups[-1]], [], random.Random(1))
        self.assertLessEqual(abs(sum(item["duration_ms"] for item in selected) - 90 * 60000), 2 * 60000)

    def test_number_of_explored_collaborators_is_bounded(self):
        class Fake(UncachedSpotify):
            def __init__(self):
                self.collaborators = []

            def get(self, path, **params):
                if path == "/me/top/artists":
                    return {"items": [{"id": "favorite"}]}
                if path == "/artists/favorite/albums":
                    return {"items": [{"id": "favorite-album"}], "total": 1}
                if path == "/albums/favorite-album/tracks":
                    return {"items": [{"artists": [{"id": "favorite"}, {"id": f"collab-{i}"}]}
                                      for i in range(10)]}
                if path.startswith("/artists/"):
                    self.collaborators.append(path.split("/")[2])
                    return {"items": [], "total": 0}
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self):
                return []

        api = Fake()
        music_candidates(api, [], random.Random(1))
        self.assertEqual(len(api.collaborators), 4)
        self.assertEqual(len(set(api.collaborators)), 4)

    def test_artist_release_selection_includes_catalog_beyond_first_page(self):
        class Fake(UncachedSpotify):
            def __init__(self):
                self.release_requests = []
                self.selected_albums = []

            def get(self, path, **params):
                if path == "/me/top/artists":
                    return {"items": [{"id": "artist"}]}
                if path == "/artists/artist/albums":
                    self.release_requests.append(params)
                    offset = params.get("offset", 0)
                    albums = [{"id": str(i), "release_date": "2026-01-01" if i < 10 else "1990-01-01"}
                              for i in range(offset, min(offset + params["limit"], 20))]
                    return {"items": albums, "total": 20}
                if path.startswith("/albums/"):
                    self.selected_albums.append(path.split("/")[2])
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self):
                return []

        class FixedRandom(random.Random):
            def sample(self, population, k):
                if len(population) == 20 and k == 2:
                    return [12, 2]
                return super().sample(population, k)

        api = Fake()
        music_candidates(api, [], FixedRandom(1))
        self.assertEqual(api.selected_albums, ["12", "2"])
        self.assertEqual(api.release_requests, [
            {"market": "NL", "include_groups": "album,single,appears_on", "limit": 10},
            {"market": "NL", "include_groups": "album,single,appears_on", "limit": 1, "offset": 12},
        ])

    def test_artist_and_release_errors_do_not_block_other_music(self):
        class Fake(UncachedSpotify):
            def get(self, path, **params):
                if path == "/me/top/artists":
                    return {"items": [{"id": "good"}, {"id": "bad"}]}
                if path == "/artists/bad/albums":
                    raise SpotifyRateLimitError("Retry-After: '45'")
                if path == "/artists/collaborator/albums":
                    raise RequestException("connection failed")
                if path == "/artists/good/albums":
                    return {"total": 2, "items": [{"id": "good"}, {"id": "bad"}]}
                if path == "/albums/bad/tracks":
                    raise SpotifyError("release unavailable")
                if path == "/albums/good/tracks":
                    return {"items": [
                        {"id": str(i), "type": "track", "uri": f"spotify:track:{i}",
                         "duration_ms": 240000, "is_playable": True,
                         "artists": [{"id": "good"}, {"id": "collaborator"}]}
                        for i in range(25)
                    ] + [{"id": "unavailable", "type": "track", "artists": [{"id": "good"}]}]}
                if path == "/tracks/unavailable":
                    raise SpotifyError("track unavailable")
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self):
                return []

        with self.assertLogs("app.selection", level="WARNING") as logs:
            items, _, _ = build_items(Fake(), [], [], [], random.Random(1))
        self.assertGreaterEqual(sum(item["duration_ms"] for item in items), 88 * 60000)
        self.assertLessEqual(sum(item["duration_ms"] for item in items), 92 * 60000)
        messages = "\n".join(logs.output)
        for expected in ("bad", "Retry-After: '45'", "release unavailable", "collaborator",
                         "connection failed", "track unavailable"):
            self.assertIn(expected, messages)

    def test_artist_failures_still_fail_when_no_music_can_be_selected(self):
        class Fake(UncachedSpotify):
            def get(self, path, **params):
                if path == "/me/top/artists":
                    return {"items": [{"id": "bad"}]}
                if path == "/artists/bad/albums":
                    raise SpotifyError("artist unavailable")
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self):
                return []

        with self.assertLogs("app.selection", level="WARNING"):
            with self.assertRaisesRegex(SpotifyError, "No playable music found"):
                build_items(Fake(), [], [], [], random.Random(1))

    def test_artist_failures_still_fail_when_music_is_under_duration(self):
        class Fake(UncachedSpotify):
            def get(self, path, **params):
                if path == "/me/top/tracks":
                    if params["time_range"] == "short_term":
                        return {"items": [
                            {"id": str(i), "type": "track", "uri": f"spotify:track:{i}",
                             "duration_ms": 240000, "is_playable": True}
                            for i in range(20)
                        ]}
                    return {"items": []}
                if path == "/me/top/artists":
                    return {"items": [{"id": "bad"}]}
                if path == "/artists/bad/albums":
                    return {"total": 2, "items": []}
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self):
                return []

        with self.assertLogs("app.selection", level="WARNING") as logs:
            with self.assertRaisesRegex(SpotifyError, "88-92 minutes"):
                build_items(Fake(), [], [], [], random.Random(1))
        self.assertIn("inconsistent artist release count", "\n".join(logs.output))

    def test_artist_range_is_chosen_per_refresh_and_limited_to_one_page(self):
        class Fake(UncachedSpotify):
            def __init__(self):
                self.top_requests = []
                self.selected = []

            def get(self, path, **params):
                if path == "/me/top/artists":
                    self.top_requests.append(params)
                    return {"items": [{"id": str(i)} for i in range(50)], "next": "next"}
                if path.startswith("/artists/"):
                    self.selected.append(int(path.split("/")[2]))
                    return {"items": [], "total": 0}
                return {"items": [], "next": None}

            def pages(self, path, params, max_items=1000):
                if path == "/me/top/artists":
                    raise AssertionError("Top artists must not be paginated")
                return []

            def followed_artists(self):
                return []

        class FixedRange(random.Random):
            def __init__(self, value):
                super().__init__(5)
                self.value = value

            def choice(self, options):
                if self.value not in options:
                    raise AssertionError(options)
                return self.value

        for time_range in ("short_term", "medium_term", "long_term"):
            with self.subTest(time_range=time_range):
                api = Fake()
                music_candidates(api, [], FixedRange(time_range))
                self.assertEqual(api.top_requests, [{"time_range": time_range, "limit": 50}])
                self.assertEqual(len(api.selected), 8)
                self.assertTrue(all(0 <= artist < 50 for artist in api.selected))

    def test_followed_artists_are_deduplicated_and_their_tracks_are_candidates(self):
        class Fake(UncachedSpotify):
            def __init__(self):
                self.selected = []

            def get(self, path, **params):
                if path == "/me/top/artists":
                    return {"items": [{"id": "both"}]}
                if path.startswith("/artists/"):
                    artist = path.split("/")[2]
                    self.selected.append(artist)
                    return {"items": [{"id": artist}], "total": 1}
                if path == "/albums/followed/tracks":
                    return {"items": [{"id": "song", "type": "track", "uri": "spotify:track:song",
                                       "is_playable": True, "duration_ms": 180000}]}
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self):
                return [{"id": "both"}, {"id": "followed"}]

        api = Fake()
        groups = music_candidates(api, [], random.Random(1))
        self.assertCountEqual(api.selected, ["both", "followed"])
        self.assertEqual([item["id"] for item in groups[2]], ["song"])

    def test_artists_from_all_track_sources_join_the_deduplicated_pool(self):
        class Fake(UncachedSpotify):
            def __init__(self):
                self.selected = []

            def get(self, path, **params):
                if path == "/me/top/artists":
                    return {"items": [{"id": "top"}]}
                if path == "/me/top/tracks":
                    term = params["time_range"]
                    return {"items": [{"id": "same" if term in ("short_term", "long_term") else "medium",
                                       "artists": [{"id": term}]}]}
                if path == "/me/player/recently-played":
                    return {"items": [{"track": {"id": "recent", "artists": [{"id": "recent"}]}}]}
                if path.startswith("/artists/"):
                    artist = path.split("/")[2]
                    self.selected.append(artist)
                    return {"items": [{"id": artist}], "total": 1}
                if path == "/albums/liked/tracks":
                    return {"items": [{"id": "discovery", "type": "track", "uri": "spotify:track:discovery",
                                       "is_playable": True, "duration_ms": 180000}]}
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return ([{"track": {"artists": []}} for _ in range(81)] +
                        [{"track": {"artists": [{"id": "liked"}, {"id": "duet"}, {"id": "followed"},
                                                {"id": "top"}, {"id": "liked"}, {}]}},
                         {"track": None}, {"track": {"artists": None}}])

            def followed_artists(self):
                return [{"id": "followed"}]

        api = Fake()
        groups = music_candidates(api, [], random.Random(1))
        self.assertCountEqual(api.selected, [
            "top", "followed", "liked", "duet", "short_term", "medium_term", "long_term", "recent",
        ])
        self.assertEqual([item["id"] for item in groups[2]], ["discovery"])

    def test_artists_from_configured_playlists_join_the_deduplicated_pool(self):
        class Fake(UncachedSpotify):
            def __init__(self):
                self.selected = []

            def get(self, path, **params):
                if path.startswith("/artists/"):
                    self.selected.append(path.split("/")[2])
                    return {"items": [], "total": 0}
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                if path == "/playlists/source/items":
                    return [{"item": {
                        "id": "configured-track", "type": "track", "uri": "spotify:track:configured-track",
                        "duration_ms": 180000, "is_playable": True,
                        "artists": [{"id": "configured-artist"}],
                    }}]
                return []

            def followed_artists(self):
                return []

        api = Fake()
        groups = music_candidates(api, ["source"], random.Random(1))

        self.assertEqual(api.selected, ["configured-artist"])
        self.assertEqual([item["id"] for item in groups[3]], ["configured-track"])

    def test_music_selection_reuses_liked_tracks_and_followed_artists(self):
        with tempfile.TemporaryDirectory() as directory:
            class Fake(Spotify):
                def __init__(self):
                    self.store = Store(os.path.join(directory, "cache.sqlite"))
                    self.calls = []

                def get(self, path, **params):
                    self.calls.append(path)
                    if path == "/me/tracks":
                        return {"items": [{"track": {
                            "id": "liked", "type": "track", "uri": "spotify:track:liked",
                            "is_playable": True, "duration_ms": 180000,
                        }}], "next": None, "total": 1}
                    if path == "/me/following":
                        return {"artists": {"items": [{"id": "followed"}], "next": None}}
                    if path.startswith("/artists/"):
                        return {"items": [], "total": 0}
                    return {"items": []}

            api = Fake()
            traces = []
            for seed in (1, 2):
                trace = []
                groups = music_candidates(api, [], random.Random(seed), trace=trace)
                traces.append(trace)
                self.assertEqual([item["id"] for item in groups[4]], ["liked"])
            self.assertIn("Liked tracks: retrieved from Spotify and saved in today's cache.", traces[0])
            self.assertIn("Followed artists: 0 reused from cache; 1 retrieved from Spotify during this refresh.",
                          traces[0])
            self.assertIn("Liked tracks: retrieved from today's cache.", traces[1])
            self.assertIn("Followed artists: 1 reused from cache; 0 retrieved from Spotify during this refresh.",
                          traces[1])
            self.assertEqual(api.calls.count("/me/tracks"), 1)
            self.assertEqual(api.calls.count("/me/following"), 1)
            self.assertEqual(api.calls.count("/artists/followed/albums"), 2)

            api.store.set_cached("liked_tracks_cache", datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat(),
                                 [{"track": {"id": "old", "type": "track", "uri": "spotify:track:old",
                                             "is_playable": True, "duration_ms": 180000}}])
            groups = music_candidates(api, [], random.Random(3))
            self.assertEqual([item["id"] for item in groups[4]], ["old"])
            self.assertEqual(api.calls.count("/me/tracks"), 1)

            episode = {"uri": "spotify:episode:test", "duration_ms": 89 * 60000}
            with patch("app.selection.podcast_candidates", return_value=[episode]):
                uris, episodes, detail = build(api, [], [], [], random.Random(4))
            history_id = api.store.start()
            api.store.prepare(history_id, "target", [], uris, episodes, detail)
            api.store.resolve(True)
            saved = api.store.history()[0]["detail"]
            self.assertIn("Liked tracks: retrieved from today's cache.", saved)
            self.assertIn("Followed artists: 1 reused from cache; 0 retrieved from Spotify during this refresh.", saved)


class SpotifyTests(unittest.TestCase):
    def test_playlist_url_validation(self):
        pid = "0123456789ABCDEFGHIJKL"
        self.assertEqual(playlist_id("https://open.spotify.com/playlist/" + pid + "?si=test"), pid)
        for value in ("https://evil.example/playlist/" + pid, "https://open.spotify.com/track/" + pid):
            with self.assertRaises(ValueError):
                playlist_id(value)

    def test_show_url_validation(self):
        sid = "0123456789ABCDEFGHIJKL"
        self.assertEqual(show_id("https://open.spotify.com/show/" + sid + "?si=test"), sid)
        self.assertEqual(show_id("spotify:show:" + sid), sid)
        with self.assertRaises(ValueError):
            show_id("https://open.spotify.com/playlist/" + sid)

    def test_pkce_state_and_callback(self):
        url, verifier, state = authorization("client")
        query = parse_qs(urlparse(url).query)
        self.assertGreaterEqual(len(verifier), 43)
        self.assertEqual(query["state"], [state])
        self.assertIn("user-library-read", query["scope"][0].split())
        self.assertIn("user-follow-read", query["scope"][0].split())
        self.assertEqual(callback_code("http://127.0.0.1:8888/callback?code=abc&state=" + state, state), "abc")
        with self.assertRaises(ValueError):
            callback_code("http://127.0.0.1:8888/callback?code=abc&state=wrong", state)

    def test_liked_tracks_pagination_accepts_more_than_1000(self):
        api = Spotify.__new__(Spotify)

        def get(path, **params):
            offset = params["offset"]
            return {"items": list(range(offset, offset + 50)),
                    "next": "next" if offset < 1000 else None}

        api.get = get
        self.assertEqual(len(api.pages("/me/tracks", max_items=None)), 1050)
        with self.assertRaises(SpotifyError):
            api.pages("/me/tracks")

    def test_liked_track_sample_selects_up_to_five_distinct_random_pages(self):
        for total in (0, 1, 50, 51, 251, 1000):
            with self.subTest(total=total):
                api = Spotify.__new__(Spotify)
                requests_seen = []

                def get(path, **params):
                    requests_seen.append((path, params))
                    start = params["offset"]
                    return {"total": total, "items": list(range(start, min(start + params["limit"], total)))}

                api.get = get
                result = api.liked_tracks(random.Random(11))
                self.assertEqual(requests_seen[0], ("/me/tracks", {"market": "NL", "limit": 1, "offset": 0}))
                if total <= 1:
                    self.assertEqual(result, list(range(total)))
                    self.assertEqual(len(requests_seen), 1)
                    continue
                page_count = (total + 49) // 50
                expected_pages = random.Random(11).sample(range(page_count), min(5, page_count))
                self.assertEqual([params["offset"] for _, params in requests_seen[1:]],
                                 [page * 50 for page in expected_pages])
                self.assertTrue(all(path == "/me/tracks" and params["market"] == "NL" and params["limit"] == 50
                                    for path, params in requests_seen[1:]))
                self.assertEqual(result, [item for page in expected_pages
                                          for item in range(page * 50, min((page + 1) * 50, total))])
                self.assertLessEqual(len(result), 250)
                self.assertLessEqual(len(requests_seen), 6)

    def test_sample_is_cached_for_the_day_and_refreshed_the_next_day(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            api.get = Mock(side_effect=lambda path, **params: {
                "total": 1000,
                "items": list(range(params["offset"], params["offset"] + params["limit"])),
            })
            with patch("app.spotify.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, 15, tzinfo=timezone.utc)
                first = api.daily_cached("liked_tracks_cache",
                                         lambda: api.liked_tracks(random.Random(1)))
                same_day = api.daily_cached("liked_tracks_cache",
                                            lambda: api.liked_tracks(random.Random(2)))
                self.assertEqual(first, same_day)
                self.assertEqual(api.get.call_count, 6)

                clock.now.return_value = datetime(2026, 1, 16, tzinfo=timezone.utc)
                next_day = api.daily_cached("liked_tracks_cache",
                                            lambda: api.liked_tracks(random.Random(2)))
                self.assertNotEqual(first, next_day)
                self.assertEqual(api.get.call_count, 12)

    def test_failed_sample_does_not_cache_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            api.get = Mock(side_effect=[
                {"total": 1000, "items": [0]},
                {"items": list(range(50))},
                SpotifyError("unavailable"),
            ])
            with patch("app.spotify.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, 15, tzinfo=timezone.utc)
                with self.assertRaisesRegex(SpotifyError, "unavailable"):
                    api.daily_cached("liked_tracks_cache",
                                     lambda: api.liked_tracks(random.Random(1)))
            self.assertIsNone(api.store.get_cached("liked_tracks_cache", "2026-01-15"))

    def test_inconsistent_liked_track_response_fails(self):
        api = Spotify.__new__(Spotify)
        api.get = Mock(return_value={"total": 1, "items": []})
        with self.assertRaisesRegex(SpotifyError, "inconsistent"):
            api.liked_tracks(random.Random(1))
        api.get = Mock(side_effect=[{"total": 100, "items": [0]}, {"items": []}])
        with self.assertRaisesRegex(SpotifyError, "sampled liked-track page"):
            api.liked_tracks(random.Random(1))

    def test_partial_sources_are_used_without_caching_and_target_reads_remain_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            day = datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
            partial = [{"track": {"id": "first"}}]
            cases = (
                ("liked_tracks_cache", [
                    {"total": 100, "items": [partial[0]]},
                    {"items": partial},
                    SpotifyError("saved page failed"),
                ], lambda: api.daily_cached("liked_tracks_cache", lambda: api.liked_tracks(random.Random(1)))),
            )
            for key, responses, fetch in cases:
                with self.subTest(key=key):
                    api.get = Mock(side_effect=responses)
                    with self.assertLogs("app.selection", level="WARNING") as logs:
                        result = get_api_items(key, fetch)
                    self.assertEqual(len(result), 1)
                    self.assertIn("page failed", "\n".join(logs.output))
                    self.assertIsNone(api.store.get_cached(key, day))

            # Partial source playlists are usable, but the target must be read completely.
            first_page = {"items": [{"item": {"uri": "spotify:track:1"}}], "next": "next"}
            api.get = Mock(side_effect=[first_page, SpotifyError("playlist page failed")])
            with self.assertLogs("app.selection", level="WARNING"):
                result = get_api_items("source playlist", lambda: api.pages("/playlists/source/items"))
            self.assertEqual(result, first_page["items"])

            api.get = Mock(side_effect=[first_page, SpotifyError("playlist page failed")])
            with self.assertRaisesRegex(SpotifyError, "playlist page failed"):
                api.playlist_items("target")

    def test_daily_liked_track_cache_survives_restart_and_expires_at_amsterdam_midnight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cache.sqlite")
            api = Spotify.__new__(Spotify)
            api.store = Store(path)
            requests_seen = []

            def get(endpoint, **params):
                requests_seen.append((endpoint, params))
                if endpoint == "/me/tracks":
                    return {"items": [{"track": {"id": "saved"}}], "next": None}
                raise AssertionError(endpoint)

            api.get = get
            for utc_time, expected_calls in (
                (datetime(2026, 1, 15, 22, 30, tzinfo=timezone.utc), 1),
                (datetime(2026, 1, 15, 22, 59, tzinfo=timezone.utc), 1),
                (datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc), 2),
            ):
                with patch("app.spotify.datetime") as clock:
                    clock.now.side_effect = lambda tz: utc_time.astimezone(tz)
                    api.store = Store(path)
                    saved = api.daily_cached(
                        "liked_tracks_cache", lambda: api.pages("/me/tracks", {"market": "NL"}, max_items=None)
                    )
                self.assertEqual(saved, [{"track": {"id": "saved"}}])
                self.assertEqual(len(requests_seen), expected_calls)

    def test_empty_daily_cache_is_reused_and_summer_midnight_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            fetch = Mock(return_value=[])
            trace = []
            with patch("app.spotify.datetime") as clock:
                for utc_time in (datetime(2026, 7, 15, 21, 59, tzinfo=timezone.utc),
                                 datetime(2026, 7, 15, 21, 59, tzinfo=timezone.utc),
                                 datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc)):
                    clock.now.side_effect = lambda tz: utc_time.astimezone(tz)
                    self.assertEqual(api.daily_cached("liked_tracks_cache", fetch, trace), [])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(trace[1], "Liked tracks: retrieved from today's cache.")

    def test_failed_or_incomplete_fetch_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            with patch("app.spotify.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, 15, tzinfo=timezone.utc)
                with patch.object(api, "get", side_effect=[
                    {"items": [{"track": {"id": "first"}}], "next": "next"},
                    SpotifyError("unavailable"),
                    {"items": [{"track": {"id": "complete"}}], "next": None},
                ]) as get:
                    fetch = lambda: api.pages("/me/tracks", {"market": "NL"}, max_items=None)
                    with self.assertRaises(SpotifyError):
                        api.daily_cached("liked_tracks_cache", fetch)
                    self.assertEqual(api.daily_cached("liked_tracks_cache", fetch), [{"track": {"id": "complete"}}])
                    self.assertEqual(get.call_count, 3)

    def test_empty_intermediate_page_fails_instead_of_caching_partial_results(self):
        api = Spotify.__new__(Spotify)
        api.get = lambda *args, **kwargs: {"items": [], "next": "next"}
        with self.assertRaisesRegex(SpotifyError, "Empty page"):
            api.pages("/me/tracks")

    def test_refresh_token_rotation_preserves_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(os.path.join(directory, "cache.sqlite"))
            store.set_many({"client_id": "client", "refresh_token": "old",
                            "followed_artists_cache": {"artists": {"artist": {"id": "artist"}}}})
            store.set_cached("liked_tracks_cache", "2026-01-01", [])
            with patch("app.spotify.token_request", return_value={"access_token": "access", "refresh_token": "new"}):
                Spotify(store)
            self.assertEqual(store.get("refresh_token"), "new")
            self.assertEqual(store.get_cached("liked_tracks_cache", "2026-01-01"), [])
            self.assertEqual(store.get("followed_artists_cache"), {"artists": {"artist": {"id": "artist"}}})

    def test_followed_artists_use_cursor_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cache.sqlite")
            api = Spotify.__new__(Spotify)
            api.store = Store(path)
            requests_seen = []

            def get(endpoint, **params):
                requests_seen.append((endpoint, params))
                number = int(params.get("after", "0")) + 1
                return {"artists": {"items": [{"id": str(number)}],
                                    "next": "next" if number < 7 else None,
                                    "cursors": {"after": str(number)}}}

            api.get = get
            with patch("app.spotify.datetime") as clock:
                # The first day stops at five requests, even after reopening SQLite.
                clock.now.return_value = datetime(2026, 1, 15, tzinfo=timezone.utc)
                self.assertEqual(len(api.followed_artists()), 5)
                api.store = Store(path)
                self.assertEqual(len(api.followed_artists()), 5)
                self.assertEqual(len(requests_seen), 5)
                # The next day resumes after the fifth page and stops at the end.
                clock.now.return_value = datetime(2026, 1, 16, tzinfo=timezone.utc)
                self.assertEqual(len(api.followed_artists()), 7)
                self.assertEqual(requests_seen[5], (
                    "/me/following", {"type": "artist", "limit": 50, "after": "5"}
                ))
                self.assertEqual(len(requests_seen), 7)
                self.assertEqual(len(api.followed_artists()), 7)
                self.assertEqual(len(requests_seen), 7)

    def test_followed_artists_remove_unfollowed_only_after_a_complete_rescan(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            api.get = Mock(return_value={"artists": {
                "items": [{"id": "old"}, {"id": "kept"}], "next": None
            }})
            with patch("app.spotify.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, 15, tzinfo=timezone.utc)
                self.assertEqual(len(api.followed_artists()), 2)
                clock.now.return_value = datetime(2026, 1, 16, tzinfo=timezone.utc)
                # An interrupted rescan must not remove artists from the old cache.
                api.get.side_effect = [
                    {"artists": {"items": [{"id": "kept"}], "next": "next", "cursors": {"after": "kept"}}},
                    SpotifyError("unavailable"),
                    {"artists": {"items": [{"id": "new"}], "next": None}},
                ]
                trace = []
                with self.assertLogs("app.selection", level="WARNING"):
                    partial = get_api_items("followed artists", lambda: api.followed_artists(trace))
                self.assertEqual({artist["id"] for artist in partial}, {"old", "kept"})
                self.assertIn("Followed artists: 1 reused from cache; 1 retrieved from Spotify during this refresh.",
                              trace)
                result = api.followed_artists()
                self.assertEqual({artist["id"] for artist in result}, {"kept", "new"})

    def test_followed_artist_failed_requests_count_towards_daily_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            api.get = Mock(side_effect=SpotifyError("unavailable"))
            with patch("app.spotify.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, 15, tzinfo=timezone.utc)
                for _ in range(5):
                    with self.assertRaisesRegex(SpotifyError, "unavailable"):
                        api.followed_artists()
                with self.assertRaisesRegex(SpotifyError, "unavailable"):
                    api.followed_artists()
                self.assertEqual(api.get.call_count, 5)
                clock.now.return_value = datetime(2026, 1, 16, tzinfo=timezone.utc)
                with self.assertRaisesRegex(SpotifyError, "unavailable"):
                    api.followed_artists()
                self.assertEqual(api.get.call_count, 6)

    def test_followed_artist_daily_limit_marks_retry_after_as_previous_error(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            pages = [
                {"artists": {"items": [{"id": str(i)}], "next": "next", "cursors": {"after": str(i)}}}
                for i in range(1, 5)
            ]
            api.get = Mock(side_effect=pages + [
                SpotifyRateLimitError("Spotify GET /me/following failed (HTTP 429); Retry-After: '172'"),
                {"artists": {"items": [{"id": "5"}], "next": None}},
            ])
            with patch("app.spotify.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
                with self.assertRaisesRegex(SpotifyError, "Retry-After: '172'"):
                    api.followed_artists()
                original_error = api.store.get("followed_artists_cache")["last_error"]

                clock.now.return_value = datetime(2026, 1, 15, 10, 5, tzinfo=timezone.utc)
                for _ in range(2):
                    trace = []
                    with self.assertLogs("app.selection", level="WARNING") as logs:
                        artists = get_api_items("followed artists", lambda: api.followed_artists(trace))
                    self.assertEqual(len(artists), 4)
                    self.assertIn("Followed artists: 4 reused from cache; 0 retrieved from Spotify during this refresh.",
                                  trace)
                    self.assertIn("daily limit of 5 requests reached; no new Spotify request", logs.output[0])
                    self.assertIn("previous error", logs.output[0])
                    self.assertIn("Retry-After: '172'", logs.output[0])
                    self.assertEqual(api.store.get("followed_artists_cache")["last_error"], original_error)
                self.assertEqual(api.get.call_count, 5)

                clock.now.return_value = datetime(2026, 1, 16, 10, 0, tzinfo=timezone.utc)
                self.assertEqual(len(api.followed_artists()), 5)
                self.assertEqual(api.get.call_count, 6)

    def test_invalid_followed_artist_cursor_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            api.get = lambda *args, **kwargs: {
                "artists": {"items": [{"id": "first"}], "next": "next", "cursors": {"after": "first"}}
            }
            with self.assertRaisesRegex(SpotifyError, "pagination cursor"):
                api.followed_artists()

    def test_missing_follow_permission_gives_reconnect_guidance(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            api.token = "token"
            with patch("app.spotify.requests.request") as request:
                request.return_value.ok = False
                request.return_value.status_code = 403
                with self.assertRaisesRegex(SpotifyError, "reconnect Spotify.*followed artists"):
                    api.followed_artists()

    def test_missing_library_permission_gives_reconnect_guidance(self):
        api = Spotify.__new__(Spotify)
        api.token = "token"
        with patch("app.spotify.requests.request") as request:
            request.return_value.ok = False
            request.return_value.status_code = 403
            with self.assertRaisesRegex(SpotifyError, "reconnect Spotify"):
                api.get("/me/tracks")

    def test_rate_limit_reports_retry_after_or_its_absence(self):
        api = Spotify.__new__(Spotify)
        api.token = "token"
        with patch("app.spotify.requests.request") as request:
            request.return_value.ok = False
            request.return_value.status_code = 429
            for headers, message in (({"Retry-After": "42"}, "Retry-After: '42'"),
                                     ({}, "Retry-After not provided")):
                with self.subTest(headers=headers):
                    request.return_value.headers = headers
                    with self.assertRaises(SpotifyError) as error:
                        api.get("/me/top/artists")
                    self.assertIn(message, str(error.exception))

    def test_token_rate_limit_reports_retry_after(self):
        with patch("app.spotify.requests.post") as post:
            post.return_value.ok = False
            post.return_value.status_code = 429
            post.return_value.headers = {"Retry-After": "30"}
            with self.assertRaisesRegex(SpotifyError, "Retry-After: '30'"):
                token_request({})


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(os.path.join(self.temp.name, "db.sqlite"))
        self.store.set_many({"playlist_id": "target", "source_ids": []})
        self.service = Refresh(self.store)

    def test_success_updates_history_and_used_episodes(self):
        class Fake:
            def playlist_items(self, pid):
                return ["old"]

            def replace(self, pid, items):
                self.items = items

        api = Fake()
        with patch("app.refresh.Spotify", return_value=api), patch(
                "app.refresh.build", return_value=(["new"], ["episode"], "120 minutes")):
            self.service.run("2026-01-01 08:00")
            self.service.run("2026-01-01 08:00")

        self.assertEqual(api.items, ["new"])
        self.assertEqual(self.store.history()[0]["status"], "succeeded")
        self.assertEqual(self.store.used_episodes(), {"episode"})
        self.assertIsNone(self.store.pending())
        self.assertEqual(len(self.store.history()), 1)

    def test_success_persists_selected_music_in_history(self):
        items = [{"uri": "spotify:track:1", "name": "Song", "artists": [{"name": "Artist"}]},
                 {"uri": "spotify:episode:1", "name": "Episode"}]

        def fake_build_items(api, playlists, news, podcasts, rng, trace):
            trace.append("News show: one episode selected.")
            return items, ["spotify:episode:1"], "90 minutes"

        with patch("app.refresh.Spotify") as spotify, patch(
                "app.selection.build_items", side_effect=fake_build_items):
            spotify.return_value.playlist_items.return_value = ["old"]
            self.service.run()
        spotify.return_value.replace.assert_called_once_with("target", [item["uri"] for item in items])
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "succeeded")
        self.assertEqual(entry["detail"], "90 minutes; 1 music tracks\nSelection:\n"
                                          "News show: one episode selected.\nItems:\n"
                                          "Artist - Song\n[Episode] Episode")

    def test_refresh_passes_configured_shows_to_selection(self):
        self.store.set_many({"news_shows": NEWS_SHOWS, "podcast_shows": PODCAST_SHOWS})
        with patch("app.refresh.Spotify") as spotify, patch(
                "app.refresh.build", return_value=(["new"], [], "detail")) as build_mock:
            spotify.return_value.playlist_items.return_value = ["old"]
            self.service.run()
        build_mock.assert_called_once_with(spotify.return_value, [], NEWS_SHOWS, PODCAST_SHOWS)

    def test_uncertain_write_is_reconciled_without_another_write(self):
        class Fake:
            def __init__(self):
                self.calls = 0

            def playlist_items(self, pid):
                self.calls += 1
                if self.calls == 1:
                    return ["old"]
                raise SpotifyError("unreachable")

            def replace(self, pid, items):
                raise TimeoutError("timeout")

        with patch("app.refresh.Spotify", return_value=Fake()), patch(
                "app.refresh.build", return_value=(["new"], ["episode"], "120 minutes")):
            with self.assertRaises(SpotifyError):
                self.service.run()
        self.assertEqual(self.store.history()[0]["status"], "uncertain")
        self.assertIsNotNone(self.store.pending())

        # Once Spotify is reachable again, compare its playlist with the saved snapshots.
        class Recovered:
            def playlist_items(self, pid):
                return ["new"]

        self.assertTrue(self.service.reconcile(Recovered()))
        self.assertEqual(self.store.history()[0]["status"], "succeeded")
        self.assertEqual(self.store.used_episodes(), {"episode"})

    def test_reconciled_history_keeps_music_list_and_repeated_episode_note(self):
        episode = "spotify:episode:1"
        self.store.prepare(self.store.start(), "target", [], [episode], [episode], "old")
        self.store.resolve(True)
        items = [{"uri": "spotify:track:1", "name": "Song", "artists": [{"name": "Artist"}]},
                 {"uri": episode}]

        class Fake:
            def __init__(self):
                self.calls = 0

            def playlist_items(self, pid):
                self.calls += 1
                if self.calls == 1:
                    return ["old"]
                raise SpotifyError("unreachable")

            def replace(self, pid, uris):
                raise TimeoutError("timeout")

        with patch("app.refresh.Spotify", return_value=Fake()), patch(
                "app.selection.build_items", return_value=(items, [episode], "90 minutes")):
            with self.assertRaises(SpotifyError):
                self.service.run()

        class Recovered:
            def playlist_items(self, pid):
                return [item["uri"] for item in items]

        self.assertTrue(self.service.reconcile(Recovered()))
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "succeeded")
        self.assertIn("Confirmed afterwards; 1 latest episodes used previously; 90 minutes; 1 music tracks",
                      entry["detail"])
        self.assertTrue(entry["detail"].endswith(
            "\nItems:\nArtist - Song\n[Episode] spotify:episode:1"))

    def test_failed_preparation_does_not_replace(self):
        class Fake:
            def playlist_items(self, pid):
                return ["old"]

            def replace(self, pid, items):
                raise AssertionError("should not write")

        with patch("app.refresh.Spotify", return_value=Fake()), patch(
                "app.refresh.build", side_effect=SpotifyError("not enough music")):
            with self.assertRaises(SpotifyError):
                self.service.run()
        self.assertIsNone(self.store.pending())
        self.assertEqual(self.store.history()[0]["status"], "failed")

    def test_rate_limit_is_recorded_in_history_without_replacing(self):
        api = Spotify.__new__(Spotify)
        api.token = "token"
        with patch("app.refresh.Spotify", return_value=api), patch.object(
                api, "playlist_items", return_value=["old"]), patch(
                "app.refresh.build", side_effect=lambda *args: api.get("/me/top/artists")), patch(
                "app.spotify.requests.request") as request:
            request.return_value.ok = False
            request.return_value.status_code = 429
            request.return_value.headers = {"Retry-After": "45"}
            with self.assertRaises(SpotifyError):
                self.service.run()
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("Retry-After: '45'", entry["detail"])
        self.assertIn("/me/top/artists", entry["detail"])
        self.assertIsNone(self.store.pending())
        self.assertEqual(request.call_args.args[0], "GET")

    def test_skipped_rate_limit_is_recorded_if_selection_fails(self):
        class Fake(UncachedSpotify):
            def playlist_items(self, pid):
                return ["old"]

            def get(self, path, **params):
                if path == "/me/top/tracks":
                    raise SpotifyRateLimitError("Retry-After: '45'")
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self, trace=None):
                return []

            def replace(self, pid, items):
                raise AssertionError("should not write")

        with patch("app.refresh.Spotify", return_value=Fake()), self.assertLogs("app", level="WARNING"):
            with self.assertRaises(SpotifyError):
                self.service.run()
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("Retry-After: '45'", entry["detail"])
        self.assertIsNone(self.store.pending())

    def test_write_rate_limit_is_recorded_if_old_playlist_is_confirmed(self):
        api = Spotify.__new__(Spotify)
        api.token = "token"
        with patch("app.refresh.Spotify", return_value=api), patch.object(
                api, "playlist_items", return_value=["old"]), patch(
                "app.refresh.build", return_value=(["new"], [], "detail")), patch(
                "app.spotify.requests.request") as request:
            request.return_value.ok = False
            request.return_value.status_code = 429
            request.return_value.headers = {"Retry-After": "60"}
            with self.assertRaises(SpotifyError):
                self.service.run()
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("Retry-After: '60'", entry["detail"])
        self.assertIsNone(self.store.pending())

    def test_write_rate_limit_is_recorded_if_verification_fails(self):
        api = Spotify.__new__(Spotify)
        api.token = "token"
        with patch("app.refresh.Spotify", return_value=api), patch(
                "app.refresh.build", return_value=(["new"], [], "detail")), patch(
                "app.spotify.requests.request") as request:
            before, limited, failed_check = (unittest.mock.Mock(), unittest.mock.Mock(), unittest.mock.Mock())
            before.ok = True
            before.content = b"data"
            before.json.return_value = {"items": [], "next": None}
            limited.ok = False
            limited.status_code = 429
            limited.headers = {"Retry-After": "60"}
            failed_check.ok = False
            failed_check.status_code = 503
            request.side_effect = [before, limited, failed_check]
            with self.assertRaises(SpotifyError):
                self.service.run()
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "uncertain")
        self.assertIn("Retry-After: '60'", entry["detail"])
        self.assertIn("verification failed", entry["detail"])
        self.assertIsNotNone(self.store.pending())

    def test_conflicting_pending_write_must_be_explicitly_released(self):
        history_id = self.store.start()
        self.store.prepare(history_id, "target", ["old"], ["new"], ["episode"], "detail")

        class Fake:
            def playlist_items(self, pid):
                return ["other"]

        with self.assertRaises(SpotifyError):
            self.service.reconcile(Fake())
        self.assertIsNotNone(self.store.pending())
        self.store.release_pending()
        self.assertIsNone(self.store.pending())
        self.assertEqual(self.store.history()[0]["status"], "uncertain")

    def test_restart_records_interrupted_attempt(self):
        history_id = self.store.start()
        self.store.prepare(history_id, "target", ["old"], ["new"], ["episode"], "detail")
        restored = Store(self.store.path)
        self.assertEqual(restored.history()[0]["status"], "uncertain")
        self.assertIsNotNone(restored.pending())


if __name__ == "__main__":
    unittest.main()
