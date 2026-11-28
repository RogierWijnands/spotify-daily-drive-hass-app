import os
import random
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from requests import RequestException

from app.refresh import Refresh
from app.request_stats import RefreshRequestStats, current_refresh_stats
from app.selection import (
    build, build_items, interleave, music_candidates, select_music, podcast_candidates, get_api_items,
)
from app.spotify import (
    Spotify, SpotifyError, SpotifyPartialError, SpotifyRateLimitError, authorization, callback_code, playlist_id,
    show_id, token_request,
)
from app.store import Store
from app.timezones import current_time

NEWS = "news"
PODCASTS = ["podcast1", "podcast2"]
NEWS_SHOWS = [{"show": NEWS, "count": 4}]
PODCAST_SHOWS = [{"show": show, "count": 1} for show in PODCASTS]


def track(number, minutes=4):
    return {"uri": f"spotify:track:{number}", "duration_ms": minutes * 60000}


class UncachedSpotify:
    market = "NL"

    # Selection fakes bypass SQLite; cache behavior is tested with the real Store.
    def daily_cached(self, key, fetch, trace=None):
        return fetch()

    def liked_tracks(self, rng):
        return self.pages("/me/tracks", {"market": "NL"}, max_items=None)

    def cached_today(self, key):
        return None

    def cache_today(self, key, value):
        pass


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
        class Fake(UncachedSpotify):
            def get(self, path, **params):
                show = path.split("/")[2]
                return {"next": None, "items": [
                    {"type": "episode", "uri": f"{show}:restricted", "duration_ms": 1000,
                     "is_playable": True, "restrictions": {"reason": "market"}},
                    {"type": "episode", "uri": f"{show}:unknown", "duration_ms": 1000},
                ] + [
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
        class Fake(UncachedSpotify):
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

    def test_episode_shortfall_is_a_warning_not_a_source_error(self):
        class Fake(UncachedSpotify):
            def get(self, path, **params):
                return {"items": [], "next": None}

        with patch("app.selection.music_candidates", return_value=[[track(i) for i in range(25)]]), \
             self.assertLogs("app.selection", level="WARNING"):
            _, _, detail = build(Fake(), [], [{"show": "news", "count": 1}], [], random.Random(1))
        self.assertIn("Source warning: Show news has 0 of 1 requested playable episodes", detail)
        self.assertNotIn("Source error: Show news", detail)

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
        class Fake(UncachedSpotify):
            def get(self, path, **params):
                return {"items": [], "next": None}
        with self.assertLogs("app.selection", level="WARNING") as logs:
            self.assertEqual(podcast_candidates(Fake(), NEWS_SHOWS, []), [])
        self.assertIn("news", "\n".join(logs.output))

    def test_podcast_failure_keeps_available_episodes_and_continues_other_shows(self):
        class Fake(UncachedSpotify):
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

    def test_news_and_podcasts_use_today_cache_only_after_errors(self):
        def episode(show, number):
            return {"type": "episode", "uri": f"spotify:episode:{show}-{number}", "name": f"{show}-{number}",
                    "duration_ms": 5 * 60000, "is_playable": True}

        with tempfile.TemporaryDirectory() as directory:
            class Fake(Spotify):
                def __init__(self):
                    self.store = Store(os.path.join(directory, "cache.sqlite"))
                    self.mode = "fresh"
                    self.calls = []

                def get(self, path, **params):
                    self.calls.append(path)
                    show = path.split("/")[2]
                    if self.mode == "failed":
                        if show == "news":
                            raise SpotifyRateLimitError("News unavailable; Retry-After: '45'")
                        raise RequestException("Podcast unavailable")
                    number = 2 if self.mode == "new" else 0
                    count = 2 if show == "news" else 1
                    return {"items": [episode(show, number + i) for i in range(count)], "next": None}

            api = Fake()
            stats = RefreshRequestStats()
            stats_token = current_refresh_stats.set(stats)
            self.addCleanup(current_refresh_stats.reset, stats_token)
            api.store.set_many({"timezone": "Europe/Amsterdam"})
            news = [{"show": "news", "count": 2}]
            podcasts = [{"show": "podcast", "count": 1}]
            with patch("app.spotify.datetime") as clock:
                clock.now.side_effect = lambda tz: datetime(2026, 1, 15, 22, 30, tzinfo=timezone.utc).astimezone(tz)
                first = podcast_candidates(api, news, podcasts)
                self.assertEqual(stats.cache_hits, 0)
                api.store = Store(api.store.path)
                api.mode = "failed"
                with patch("app.selection.music_candidates", return_value=[[track(i) for i in range(25)]]), \
                        self.assertLogs("app.selection", level="WARNING"):
                    uris, episode_uris, detail = build(api, [], news, podcasts, random.Random(1))
                self.assertEqual(stats.cache_hits, 2)
                self.assertEqual(episode_uris, [item["uri"] for item in first])
                self.assertTrue(set(episode_uris).issubset(uris))
                history_id = api.store.start()
                api.store.finish(history_id, "succeeded", detail)
                saved = api.store.history()[0]["detail"]
                self.assertIn("News show news: 2/2 requested episodes selected", saved)
                self.assertIn("2 previously fetched episodes reused from today's cache after a Spotify error.", saved)
                self.assertIn("Podcast show podcast: 1/1 requested episodes selected", saved)
                self.assertIn("1 previously fetched episode reused from today's cache after a Spotify error.", saved)
                self.assertIn("News unavailable; Retry-After: '45'", saved)
                self.assertIn("Podcast unavailable", saved)

                api.mode = "new"
                trace, issues = [], []
                new = podcast_candidates(api, news, podcasts, issues, trace)
                self.assertEqual(stats.cache_hits, 2)
                self.assertNotEqual([item["uri"] for item in new], episode_uris)
                self.assertEqual(issues, [])
                self.assertNotIn("reused from today's cache", "\n".join(trace))
                self.assertNotIn("unavailable", "\n".join(trace))

                clock.now.side_effect = lambda tz: datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc).astimezone(tz)
                api.mode = "failed"
                trace = []
                with self.assertLogs("app.selection", level="WARNING"):
                    self.assertEqual(podcast_candidates(api, news, podcasts, trace=trace), [])
                self.assertEqual(stats.cache_hits, 2)
                self.assertIn("No episodes from today's cache were available.", "\n".join(trace))
                self.assertIn("0/2 requested episodes selected", trace[0])
                self.assertIn("0 episodes selected because Spotify requests failed.", trace[0])
                self.assertEqual(api.calls.count("/shows/news/episodes"), 4)
                self.assertEqual(api.calls.count("/shows/podcast/episodes"), 4)

    def test_partial_episode_fetch_prefers_fresh_and_preserves_cached_episodes(self):
        def episode(name):
            return {"type": "episode", "uri": f"spotify:episode:{name}", "duration_ms": 60000,
                    "is_playable": True}

        with tempfile.TemporaryDirectory() as directory:
            class Fake(Spotify):
                def __init__(self):
                    self.store = Store(os.path.join(directory, "cache.sqlite"))
                    self.fail_first_page = False

                def get(self, path, **params):
                    if self.fail_first_page or params["offset"]:
                        raise SpotifyError("episode page unavailable")
                    return {"items": [episode("new"), episode("old")], "next": "next"}

            api = Fake()
            api.cache_today("show_episodes_news_cache", [episode("old"), episode("older")])
            trace = []
            with self.assertLogs("app.selection", level="WARNING"):
                result = podcast_candidates(api, [{"show": "news", "count": 3}], [], trace=trace)
            self.assertEqual([item["uri"] for item in result],
                             ["spotify:episode:new", "spotify:episode:old", "spotify:episode:older"])
            self.assertIn("1 previously fetched episode reused from today's cache", "\n".join(trace))
            api.fail_first_page = True
            with self.assertLogs("app.selection", level="WARNING"):
                again = podcast_candidates(api, [{"show": "news", "count": 3}], [])
            self.assertEqual([item["uri"] for item in again], [item["uri"] for item in result])

    def test_failed_music_sources_leave_other_sources_available(self):
        class Fake(UncachedSpotify):
            def get(self, path, **params):
                if path == "/me/top/tracks" and params["time_range"] == "short_term":
                    return {"items": [{"type": "track", "id": "unavailable"}]}
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
                         "artists inaccessible", "followed inaccessible",
                         "playlist inaccessible", "show inaccessible"):
            self.assertIn(expected, messages)
            self.assertIn(expected, "\n".join(trace))

    def test_invalid_candidates_do_not_hide_later_tracks(self):
        class Fake(UncachedSpotify):
            def get(self, path, **params):
                if path.startswith("/tracks/"):
                    raise AssertionError("Individual tracks must not be fetched")
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                if path == "/me/tracks":
                    return []
                return [
                    {"item": {"id": f"bad{i}", "type": "track"}} for i in range(80)
                ] + [
                    {"item": {"id": str(i), "type": "track", "uri": f"spotify:track:{i}",
                              "duration_ms": 240000, "is_playable": True}} for i in range(100)
                ]

            def followed_artists(self):
                return []

        class NoShuffle(random.Random):
            def shuffle(self, items):
                pass

        groups = music_candidates(Fake(), ["source"], NoShuffle(1))
        self.assertEqual(len(groups[3]), 80)
        items, _, _ = build_items(Fake(), ["source"], [], [], NoShuffle(1))
        self.assertLessEqual(abs(sum(item["duration_ms"] for item in items) - 90 * 60000), 2 * 60000)

    def test_tracks_are_selected_without_checking_playability(self):
        unknown = {"id": "unknown", "uri": "spotify:track:unknown", "type": "track", "duration_ms": 180000}
        unavailable = {"id": "unavailable", "uri": "spotify:track:unavailable", "type": "track",
                       "duration_ms": 180000, "is_playable": False, "restrictions": {"reason": "market"}}
        local = {"id": "local", "uri": "spotify:track:local", "type": "track",
                 "duration_ms": 180000, "is_local": True}

        class Fake(UncachedSpotify):
            def get(self, path, **params):
                if path.startswith("/tracks/"):
                    raise AssertionError("Individual tracks must not be fetched")
                if path == "/me/top/tracks" and params["time_range"] == "short_term":
                    return {"items": [unknown, unavailable, local, {"id": "missing", "type": "track"}]}
                if path == "/me/player/recently-played":
                    return {"items": [{"track": unknown}]}
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self):
                return []

        groups = music_candidates(Fake(), [], random.Random(1))
        self.assertCountEqual([item["id"] for item in groups[0]], ["unknown", "unknown", "unavailable"])

    def test_selects_configured_show_counts_and_interleaves_categories(self):
        class Fake(UncachedSpotify):
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

    def test_liked_tracks_can_fill_the_mix(self):
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
        self.assertEqual(len(groups[4]), 31)

        podcasts = [{"uri": f"spotify:episode:{i}", "duration_ms": 7 * 60000} for i in range(5)]
        selected = select_music(groups, podcasts, random.Random(3))
        self.assertTrue(selected)
        self.assertTrue(all(item in groups[4] for item in selected))
        self.assertLessEqual(abs(sum(i["duration_ms"] for i in selected + podcasts) - 90 * 60000), 2 * 60000)

    def test_collaborator_releases_are_candidates_without_recursive_search(self):
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
        self.assertIn("Collaborators searched: Guest Artist (collaborator); up to two available artist releases "
                      "per searched artist were randomly chosen.", "\n".join(trace))
        self.assertEqual(len(groups[-1]), 26)
        self.assertIn("unplayable", [item["id"] for item in groups[-1]])
        self.assertNotIn("unrelated-album-track", [item["id"] for item in groups[-1]])

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
                self.artist_release_requests = []
                self.selected_albums = []

            def get(self, path, **params):
                if path == "/me/top/artists":
                    return {"items": [{"id": "artist"}]}
                if path == "/artists/artist/albums":
                    self.artist_release_requests.append(params)
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
        self.assertEqual(api.artist_release_requests, [
            {"market": "NL", "include_groups": "album,single,appears_on", "limit": 10},
            {"market": "NL", "include_groups": "album,single,appears_on", "limit": 1, "offset": 12},
        ])

    def test_nearby_randomly_chosen_artist_releases_share_a_request_and_fall_back_on_failure(self):
        class Fake(UncachedSpotify):
            def __init__(self, fail_combined):
                self.fail_combined = fail_combined
                self.artist_release_requests = []
                self.albums = []

            def get(self, path, **params):
                if path == "/me/top/artists":
                    return {"items": [{"id": "artist"}]}
                if path == "/artists/artist/albums":
                    self.artist_release_requests.append(params)
                    offset = params.get("offset", 0)
                    if offset and params["limit"] > 1:
                        if self.fail_combined == "error":
                            raise SpotifyError("combined unavailable")
                        if self.fail_combined == "invalid":
                            return {"items": [{"id": "incomplete"}]}
                    return {"total": 30, "items": [
                        {"id": str(i)} for i in range(offset, min(offset + params["limit"], 30))
                    ]}
                if path.startswith("/albums/"):
                    self.albums.append(path.split("/")[2])
                    return {"items": []}
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self):
                return []

        class FixedRandom(random.Random):
            def __init__(self, positions):
                super().__init__(1)
                self.positions = positions

            def sample(self, population, k):
                if len(population) == 30 and k == 2:
                    return self.positions
                return super().sample(population, k)

        cases = (
            ([19, 12], None, [(12, 8)]),
            ([21, 12], None, [(12, 10)]),
            ([22, 12], None, [(22, 1), (12, 1)]),
            ([19, 12], "error", [(12, 8), (19, 1), (12, 1)]),
            ([19, 12], "invalid", [(12, 8), (19, 1), (12, 1)]),
        )
        for positions, fails, expected in cases:
            with self.subTest(positions=positions, fails=fails):
                api = Fake(fails)
                if fails:
                    with self.assertLogs("app.selection", level="WARNING") as logs:
                        music_candidates(api, [], FixedRandom(tuple(positions)))
                    self.assertIn("combined unavailable" if fails == "error" else "invalid combined",
                                  "\n".join(logs.output))
                else:
                    music_candidates(api, [], FixedRandom(tuple(positions)))
                self.assertEqual(api.albums, [str(index) for index in positions])
                self.assertEqual([(p["offset"], p["limit"]) for p in api.artist_release_requests[1:]], expected)

    def test_shared_album_tracks_are_reused_only_within_a_refresh(self):
        class Fake(UncachedSpotify):
            def __init__(self, fail_first=False):
                self.album_calls = 0
                self.fail_first = fail_first

            def get(self, path, **params):
                if path == "/me/top/artists":
                    return {"items": [{"id": "first"}, {"id": "second"}]}
                if path.startswith("/artists/"):
                    return {"total": 1, "items": [{"id": "shared"}]}
                if path == "/albums/shared/tracks":
                    self.album_calls += 1
                    if self.fail_first and self.album_calls == 1:
                        raise SpotifyError("album unavailable")
                    return {"items": [{"id": "song", "type": "track", "uri": "spotify:track:song",
                                       "duration_ms": 180000}]}
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self, trace=None):
                return []

        stats = RefreshRequestStats()
        stats_token = current_refresh_stats.set(stats)
        self.addCleanup(current_refresh_stats.reset, stats_token)
        api = Fake()
        for expected_calls in (1, 2):
            trace = []
            groups = music_candidates(api, [], random.Random(1), trace=trace)
            self.assertEqual(api.album_calls, expected_calls)
            self.assertEqual(stats.cache_hits, expected_calls)
            self.assertEqual([item["id"] for item in groups[2]], ["song", "song"])
            self.assertIn("Artist release track lists: 1 reused from this refresh's cache; "
                          "1 retrieved from Spotify during this refresh.", trace)

        api = Fake(fail_first=True)
        with self.assertLogs("app.selection", level="WARNING") as logs:
            groups = music_candidates(api, [], random.Random(1))
        self.assertIn("album unavailable", "\n".join(logs.output))
        self.assertEqual(api.album_calls, 2)
        self.assertEqual(stats.cache_hits, 2)
        self.assertEqual([item["id"] for item in groups[2]], ["song"])

    def test_artist_and_artist_release_errors_do_not_block_other_music(self):
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
                    raise SpotifyError("artist release unavailable")
                if path == "/albums/good/tracks":
                    return {"items": [
                        {"id": str(i), "type": "track", "uri": f"spotify:track:{i}",
                         "duration_ms": 240000, "is_playable": True,
                         "artists": [{"id": "good"}, {"id": "collaborator"}]}
                        for i in range(25)
                    ] + [{"id": "unavailable", "type": "track", "artists": [{"id": "good"}]}]}
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                return []

            def followed_artists(self, trace=None):
                return []

        with self.assertLogs("app.selection", level="WARNING") as logs:
            items, _, _ = build_items(Fake(), [], [], [], random.Random(1))
        self.assertGreaterEqual(sum(item["duration_ms"] for item in items), 88 * 60000)
        self.assertLessEqual(sum(item["duration_ms"] for item in items), 92 * 60000)
        messages = "\n".join(logs.output)
        for expected in ("bad", "Retry-After: '45'", "artist release unavailable", "collaborator",
                         "connection failed"):
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

            def followed_artists(self, trace=None):
                return []

        trace, issues = [], []
        with self.assertLogs("app.selection", level="WARNING"):
            music_candidates(Fake(), [], random.Random(1), issues, trace)
        self.assertIn("Artist release track lists: 0 reused from this refresh's cache; "
                      "0 retrieved from Spotify during this refresh; "
                      "0 retrieved because artist release requests failed.", trace)

        with self.assertLogs("app.selection", level="WARNING"):
            with self.assertRaisesRegex(SpotifyError, "No suitable music found"):
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

    def test_artist_range_is_limited_to_one_page(self):
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
            self.assertEqual(api.calls.count("/artists/followed/albums"), 1)

            api.store.set_cached("liked_tracks_cache", datetime.now().astimezone().date().isoformat(),
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
            for term in ("short", "medium", "long"):
                self.assertIn(f"Top tracks {term} term: retrieved from today's cache.", saved)
            self.assertIn("Recently played tracks: retrieved from today's cache.", saved)
            self.assertRegex(saved, r"Top artists (short|medium|long) term: retrieved from today's cache;")

    def test_top_and_recent_tracks_and_top_artists_are_cached_per_day(self):
        class FixedRange(random.Random):
            def __init__(self, value):
                super().__init__(1)
                self.value = value

            def choice(self, options):
                return self.value

        with tempfile.TemporaryDirectory() as directory:
            class Fake(Spotify):
                def __init__(self):
                    self.store = Store(os.path.join(directory, "cache.sqlite"))
                    self.calls = []

                def get(self, path, **params):
                    self.calls.append((path, params))
                    if path == "/me/top/tracks":
                        term = params["time_range"]
                        count = sum(p == path and args["time_range"] == term for p, args in self.calls)
                        return {"items": [{"id": f"{term}-{count}", "type": "track",
                                           "uri": f"spotify:track:{term}-{count}", "duration_ms": 180000}]}
                    if path == "/me/top/artists":
                        return {"items": [{"id": params["time_range"]}]}
                    if path == "/me/player/recently-played":
                        count = sum(p == path for p, _ in self.calls)
                        return {"items": [{"track": {"id": f"recent-{count}", "type": "track",
                                                     "uri": f"spotify:track:recent-{count}",
                                                     "duration_ms": 180000}}]}
                    if path.startswith("/artists/"):
                        return {"items": [], "total": 0}
                    return {"items": []}

                def liked_tracks(self, rng):
                    return []

                def followed_artists(self, trace=None):
                    return []

            api = Fake()
            with patch("app.spotify.datetime") as clock:
                traces = []
                for day, term in ((15, "short_term"), (15, "short_term"), (15, "medium_term"),
                                  (15, "long_term"), (16, "medium_term")):
                    clock.now.return_value = datetime(2026, 1, day, 12, tzinfo=timezone.utc)
                    trace = []
                    groups = music_candidates(api, [], FixedRange(term), trace=trace)
                    traces.append(trace)
                    short_id = "short_term-2" if day == 16 else "short_term-1"
                    self.assertIn(short_id, [item["id"] for item in groups[0]])
                    self.assertIn("recent-2" if day == 16 else "recent-1", [item["id"] for item in groups[0]])
                    chosen = "short_term" if day == 15 else "medium_term"
                    self.assertTrue(any(
                        f"Artists searched for discovery (not necessarily selected for the playlist): {chosen}."
                        in line for line in trace
                    ))
                    api.store = Store(api.store.path)

            top_calls = [(path, params["time_range"]) for path, params in api.calls if path.startswith("/me/top/")]
            for term in ("short_term", "medium_term", "long_term"):
                self.assertEqual(top_calls.count(("/me/top/tracks", term)), 2)
                self.assertEqual(top_calls.count(("/me/top/artists", term)), 0 if term == "long_term" else 1)
            self.assertEqual(sum(path == "/me/player/recently-played" for path, _ in api.calls), 2)
            self.assertIn("Top tracks short term: retrieved from Spotify and saved in today's cache.", traces[0])
            self.assertIn("Top tracks short term: retrieved from today's cache.", traces[1])
            self.assertTrue(any(line.startswith("Top artists short term: retrieved from today's cache;")
                                for line in traces[1]))
            self.assertTrue(any("Top artists short term: retrieved from Spotify and saved in today's cache; "
                                "time range chosen randomly for today;" in line for line in traces[0]))
            self.assertTrue(any("Top artists short term: retrieved from today's cache; "
                                "time range reused from today's cache;" in line for line in traces[2]))
            self.assertTrue(any("Top artists medium term: retrieved from Spotify and saved in today's cache; "
                                "time range chosen randomly for today;" in line for line in traces[4]))
            self.assertTrue(any(line.startswith(
                "Top artists medium term: retrieved from Spotify and saved in today's cache;"
            ) for line in traces[4]))
            self.assertIn("Recently played tracks: retrieved from Spotify and saved in today's cache.", traces[0])
            self.assertIn("Recently played tracks: retrieved from today's cache.", traces[1])
            self.assertIn("Recently played tracks: retrieved from Spotify and saved in today's cache.", traces[4])

    def test_source_playlists_are_cached_separately_per_local_day(self):
        with tempfile.TemporaryDirectory() as directory:
            class Fake(Spotify):
                def __init__(self):
                    self.store = Store(os.path.join(directory, "cache.sqlite"))
                    self.calls = []

                def get(self, path, **params):
                    self.calls.append((path, params))
                    if path.startswith("/playlists/"):
                        pid = path.split("/")[2]
                        if pid == "Empty":
                            return {"items": [], "next": None}
                        offset = params["offset"]
                        item = track(f"{pid}-{offset}")
                        item["id"] = f"{pid}-{offset}"
                        item["type"] = "track"
                        return {"items": [{"item": item}], "next": "next" if offset == 0 else None}
                    return {"items": []}

                def liked_tracks(self, rng):
                    return []

                def followed_artists(self, trace=None):
                    return []

            api = Fake()
            stats = RefreshRequestStats()
            token = current_refresh_stats.set(stats)
            self.addCleanup(current_refresh_stats.reset, token)
            with patch("app.spotify.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
                trace = []
                first = music_candidates(api, ["AbC", "Empty"], random.Random(1), trace=trace)
                self.assertEqual({item["id"] for item in first[3]}, {"AbC-0", "AbC-1"})
                self.assertIn("Source playlist AbC: retrieved from Spotify and saved in today's cache.", trace)
                self.assertEqual(stats.cache_hits, 0)
                api.store = Store(api.store.path)
                trace = []
                second = music_candidates(api, ["Empty", "AbC", "New"], random.Random(2), trace=trace)
                self.assertEqual({item["id"] for item in second[3]}, {"AbC-0", "AbC-1", "New-0", "New-1"})
                self.assertIn("Source playlist AbC: retrieved from today's cache.", trace)
                self.assertIn("Source playlist Empty: retrieved from today's cache.", trace)
                self.assertEqual(sum(path == "/playlists/AbC/items" for path, _ in api.calls), 2)
                self.assertEqual(sum(path == "/playlists/Empty/items" for path, _ in api.calls), 1)
                self.assertEqual(sum(path == "/playlists/New/items" for path, _ in api.calls), 2)
                self.assertEqual(stats.cache_hits, 9)

                clock.now.return_value = datetime(2026, 1, 16, 12, tzinfo=timezone.utc)
                music_candidates(api, ["AbC", "Empty"], random.Random(3))
                self.assertEqual(sum(path == "/playlists/AbC/items" for path, _ in api.calls), 4)
                self.assertEqual(sum(path == "/playlists/Empty/items" for path, _ in api.calls), 2)

    def test_source_playlist_errors_use_old_cache_or_partial_results_without_caching_them(self):
        with tempfile.TemporaryDirectory() as directory:
            class Fake(Spotify):
                def __init__(self):
                    self.store = Store(os.path.join(directory, "cache.sqlite"))
                    self.mode = "error"
                    self.calls = 0

                def get(self, path, **params):
                    if path.startswith("/playlists/"):
                        self.calls += 1
                        if self.mode == "partial" and params["offset"] == 0:
                            return {"items": [{"item": {
                                "id": "fresh", "type": "track", "uri": "spotify:track:fresh",
                                "duration_ms": 180000,
                            }}], "next": "next"}
                        raise SpotifyError("playlist unavailable")
                    return {"items": []}

                def liked_tracks(self, rng):
                    return []

                def followed_artists(self, trace=None):
                    return []

            api = Fake()
            old = [{"item": {"id": "old", "type": "track", "uri": "spotify:track:old", "duration_ms": 180000}}]
            api.store.set_cached("source_playlist_AbC_cache", "2026-01-15", old)
            with patch("app.spotify.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, 16, 12, tzinfo=timezone.utc)
                issues, trace = [], []
                with self.assertLogs("app.selection", level="WARNING"):
                    groups = music_candidates(api, ["AbC"], random.Random(1), issues, trace)
                self.assertEqual([item["id"] for item in groups[3]], ["old"])
                self.assertIn("Source playlist AbC: Spotify fetch failed; reused cache from 2026-01-15.", trace)
                self.assertTrue(any("Using cached playlist AbC after an error: playlist unavailable" in issue
                                    for issue in issues))
                self.assertIsNone(api.store.get_cached("source_playlist_AbC_cache", "2026-01-16"))

                api.mode = "partial"
                issues, trace = [], []
                with self.assertLogs("app.selection", level="WARNING"):
                    groups = music_candidates(api, ["AbC"], random.Random(1), issues, trace)
                self.assertEqual([item["id"] for item in groups[3]], ["fresh"])
                self.assertIn("Source playlist AbC: partially retrieved from Spotify during this refresh; not cached.",
                              trace)
                self.assertTrue(any("Using partial playlist AbC after an error: playlist unavailable" in issue
                                    for issue in issues))
                self.assertIsNone(api.store.get_cached("source_playlist_AbC_cache", "2026-01-16"))
                self.assertEqual(api.calls, 3)

                api.mode = "error"
                api.store.set_cached("source_playlist_Empty_cache", "2026-01-15", [])
                issues, trace = [], []
                with self.assertLogs("app.selection", level="WARNING"):
                    music_candidates(api, ["Missing", "Empty"], random.Random(1), issues, trace)
                self.assertIn(
                    "Source playlist Missing: Spotify fetch failed; no data retrieved from cache; "
                    "0 results because the fetch failed.", trace,
                )
                self.assertIn(
                    "Source playlist Empty: Spotify fetch failed; reused cache from 2026-01-15; "
                    "0 results because the fetch failed.", trace,
                )
                self.assertTrue(any("Skipping playlist Missing: playlist unavailable" in issue for issue in issues))
                self.assertTrue(any("Using cached playlist Empty after an error: playlist unavailable" in issue
                                    for issue in issues))

    def test_partial_liked_tracks_origin_is_saved_in_history(self):
        with tempfile.TemporaryDirectory() as directory:
            class Fake(Spotify):
                def __init__(self):
                    self.store = Store(os.path.join(directory, "cache.sqlite"))

                def get(self, path, **params):
                    return {"items": []}

                def liked_tracks(self, rng):
                    raise SpotifyPartialError(SpotifyError("page unavailable"), [
                        {"track": {"id": "liked", "type": "track", "uri": "spotify:track:liked",
                                   "duration_ms": 180000}}
                    ])

                def followed_artists(self, trace=None):
                    return []

            api = Fake()
            episode = {"uri": "spotify:episode:test", "duration_ms": 87 * 60000}
            with patch("app.selection.podcast_candidates", return_value=[episode]), \
                    self.assertLogs("app.selection", level="WARNING"):
                uris, episodes, detail = build(api, [], [], [], random.Random(1))
            history_id = api.store.start()
            api.store.prepare(history_id, "target", [], uris, episodes, detail)
            api.store.resolve(True)
            saved = api.store.history()[0]["detail"]
            self.assertIn("Liked tracks: partially retrieved from Spotify during this refresh; not cached.", saved)
            self.assertIn("Source error: Using partial liked tracks after an error: page unavailable", saved)

    def test_failed_or_invalid_top_and_recent_responses_are_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            class Fake(Spotify):
                def __init__(self):
                    self.store = Store(os.path.join(directory, "cache.sqlite"))
                    self.fail = True
                    self.calls = []

                def get(self, path, **params):
                    self.calls.append((path, params))
                    if self.fail and path == "/me/top/tracks" and params["time_range"] == "short_term":
                        return {"items": None}
                    if self.fail and path == "/me/top/artists":
                        raise SpotifyRateLimitError("Retry-After: '45'")
                    if self.fail and path == "/me/player/recently-played":
                        raise RequestException("recent unavailable")
                    return {"items": []}

                def liked_tracks(self, rng):
                    return []

                def followed_artists(self, trace=None):
                    return []

            class FixedRange(random.Random):
                def choice(self, options):
                    return "short_term"

            api = Fake()
            day = datetime.now().astimezone().date().isoformat()
            issues, trace = [], []
            with self.assertLogs("app.selection", level="WARNING"):
                music_candidates(api, [], FixedRange(1), issues, trace)
            self.assertIn("invalid item list", "\n".join(issues))
            self.assertIn("Retry-After: '45'", "\n".join(issues))
            self.assertIn("recent unavailable", "\n".join(issues))
            self.assertIn("Recently played tracks: Spotify fetch failed; no data retrieved from cache; "
                          "0 results because the fetch failed.", trace)
            self.assertIsNone(api.store.get_cached("top_tracks_short_term_cache", day))
            self.assertIsNone(api.store.get_cached("top_artists_short_term_cache", day))
            self.assertIsNone(api.store.get_cached("recently_played_tracks_cache", day))

            api.fail = False
            issues, trace = [], []
            with patch.object(FixedRange, "choice", return_value="long_term") as choice:
                music_candidates(api, [], FixedRange(1), issues, trace)
            choice.assert_not_called()
            self.assertTrue(all("during an earlier discovery fetch today" in issue for issue in issues))
            self.assertIn("invalid item list", "\n".join(issues))
            self.assertEqual(api.store.get_cached("top_tracks_short_term_cache", day), [])
            self.assertEqual(api.store.get_cached("top_artists_short_term_cache", day), [])
            self.assertEqual(api.store.get_cached("recently_played_tracks_cache", day), [])
            self.assertIn("Top tracks short term: retrieved from Spotify and saved in today's cache.", trace)
            self.assertTrue(any(line.startswith("Top artists short term: retrieved from Spotify and saved in today's cache;")
                                for line in trace))
            self.assertIn("Recently played tracks: retrieved from Spotify and saved in today's cache.", trace)

    def test_failed_music_sources_use_older_cache_and_report_current_errors(self):
        def song(number):
            return {"id": str(number), "type": "track", "uri": f"spotify:track:{number}",
                    "duration_ms": 4 * 60000}

        with tempfile.TemporaryDirectory() as directory:
            class Fake(Spotify):
                def __init__(self):
                    self.store = Store(os.path.join(directory, "cache.sqlite"))
                    self.fail = True
                    self.calls = []

                def get(self, path, **params):
                    self.calls.append((path, params))
                    if self.fail:
                        raise SpotifyRateLimitError("Spotify unavailable; Retry-After: '32'")
                    return {"items": []}

                def liked_tracks(self, rng):
                    if self.fail:
                        raise RequestException("Liked tracks unavailable")
                    return []

                def followed_artists(self, trace=None):
                    return []

            class FixedRange(random.Random):
                def choice(self, options):
                    return "short_term"

            api = Fake()
            old_day = "2026-01-15"
            for term, number in (("short", 0), ("medium", 1), ("long", 2)):
                api.store.set_cached(f"top_tracks_{term}_term_cache", old_day, [song(number)])
            api.store.set_cached("top_artists_short_term_cache", old_day, [])
            api.store.set_cached("recently_played_tracks_cache", old_day, [{"track": song(3)}])
            api.store.set_cached("liked_tracks_cache", old_day, [{"track": song(i)} for i in range(25)])

            with patch("app.spotify.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, 16, 12, tzinfo=timezone.utc)
                issues, trace = [], []
                with self.assertLogs("app.selection", level="WARNING"):
                    groups = music_candidates(api, [], FixedRange(1), issues, trace)
                self.assertIn("3", [item["id"] for item in groups[0]])
                self.assertEqual(len(groups[4]), 25)
                for label in ("Top tracks short term", "Top tracks medium term", "Top tracks long term",
                              "Recently played tracks", "Liked tracks"):
                    self.assertIn(f"{label}: Spotify fetch failed; reused cache from {old_day}.", trace)
                self.assertTrue(any(line.startswith(
                    f"Top artists short term: Spotify fetch failed; reused cache from {old_day};"
                ) for line in trace))
                self.assertTrue(any("0 results because the fetch failed" in line for line in trace
                                    if line.startswith("Top artists short term:")))
                self.assertTrue(any("artist source errors prevented discovery." in line for line in trace
                                    if line.startswith("Top artists short term:")))
                self.assertIn("Retry-After: '32'", "\n".join(issues))
                self.assertIn("Liked tracks unavailable", "\n".join(issues))
                self.assertIsNone(api.store.get_cached("liked_tracks_cache", "2026-01-16"))

                with self.assertLogs("app.selection", level="WARNING"):
                    _, _, detail = build(api, [], [], [], FixedRange(1))
                history_id = api.store.start()
                api.store.finish(history_id, "succeeded", detail)
                saved = api.store.history()[0]["detail"]
                self.assertIn(f"Liked tracks: Spotify fetch failed; reused cache from {old_day}.", saved)
                self.assertIn("Source error: Using cached liked tracks after an error: Liked tracks unavailable",
                              saved)
                self.assertIn("Retry-After: '32'", saved)

                api.fail = False
                issues, trace = [], []
                groups = music_candidates(api, [], FixedRange(1), issues, trace)
                self.assertFalse(any(groups))
                self.assertTrue(all("during an earlier discovery fetch today" in issue for issue in issues))
                self.assertIn("Spotify unavailable", "\n".join(issues))
                self.assertIn("Liked tracks: retrieved from Spotify and saved in today's cache.", trace)
                self.assertNotIn("unavailable", "\n".join(trace))
                self.assertIsNone(api.store.get_previous_cached("liked_tracks_cache", "2026-01-16"))

    def test_partial_fresh_music_takes_priority_over_older_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            api.store.set_cached("liked_tracks_cache", "2026-01-15", [{"track": {"id": "old"}}])
            partial = [{"track": {"id": "new"}}]

            def fetch():
                raise SpotifyPartialError(SpotifyError("later page unavailable"), partial)

            with patch("app.spotify.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, 16, 12, tzinfo=timezone.utc)
                trace, issues = [], []
                with self.assertLogs("app.selection", level="WARNING"):
                    result = get_api_items(
                        "liked tracks", lambda: api.daily_cached("liked_tracks_cache", fetch, trace), issues)
                self.assertEqual(result, partial)
                self.assertIn("later page unavailable", "\n".join(issues))
                self.assertIn("Liked tracks: partially retrieved from Spotify during this refresh; not cached.", trace)
                self.assertEqual(api.store.get_previous_cached("liked_tracks_cache", "2026-01-16"),
                                 ("2026-01-15", [{"track": {"id": "old"}}]))


class SpotifyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(os.path.join(directory.name, "requests.sqlite"))

    def test_existing_artist_release_track_counts_keep_their_numbering(self):
        started = datetime.now().astimezone()
        self.store.start_request("Release tracks", started, "Read; release ID album123")
        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.token = "secret"
        response = Mock(ok=False, status_code=429, headers={},
                        json=Mock(return_value={}), content=b"{}")
        with patch("app.spotify.requests.request", return_value=response):
            with self.assertRaises(SpotifyRateLimitError):
                api.get("/albums/album123/tracks")

        overview = self.store.request_overview(started.date().isoformat())
        self.assertEqual(overview["categories"], [
            {"category": "Artist release tracks", "requests": 2, "errors": 1}
        ])
        self.assertEqual(overview["first_quota"]["category"], "Artist release tracks")
        self.assertEqual(overview["first_quota"]["category_number"], 2)
        self.assertEqual(overview["first_quota"]["detail"], "Read; artist release ID album123")

    def test_existing_artist_release_quota_details_use_clearer_labels(self):
        started = datetime.now().astimezone()
        info = self.store.start_request("Release tracks", started, "Read; release ID album123")
        self.store.fail_request(info, quota=True)
        first_quota = Store(self.store.path).request_overview(started.date().isoformat())["first_quota"]
        self.assertEqual(first_quota["category"], "Artist release tracks")
        self.assertEqual(first_quota["detail"], "Read; artist release ID album123")

    def test_daily_discovery_cache_limits_requests_and_keeps_partial_results(self):
        class FixedRandom(random.Random):
            def shuffle(self, values):
                pass

            def sample(self, population, k):
                if len(population) == 30 and k == 2:
                    return [10, 25]
                return super().sample(population, k)

        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.token = "secret"
        api.followed_artists = Mock(return_value=[])
        api.liked_tracks = Mock(return_value=[])
        fail_artist_release = False

        def request(method, url, **kwargs):
            path = url.removeprefix("https://api.spotify.com/v1")
            params = kwargs["params"]
            if path == "/me/top/artists":
                body = {"items": [{"id": f"artist{i}"} for i in range(8)]}
            elif path.startswith("/artists/"):
                artist = path.split("/")[2]
                offset = params.get("offset", 0)
                body = {"total": 30, "items": [
                    {"id": f"{artist}release{i}"}
                    for i in range(offset, min(offset + params["limit"], 30))
                ]}
            elif path.startswith("/albums/"):
                album = path.split("/")[2]
                if fail_artist_release and album == "artist0release10":
                    return Mock(ok=False, status_code=429, headers={},
                                json=Mock(return_value={}), content=b"{}")
                artist = album.split("release")[0]
                credits = [{"id": artist}]
                if artist == "artist0":
                    credits += [{"id": f"collaborator{i}"} for i in range(4)]
                body = {"items": [{
                    "id": album, "type": "track", "uri": f"spotify:track:{album}",
                    "duration_ms": 180000, "artists": credits,
                }]}
            else:
                body = {"items": []}
            return Mock(ok=True, status_code=200, json=Mock(return_value=body), content=b"{}")

        with patch("app.spotify.datetime") as clock, \
             patch("app.spotify.requests.request", side_effect=request):
            clock.now.return_value = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
            first = music_candidates(api, [], FixedRandom(1), trace=[])
            first_overview = self.store.request_overview("2026-01-15")
            categories = {entry["category"]: entry for entry in first_overview["categories"]}
            self.assertEqual(categories["Artist releases"]["requests"], 36)
            self.assertEqual(categories["Artist release tracks"]["requests"], 24)
            self.assertEqual((len(first[2]), len(first[5])), (16, 8))

            # Reopen SQLite and repeat four times: neither discovery category grows.
            api.store = Store(self.store.path)
            for _ in range(4):
                trace = []
                cached = music_candidates(api, [], FixedRandom(2), trace=trace)
                self.assertEqual((cached[2], cached[5]), (first[2], first[5]))
                self.assertTrue(any("reused from today's cache" in line
                                    and "no artist release" in line for line in trace))
            self.assertEqual(self.store.request_overview("2026-01-15"), first_overview)

            # A failed artist release on the next day is saved as a partial result, not retried.
            clock.now.return_value = datetime(2026, 1, 16, 12, tzinfo=timezone.utc)
            fail_artist_release = True
            issues, trace = [], []
            with self.assertLogs("app.selection", level="WARNING"):
                partial = music_candidates(api, [], FixedRandom(1), issues, trace)
            self.assertTrue(any("Retry-After" in issue for issue in issues))
            self.assertEqual(len(partial[2]), 15)
            next_overview = self.store.request_overview("2026-01-16")
            categories = {entry["category"]: entry for entry in next_overview["categories"]}
            self.assertEqual(categories["Artist releases"]["requests"], 36)
            self.assertEqual((categories["Artist release tracks"]["requests"],
                              categories["Artist release tracks"]["errors"]), (24, 1))

            api.store = Store(self.store.path)
            issues, trace = [], []
            reused = music_candidates(api, [], FixedRandom(3), issues, trace)
            self.assertEqual((reused[2], reused[5]), (partial[2], partial[5]))
            self.assertEqual(self.store.request_overview("2026-01-16"), next_overview)
            self.assertTrue(any("earlier discovery fetch today" in issue for issue in issues))
            self.assertTrue(any("earlier discovery fetch encountered errors" in line for line in trace))

    def test_empty_failed_discoveries_are_reused_and_reported_in_history(self):
        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.token = "secret"
        api.followed_artists = Mock(return_value=[])
        api.liked_tracks = Mock(return_value=[])

        def request(method, url, **kwargs):
            path = url.removeprefix("https://api.spotify.com/v1")
            if path == "/artists/favorite/albums":
                return Mock(ok=False, status_code=429, headers={},
                            json=Mock(return_value={}), content=b"{}")
            if path == "/me/top/artists":
                items = [{"id": "favorite"}]
            elif path == "/me/top/tracks":
                items = [{"id": str(i), "type": "track", "uri": f"spotify:track:{i}",
                          "duration_ms": 240000} for i in range(25)]
            else:
                items = []
            return Mock(ok=True, status_code=200, json=Mock(return_value={"items": items}),
                        content=b"{}")

        with patch("app.spotify.requests.request", side_effect=request), \
             self.assertLogs("app.selection", level="WARNING"):
            build(api, [], [], [], random.Random(1))

        day = datetime.now().astimezone().date().isoformat()
        before = self.store.request_overview(day)
        _, _, detail = build(api, [], [], [], random.Random(2))
        after = self.store.request_overview(day)
        self.assertEqual(before, after)
        self.assertIn("0 artist tracks and 0 collaborator tracks", detail)
        self.assertIn("the earlier discovery fetch encountered errors", detail)
        self.assertIn("Source error: Skipping artist releases for artist favorite:", detail)
        self.assertIn("during an earlier discovery fetch today", detail)
        self.assertIn("artist discoveries: 0 (no candidates because source retrieval failed)", detail)

    def test_discovery_fetch_crossing_midnight_is_cached_for_its_start_day(self):
        api = Spotify.__new__(Spotify)
        api.store = self.store
        self.store.set_many({"timezone": "UTC"})
        api.followed_artists = Mock(return_value=[])
        api.liked_tracks = Mock(return_value=[])
        first = datetime(2026, 1, 15, 23, 59, tzinfo=timezone.utc)
        next_day = datetime(2026, 1, 16, 0, 0, tzinfo=timezone.utc)

        with patch("app.spotify.datetime") as clock:
            clock.now.return_value = first

            def get(path, **params):
                if path == "/me/top/artists":
                    return {"items": [{"id": "favorite"}]}
                if path == "/artists/favorite/albums":
                    clock.now.return_value = next_day
                    return {"total": 0, "items": []}
                return {"items": []}

            api.get = Mock(side_effect=get)
            music_candidates(api, [], random.Random(1))
            self.assertIsNotNone(self.store.get_cached("discovery_tracks_cache", "2026-01-15"))
            self.assertIsNone(self.store.get_cached("discovery_tracks_cache", "2026-01-16"))
            music_candidates(api, [], random.Random(2))
            self.assertEqual(sum(call.args[0] == "/artists/favorite/albums"
                                 for call in api.get.call_args_list), 2)

    def test_unrelated_podcast_failure_is_not_saved_as_discovery_failure(self):
        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.get = Mock(return_value={"items": []})
        api.followed_artists = Mock(return_value=[])
        api.liked_tracks = Mock(return_value=[])
        issues = ["Failed to fetch remaining episodes for show news: unavailable"]
        music_candidates(api, [], random.Random(1), issues)

        later_issues, trace = [], []
        music_candidates(api, [], random.Random(2), later_issues, trace)
        self.assertEqual(later_issues, [])
        self.assertFalse(any("earlier discovery fetch encountered errors" in line for line in trace))

    def test_configured_market_reaches_all_market_sensitive_requests(self):
        self.store.set_many({"client_id": "client", "refresh_token": "token", "market": "US"})
        with patch("app.spotify.token_request", return_value={"access_token": "access"}):
            api = Spotify(self.store)

        calls = []

        def get(path, **params):
            calls.append((path, params))
            if path == "/me/top/artists":
                return {"items": [{"id": "artist"}]}
            if path == "/artists/artist/albums":
                return {"items": [{"id": "album"}], "total": 1}
            if path == "/me/tracks":
                return {"items": [], "total": 0}
            return {"items": [], "next": None}

        api.get = get
        api.followed_artists = Mock(return_value=[])
        api.playlist_items("target")
        podcast_candidates(api, [{"show": "show", "count": 1}], [])
        music_candidates(api, ["source"], random.Random(1))

        relevant = {
            "/playlists/target/items", "/playlists/source/items", "/shows/show/episodes",
            "/me/tracks", "/artists/artist/albums", "/albums/album/tracks",
        }
        market_calls = [(path, params) for path, params in calls if path in relevant]
        self.assertEqual({path for path, _ in market_calls}, relevant)
        self.assertTrue(all(params["market"] == "US" for _, params in market_calls))

    @unittest.skipUnless(hasattr(time, "tzset"), "Changing the system timezone requires tzset")
    def test_system_timezone_and_configured_override_use_correct_local_day(self):
        clock = Mock()
        clock.now.return_value = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc)

        try:
            with patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
                time.tzset()
                system_time = current_time(self.store, clock)
                self.assertEqual(system_time.isoformat(), "2026-07-15T16:30:00-07:00")
                self.store.set_many({"timezone": "Asia/Tokyo"})
                configured_time = current_time(self.store, clock)
                self.assertEqual(configured_time.isoformat(), "2026-07-16T08:30:00+09:00")
        finally:
            time.tzset()

    def test_request_counts_and_cache_follow_configured_midnight(self):
        self.store.set_many({"timezone": "America/Los_Angeles"})
        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.token = "secret"
        before = datetime(2026, 7, 16, 6, 59, tzinfo=timezone.utc)
        after = datetime(2026, 7, 16, 7, 0, tzinfo=timezone.utc)
        response = Mock(ok=True, content=b"", json=Mock(return_value={}))
        fetch = Mock(return_value=[])

        with patch("app.spotify.datetime") as clock, \
             patch("app.spotify.requests.request", return_value=response):
            clock.now.side_effect = [before, after, before, after]
            api.daily_cached("liked_tracks_cache", fetch)
            api.daily_cached("liked_tracks_cache", fetch)
            api.get("/me/tracks")
            api.get("/me/tracks")

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.store.request_overview("2026-07-15")["requests"], 1)
        self.assertEqual(self.store.request_overview("2026-07-16")["requests"], 1)

    def test_request_overview_groups_actual_requests_by_component(self):
        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.token = "secret-token"
        response = Mock(ok=True, content=b"{}", json=Mock(return_value={}))
        requests = [
            ("GET", "/me/top/tracks", {"time_range": "short_term"}, "Top tracks (short term)"),
            ("GET", "/me/top/artists", {"time_range": "long_term"}, "Top artists (long term)"),
            ("GET", "/me/player/recently-played", {}, "Recently played tracks"),
            ("GET", "/me/tracks", {}, "Liked tracks"),
            ("GET", "/me/following", {}, "Followed artists"),
            ("GET", "/shows/show123/episodes", {}, "Show episodes"),
            ("GET", "/artists/artist123/albums", {}, "Artist releases"),
            ("GET", "/albums/album123/tracks", {}, "Artist release tracks"),
            ("GET", "/playlists/playlist123/items", {}, "Playlist reads"),
            ("PUT", "/playlists/playlist123/items", {}, "Playlist updates"),
            ("GET", "/other", {}, "Other Spotify API requests"),
        ]
        with patch("app.spotify.requests.request", return_value=response):
            for method, path, params, _ in requests:
                api.request(method, path, params=params)
        with patch("app.spotify.requests.post", return_value=response):
            token_request({"grant_type": "refresh_token", "refresh_token": "private-token"}, self.store)

        day = datetime.now().astimezone().date().isoformat()
        overview = self.store.request_overview(day)
        categories = {entry["category"]: entry for entry in overview["categories"]}
        self.assertEqual(overview["requests"], len(requests) + 1)
        self.assertEqual(overview["errors"], 0)
        self.assertIsNone(overview["first_quota"])
        self.assertEqual(set(categories), {item[3] for item in requests} | {"Token requests"})
        self.assertTrue(all(entry["requests"] == 1 for entry in categories.values()))
        self.assertNotIn("secret", repr(overview))
        self.assertNotIn("private-token", repr(overview))

    def test_request_errors_quota_and_amsterdam_midnight_are_recorded(self):
        self.store.set_many({"timezone": "Europe/Amsterdam"})
        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.token = "secret"
        before_midnight = datetime(2026, 3, 29, 23, 59, tzinfo=ZoneInfo("Europe/Amsterdam"))
        after_midnight = datetime(2026, 3, 30, 0, 0, tzinfo=ZoneInfo("Europe/Amsterdam"))
        ok = Mock(ok=True, content=b"{}", json=Mock(return_value={}))
        rate_limit = Mock(ok=False, status_code=429, headers={}, json=Mock(side_effect=ValueError()))
        quota = Mock(ok=False, status_code=403, headers={},
                     json=Mock(return_value={"error": {"reason": "QUOTA_EXCEEDED"}}))
        invalid_json = Mock(ok=True, content=b"{", json=Mock(side_effect=ValueError()))

        with patch("app.spotify.datetime") as clock, \
             patch("app.spotify.requests.request", side_effect=[
                 ok, rate_limit, quota, RequestException("network failure"), invalid_json, ok,
             ]):
            clock.now.side_effect = [before_midnight] * 5 + [after_midnight]
            api.get("/me/tracks")
            with self.assertRaises(SpotifyRateLimitError):
                api.get("/artists/artist123/albums", offset=20, limit=10, include_groups="album,single")
            with self.assertRaises(SpotifyError):
                api.get("/me/tracks")
            with self.assertRaises(RequestException):
                api.get("/me/following")
            with self.assertRaises(ValueError):
                api.get("/me/tracks")
            api.get("/me/tracks")

        # A new Store instance must see the same saved numbers and first quota response.
        overview = Store(self.store.path).request_overview("2026-03-29")
        self.assertEqual((overview["requests"], overview["errors"]), (5, 4))
        self.assertEqual(overview["first_quota"], {
            "number": 2, "category": "Artist releases", "category_number": 1,
            "started": before_midnight.isoformat(),
            "detail": "Read; artist ID artist123; offset 20; limit 10; artist release types: albums and singles",
        })
        categories = {entry["category"]: entry for entry in overview["categories"]}
        self.assertEqual((categories["Liked tracks"]["requests"], categories["Liked tracks"]["errors"]), (3, 2))
        self.assertEqual((categories["Followed artists"]["requests"], categories["Followed artists"]["errors"]), (1, 1))
        tomorrow = self.store.request_overview("2026-03-30")
        self.assertEqual((tomorrow["requests"], tomorrow["errors"], tomorrow["first_quota"]), (1, 0, None))

    def test_quota_reason_and_first_completed_response(self):
        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.token = "secret"
        quota = Mock(ok=False, status_code=403, headers={},
                     json=Mock(return_value={"error": {"reason": "QUOTA_EXCEEDED"}}))
        with patch("app.spotify.requests.request", return_value=quota):
            with self.assertRaises(SpotifyError):
                api.get("/me/tracks")

        day = datetime.now().astimezone().date().isoformat()
        self.assertEqual(self.store.request_overview(day)["first_quota"]["number"], 1)

        other_day = datetime(2026, 1, 1, 12, tzinfo=ZoneInfo("Europe/Amsterdam"))
        earlier = self.store.start_request("Liked tracks", other_day, "GET")
        later = self.store.start_request("Liked tracks", other_day, "GET; offset 50")
        self.store.fail_request(later, quota=True)
        self.store.fail_request(earlier, quota=True)
        first = self.store.request_overview("2026-01-01")["first_quota"]
        self.assertEqual((first["number"], first["category_number"], first["detail"]), (2, 2, "GET; offset 50"))

        self.store.clear_data()
        self.assertEqual(self.store.request_overview(day)["requests"], 0)
        self.assertEqual(self.store.request_overview("2026-01-01")["categories"], [])

    def test_token_quota_failure_is_counted_without_storing_credentials(self):
        quota = Mock(ok=False, status_code=429, headers={}, json=Mock(return_value={}))
        with patch("app.spotify.requests.post", return_value=quota):
            with self.assertRaises(SpotifyRateLimitError):
                token_request({"grant_type": "refresh_token", "refresh_token": "private-token"}, self.store)

        day = datetime.now().astimezone().date().isoformat()
        overview = self.store.request_overview(day)
        self.assertEqual((overview["requests"], overview["errors"]), (1, 1))
        self.assertEqual(overview["first_quota"]["category"], "Token requests")
        self.assertEqual(overview["first_quota"]["detail"], "Token refresh")
        self.assertNotIn("private-token", repr(overview))

    def test_concurrent_requests_get_unique_numbers(self):
        started = datetime(2026, 1, 1, 12, tzinfo=ZoneInfo("Europe/Amsterdam"))
        with ThreadPoolExecutor(max_workers=8) as executor:
            requests = list(executor.map(
                lambda _: self.store.start_request("Liked tracks", started, "Read"), range(20)
            ))
        self.assertEqual({item[2] for item in requests}, set(range(1, 21)))
        self.assertEqual({item[3] for item in requests}, set(range(1, 21)))
        self.assertEqual(self.store.request_overview("2026-01-01")["requests"], 20)

    def test_statistics_failure_does_not_replace_spotify_request_outcome(self):
        api = Spotify.__new__(Spotify)
        api.store = Mock()
        api.store.get.return_value = "system"
        api.token = "secret"
        ok = Mock(ok=True, content=b"", json=Mock(return_value={}))
        api.store.start_request.side_effect = sqlite3.OperationalError("database unavailable")
        with patch("app.spotify.requests.request", return_value=ok), \
             self.assertLogs("app.spotify", level="ERROR") as logged:
            self.assertEqual(api.request("PUT", "/playlists/target/items"), {})
        self.assertIn("Could not record Spotify request", logged.output[0])

        api.store.start_request.side_effect = None
        api.store.fail_request.side_effect = sqlite3.OperationalError("database unavailable")
        failure = Mock(ok=False, status_code=429, headers={}, json=Mock(return_value={}))
        with patch("app.spotify.requests.request", return_value=failure), \
             self.assertLogs("app.spotify", level="ERROR") as logged, \
             self.assertRaises(SpotifyRateLimitError):
            api.get("/me/tracks")
        self.assertIn("Could not record Spotify request failure", logged.output[0])

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

    def test_liked_tracks_select_up_to_five_distinct_random_pages(self):
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
                self.assertEqual(requests_seen[0], ("/me/tracks", {"market": "NL", "limit": 50, "offset": 0}))
                if total <= 1:
                    self.assertEqual(result, list(range(total)))
                    self.assertEqual(len(requests_seen), 1)
                    continue
                page_count = (total + 49) // 50
                expected_pages = random.Random(11).sample(range(page_count), min(5, page_count))
                self.assertEqual([params["offset"] for _, params in requests_seen[1:]],
                                 [page * 50 for page in expected_pages if page != 0])
                self.assertTrue(all(path == "/me/tracks" and params["market"] == "NL" and params["limit"] == 50
                                    for path, params in requests_seen[1:]))
                self.assertEqual(result, [item for page in expected_pages
                                          for item in range(page * 50, min((page + 1) * 50, total))])
                self.assertLessEqual(len(result), 250)
                self.assertLessEqual(len(requests_seen), 6)

    def test_random_liked_tracks_are_cached_for_the_day_and_refreshed_the_next_day(self):
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

    def test_failed_liked_track_fetch_does_not_cache_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            api.get = Mock(side_effect=[
                {"total": 1000, "items": list(range(50))},
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
        api.get = Mock(side_effect=[{"total": 100, "items": list(range(50))}, {"items": []}])
        with self.assertRaisesRegex(SpotifyError, "randomly chosen liked-track page"):
            api.liked_tracks(random.Random(1))

    def test_partial_sources_are_used_without_caching_and_target_reads_remain_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Spotify.__new__(Spotify)
            api.store = Store(os.path.join(directory, "cache.sqlite"))
            day = datetime.now().astimezone().date().isoformat()
            partial = [{"track": {"id": "first"}}]
            trace = []
            cases = (
                ("liked_tracks_cache", [
                    {"total": 1000, "items": list(range(50))},
                    {"items": partial},
                    SpotifyError("saved page failed"),
                ], lambda: api.daily_cached("liked_tracks_cache", lambda: api.liked_tracks(random.Random(1)), trace)),
            )
            for key, responses, fetch in cases:
                with self.subTest(key=key):
                    api.get = Mock(side_effect=responses)
                    with self.assertLogs("app.selection", level="WARNING") as logs:
                        result = get_api_items(key, fetch)
                    self.assertEqual(len(result), 1)
                    self.assertIn("page failed", "\n".join(logs.output))
                    self.assertIsNone(api.store.get_cached(key, day))
                    self.assertIn("Liked tracks: partially retrieved from Spotify during this refresh; not cached.",
                                  trace)

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
            api.store.set_many({"timezone": "Europe/Amsterdam"})
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
            api.store.set_many({"timezone": "Europe/Amsterdam"})
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
                trace = []
                with self.assertRaisesRegex(SpotifyError, "unavailable"):
                    api.followed_artists(trace)
                self.assertIn("0 retrieved from Spotify during this refresh; "
                              "no artists available because a Spotify request failed; "
                              "Spotify was queried during this refresh.", trace[0])
                for _ in range(4):
                    with self.assertRaisesRegex(SpotifyError, "unavailable"):
                        api.followed_artists()
                trace = []
                self.assertEqual(api.followed_artists(trace), [])
                self.assertEqual(trace, [
                    "Followed artists: 0 reused from cache; 0 retrieved from Spotify during this refresh; "
                    "daily request limit of 5 reached; no new Spotify request; no artists available; "
                    "no Spotify request was made during this refresh."
                ])
                self.assertEqual(api.get.call_count, 5)
                clock.now.return_value = datetime(2026, 1, 16, tzinfo=timezone.utc)
                with self.assertRaisesRegex(SpotifyError, "unavailable"):
                    api.followed_artists()
                self.assertEqual(api.get.call_count, 6)

    def test_followed_artist_daily_limit_does_not_repeat_previous_error(self):
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
                state = api.store.get("followed_artists_cache")
                self.assertNotIn("last_error", state)
                state["last_error"] = "previous error; Retry-After: '172'"
                api.store.set_many({"followed_artists_cache": state})

                clock.now.return_value = datetime(2026, 1, 15, 10, 5, tzinfo=timezone.utc)
                for _ in range(2):
                    trace = []
                    issues = []
                    artists = get_api_items("followed artists", lambda: api.followed_artists(trace), issues)
                    self.assertEqual(len(artists), 4)
                    self.assertEqual(trace, [
                        "Followed artists: 4 reused from cache; 0 retrieved from Spotify during this refresh; "
                        "daily request limit of 5 reached; no new Spotify request."
                    ])
                    self.assertEqual(issues, [])
                    self.assertNotIn("Retry-After", "\n".join(trace))
                self.assertEqual(api.get.call_count, 5)

                clock.now.return_value = datetime(2026, 1, 16, 10, 0, tzinfo=timezone.utc)
                self.assertEqual(len(api.followed_artists()), 5)
                self.assertEqual(api.get.call_count, 6)
                self.assertNotIn("last_error", api.store.get("followed_artists_cache"))

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
                request.return_value.json.return_value = {
                    "error": {"message": "Access denied", "reason": "MISSING_SCOPE"}
                }
                with self.assertRaisesRegex(SpotifyError, "reconnect Spotify.*followed artists") as error:
                    api.followed_artists()
                self.assertIn("Message: Access denied; Reason: MISSING_SCOPE", str(error.exception))

    def test_missing_library_permission_gives_reconnect_guidance(self):
        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.token = "token"
        with patch("app.spotify.requests.request") as request:
            request.return_value.ok = False
            request.return_value.status_code = 403
            with self.assertRaisesRegex(SpotifyError, "reconnect Spotify"):
                api.get("/me/tracks")

    def test_rate_limit_reports_retry_after_or_its_absence(self):
        api = Spotify.__new__(Spotify)
        api.store = self.store
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

    def test_api_error_details_ignore_invalid_or_unrelated_response_data(self):
        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.token = "token"
        with patch("app.spotify.requests.request") as request:
            response = request.return_value
            response.ok = False
            response.status_code = 400
            for body in ({"error": {"message": "Bad\nrequest", "reason": "INVALID_REQUEST"}},
                          {"error": {"message": [], "reason": 123}},
                          {"unexpected": "must not appear"}):
                with self.subTest(body=body):
                    response.json.return_value = body
                    with self.assertRaises(SpotifyError) as error:
                        api.get("/tracks/invalid")
                    if body.get("error", {}).get("reason") == "INVALID_REQUEST":
                        self.assertIn("Message: Bad request; Reason: INVALID_REQUEST", str(error.exception))
                    else:
                        self.assertNotIn("Reason:", str(error.exception))
                        self.assertNotIn("must not appear", str(error.exception))
            response.json.side_effect = ValueError("not JSON")
            with self.assertRaisesRegex(SpotifyError, r"HTTP 400"):
                api.get("/tracks/invalid")

    def test_api_error_does_not_expose_access_token(self):
        api = Spotify.__new__(Spotify)
        api.store = self.store
        api.token = "secret-access-token"
        with patch("app.spotify.requests.request") as request:
            request.return_value.ok = False
            request.return_value.status_code = 400
            request.return_value.json.return_value = {"error": {"message": "Invalid secret-access-token"}}
            with self.assertRaises(SpotifyError) as error:
                api.get("/tracks/invalid")
        self.assertIn("Message: Invalid [redacted]", str(error.exception))
        self.assertNotIn(api.token, str(error.exception))

    def test_token_error_reports_oauth_description(self):
        with patch("app.spotify.requests.post") as post:
            post.return_value.ok = False
            post.return_value.status_code = 400
            post.return_value.json.return_value = {
                "error": "invalid_grant", "error_description": "Credential secret-value expired",
                "unrelated": "do not log",
            }
            with self.assertRaises(SpotifyError) as error:
                token_request({"refresh_token": "secret-value"}, self.store)
        self.assertIn("Error: invalid_grant; Description: Credential [redacted] expired", str(error.exception))
        self.assertNotIn("secret-value", str(error.exception))
        self.assertNotIn("do not log", str(error.exception))

    def test_token_rate_limit_reports_retry_after(self):
        with patch("app.spotify.requests.post") as post:
            post.return_value.ok = False
            post.return_value.status_code = 429
            post.return_value.headers = {"Retry-After": "30"}
            with self.assertRaisesRegex(SpotifyError, "Retry-After: '30'"):
                token_request({}, self.store)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(os.path.join(self.temp.name, "db.sqlite"))
        self.store.set_many({"playlist_id": "target", "source_ids": []})
        self.service = Refresh(self.store)

    def test_request_stats_include_token_failures_cache_and_playlist_write(self):
        self.store.set_many({"client_id": "client", "refresh_token": "saved"})
        day = datetime.now().astimezone().date().isoformat()
        self.store.set_cached("liked_tracks_cache", day, [])
        self.store.set_cached("top_artists_time_range_cache", day, "short_term")
        token = Mock(ok=True, json=Mock(return_value={"access_token": "access"}))

        def request(method, url, **kwargs):
            if method == "PUT":
                return Mock(ok=True, content=b"")
            if url.endswith("/me/tracks"):
                return Mock(ok=False, status_code=429, headers={},
                            json=Mock(return_value={}), content=b"{}")
            return Mock(ok=True, content=b"{}", json=Mock(return_value={
                "items": [{"item": {"uri": "spotify:track:old"}}], "next": None,
            }))

        def build_with_cache(api, *args):
            self.assertEqual(api.daily_cached("liked_tracks_cache", lambda: ["unexpected"]), [])
            self.assertEqual(api.daily_cached("top_artists_time_range_cache", lambda: "long_term"), "short_term")
            with self.assertRaises(SpotifyRateLimitError):
                api.get("/me/tracks")
            return ["spotify:track:new"], [], "90 minutes"

        with patch("app.spotify.requests.post", return_value=token), \
             patch("app.spotify.requests.request", side_effect=request), \
             patch("app.refresh.build", side_effect=build_with_cache):
            self.service.run()

        entry = Store(self.store.path).history()[0]
        self.assertEqual(entry["status"], "succeeded")
        self.assertEqual((entry["api_requests"], entry["token_requests"], entry["cache_hits"]), (3, 1, 1))
        self.assertIsNone(current_refresh_stats.get())

    def test_request_stats_include_playlist_verification_after_failure(self):
        self.store.set_many({"client_id": "client", "refresh_token": "saved"})
        token = Mock(ok=True, json=Mock(return_value={"access_token": "access"}))
        playlist = Mock(ok=True, content=b"{}", json=Mock(return_value={
            "items": [{"item": {"uri": "spotify:track:old"}}], "next": None,
        }))
        with patch("app.spotify.requests.post", return_value=token), \
             patch("app.spotify.requests.request", side_effect=[
                 playlist, RequestException("connection lost"), playlist,
             ]), patch("app.refresh.build", return_value=(["spotify:track:new"], [], "90 minutes")), \
             self.assertLogs("app.refresh", level="ERROR"):
            with self.assertRaisesRegex(SpotifyError, "Spotify change was not applied"):
                self.service.run()
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual((entry["api_requests"], entry["token_requests"], entry["cache_hits"]), (3, 1, 0))

    def test_request_stats_include_failed_token_request(self):
        self.store.set_many({"client_id": "client", "refresh_token": "saved"})
        with patch("app.spotify.requests.post", side_effect=RequestException("token unavailable")), \
             self.assertLogs("app.refresh", level="ERROR"):
            with self.assertRaises(RequestException):
                self.service.run()
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual((entry["api_requests"], entry["token_requests"], entry["cache_hits"]), (0, 1, 0))

    def test_stats_storage_failure_does_not_change_successful_refresh(self):
        api = Mock()
        api.playlist_items.return_value = ["old"]
        with patch("app.refresh.Spotify", return_value=api), \
             patch("app.refresh.build", return_value=(["new"], [], "90 minutes")), \
             patch.object(self.store, "save_request_stats", side_effect=sqlite3.OperationalError("locked")), \
             self.assertLogs("app.refresh", level="ERROR") as logged:
            self.service.run()
        self.assertIn("Could not save refresh request statistics", "\n".join(logged.output))
        self.assertEqual(self.store.history()[0]["status"], "succeeded")
        self.assertIsNone(self.store.history()[0]["api_requests"])

    def test_existing_database_history_has_unknown_request_stats(self):
        path = os.path.join(self.temp.name, "old.sqlite")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, started TEXT NOT NULL, "
                       "status TEXT NOT NULL, detail TEXT NOT NULL, scheduled_key TEXT UNIQUE)")
            db.execute("INSERT INTO history (started, status, detail) VALUES (?, ?, ?)",
                       ("2026-01-01", "succeeded", "old"))
        entry = Store(path).history()[0]
        self.assertEqual(entry["detail"], "old")
        self.assertEqual((entry["api_requests"], entry["token_requests"], entry["cache_hits"]),
                         (None, None, None))

    def test_reconciliation_preserves_old_refresh_counts(self):
        self.store.set_many({"client_id": "client", "refresh_token": "saved"})
        old_id = self.store.start()
        self.store.prepare(old_id, "target", ["old"], ["previous"], [], "previous")
        self.store.finish(old_id, "uncertain", "unreachable")
        self.store.save_request_stats(old_id, RefreshRequestStats(5, 1, 2))
        token = Mock(ok=True, json=Mock(return_value={"access_token": "access"}))
        playlist = Mock(ok=True, content=b"{}", json=Mock(return_value={
            "items": [{"item": {"uri": "previous"}}], "next": None,
        }))
        with patch("app.spotify.requests.post", return_value=token), \
             patch("app.spotify.requests.request", side_effect=[playlist, playlist, Mock(ok=True, content=b"")]), \
             patch("app.refresh.build", return_value=(["new"], [], "new")):
            self.service.run()
        current, previous = self.store.history()
        self.assertEqual(previous["status"], "succeeded")
        self.assertEqual((previous["api_requests"], previous["token_requests"], previous["cache_hits"]),
                         (5, 1, 2))
        self.assertEqual((current["api_requests"], current["token_requests"], current["cache_hits"]),
                         (3, 1, 0))

    def test_concurrent_refreshes_keep_counts_separate(self):
        other = Store(os.path.join(self.temp.name, "other.sqlite"))
        for store in (self.store, other):
            store.set_many({"client_id": "client", "refresh_token": "saved", "playlist_id": "target"})
        day = datetime.now().astimezone().date().isoformat()
        self.store.set_cached("liked_tracks_cache", day, [])
        barrier = threading.Barrier(2)
        token = Mock(ok=True, json=Mock(return_value={"access_token": "access"}))

        def request(method, url, **kwargs):
            if method == "PUT":
                return Mock(ok=True, content=b"")
            return Mock(ok=True, content=b"{}", json=Mock(return_value={"items": [], "next": None}))

        def build_concurrently(api, *args):
            barrier.wait(timeout=5)
            if api.store.path == self.store.path:
                api.daily_cached("liked_tracks_cache", lambda: ["unexpected"])
                api.get("/me/tracks")
            return ["new"], [], "new"

        with patch("app.spotify.requests.post", return_value=token), \
             patch("app.spotify.requests.request", side_effect=request), \
             patch("app.refresh.build", side_effect=build_concurrently), \
             ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(service.run) for service in (self.service, Refresh(other))]
            for future in futures:
                future.result()
        first, second = self.store.history()[0], other.history()[0]
        self.assertEqual((first["api_requests"], first["token_requests"], first["cache_hits"]), (3, 1, 1))
        self.assertEqual((second["api_requests"], second["token_requests"], second["cache_hits"]), (2, 1, 0))

    def test_disabling_automatic_refresh_does_not_block_manual_refresh(self):
        self.store.set_many({"automatic_refresh_enabled": False})
        api = Mock()
        api.playlist_items.return_value = ["old"]
        with patch("app.refresh.Spotify", return_value=api) as spotify, \
             patch("app.refresh.build", return_value=(["new"], [], "90 minutes")):
            self.service.run("2026-01-01 08:15")
            spotify.assert_not_called()
            self.assertEqual(self.store.history(), [])

            self.service.run()
            self.assertEqual(len(self.store.history()), 1)
            api.replace.assert_called_once_with("target", ["new"])

            self.store.set_many({"automatic_refresh_enabled": True})
            self.service.run("2026-01-01 08:15")
            self.assertEqual(len(self.store.history()), 2)

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

    def test_failed_selection_preserves_cache_origins_in_history(self):
        class Fake(Spotify):
            def __init__(self, store):
                self.store = store

            def playlist_items(self, pid):
                return ["old"]

            def get(self, path, **params):
                if path == "/me/following":
                    return {"artists": {"items": [], "next": None}}
                return {"items": []}

            def liked_tracks(self, rng):
                return []

        with patch("app.refresh.Spotify", side_effect=Fake), self.assertLogs("app.refresh", level="ERROR"):
            for _ in range(2):
                with self.assertRaisesRegex(SpotifyError, "No suitable music found"):
                    self.service.run()

        first, second = sorted(self.store.history(), key=lambda entry: entry["started"])
        self.assertEqual(first["status"], "failed")
        for label in ("Top tracks short term", "Top tracks medium term", "Top tracks long term",
                      "Recently played tracks", "Liked tracks"):
            self.assertIn(f"{label}: retrieved from Spotify and saved in today's cache.", first["detail"])
            self.assertIn(f"{label}: retrieved from today's cache.", second["detail"])
        self.assertRegex(first["detail"], r"Top artists (short|medium|long) term: retrieved from Spotify")
        self.assertRegex(second["detail"], r"Top artists (short|medium|long) term: retrieved from (today's cache|Spotify)")
        self.assertIn("Followed artists: 0 reused from cache; 0 retrieved from Spotify during this refresh; "
                      "no artists available; Spotify was queried during this refresh.", first["detail"])
        self.assertIn("Followed artists: 0 reused from cache; 0 retrieved from Spotify during this refresh; "
                      "no artists available; no Spotify request was made during this refresh.", second["detail"])

    def test_uncertain_write_preserves_cache_origins_in_history(self):
        api = Mock()
        api.playlist_items.side_effect = [["old"], SpotifyError("unreachable")]
        api.replace.side_effect = TimeoutError("timeout")
        detail = ("90 minutes; 1 music tracks\nSelection:\n"
                  "Liked tracks: retrieved from today's cache.\nItems:\nSong")

        with patch("app.refresh.Spotify", return_value=api), patch(
                "app.refresh.build", return_value=(["new"], [], detail)), self.assertLogs(
                "app.refresh", level="ERROR"):
            with self.assertRaisesRegex(SpotifyError, "unreachable"):
                self.service.run()

        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "uncertain")
        self.assertIn("unreachable", entry["detail"])
        self.assertIn("\nSelection:\nLiked tracks: retrieved from today's cache.", entry["detail"])
        self.assertNotIn("\nItems:\n", entry["detail"])

        recovered = Mock()
        recovered.playlist_items.return_value = ["old"]
        self.assertFalse(self.service.reconcile(recovered))
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("Liked tracks: retrieved from today's cache.", entry["detail"])

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
        api.store = self.store
        api.token = "token"
        with patch("app.refresh.Spotify", return_value=api), patch.object(
                api, "playlist_items", return_value=["old"]), patch(
                "app.refresh.build", side_effect=lambda *args: api.get("/me/top/artists")), patch(
                "app.spotify.requests.request") as request:
            request.return_value.ok = False
            request.return_value.status_code = 429
            request.return_value.headers = {"Retry-After": "45"}
            request.return_value.json.return_value = {
                "error": {"message": "Quota exceeded", "reason": "QUOTA_EXCEEDED"}
            }
            with self.assertRaises(SpotifyError):
                self.service.run()
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("Retry-After: '45'", entry["detail"])
        self.assertIn("Message: Quota exceeded; Reason: QUOTA_EXCEEDED", entry["detail"])
        self.assertIn("/me/top/artists", entry["detail"])
        self.assertIsNone(self.store.pending())
        self.assertEqual(request.call_args.args[0], "GET")

    def test_successful_refresh_records_source_error_details(self):
        class Fake(Spotify):
            def __init__(self, store):
                self.store = store
                self.token = "token"

            def playlist_items(self, pid):
                return ["old"]

            def replace(self, pid, items):
                pass

            def get(self, path, **params):
                if path == "/me/top/tracks" and params["time_range"] == "short_term":
                    return super().get(path, **params)
                return {"items": []}

            def liked_tracks(self, rng):
                return []

            def followed_artists(self, trace=None):
                return []

            def pages(self, path, params, max_items=1000):
                return [{"item": {"id": str(i), "type": "track", "uri": f"spotify:track:{i}",
                                   "duration_ms": 240000, "is_playable": True}} for i in range(25)]

        self.store.set_many({"source_ids": ["source"]})
        with patch("app.refresh.Spotify", side_effect=Fake), patch(
                "app.spotify.requests.request") as request, self.assertLogs("app.selection", level="WARNING"):
            request.return_value.ok = False
            request.return_value.status_code = 429
            request.return_value.headers = {"Retry-After": "45"}
            request.return_value.json.return_value = {
                "error": {"message": "Quota exceeded", "reason": "QUOTA_EXCEEDED"}
            }
            self.service.run()
        entry = self.store.history()[0]
        self.assertEqual(entry["status"], "succeeded")
        self.assertIn("Source error: Skipping short-term top tracks", entry["detail"])
        self.assertIn("Top tracks short term: Spotify fetch failed; no data retrieved from cache; "
                      "0 results because the fetch failed.", entry["detail"])
        self.assertIn("current and recent: 0 (no candidates because source retrieval failed)", entry["detail"])
        self.assertIn("Message: Quota exceeded; Reason: QUOTA_EXCEEDED; Retry-After: '45'", entry["detail"])

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
        api.store = self.store
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
        api.store = self.store
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
        self.store.prepare(history_id, "target", ["old"], ["new"], ["episode"],
                           "detail\nSelection:\nLiked tracks: retrieved from today's cache.\nItems:\nSong")

        class Fake:
            def playlist_items(self, pid):
                return ["other"]

        with self.assertRaises(SpotifyError):
            self.service.reconcile(Fake())
        self.assertIsNotNone(self.store.pending())
        self.store.release_pending()
        self.assertIsNone(self.store.pending())
        self.assertEqual(self.store.history()[0]["status"], "uncertain")
        self.assertIn("Liked tracks: retrieved from today's cache.", self.store.history()[0]["detail"])

    def test_restart_records_interrupted_attempt(self):
        history_id = self.store.start()
        self.store.prepare(history_id, "target", ["old"], ["new"], ["episode"],
                           "detail\nSelection:\nLiked tracks: retrieved from today's cache.\nItems:\nSong")
        restored = Store(self.store.path)
        self.assertEqual(restored.history()[0]["status"], "uncertain")
        self.assertIn("Liked tracks: retrieved from today's cache.", restored.history()[0]["detail"])
        self.assertIsNotNone(restored.pending())


if __name__ == "__main__":
    unittest.main()
