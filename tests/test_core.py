import os
import random
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.refresh import Refresh
from app.selection import build, build_items, interleave, music_candidates, select_music, podcast_candidates
from app.spotify import Spotify, SpotifyError, authorization, callback_code, playlist_id, show_id
from app.store import Store

NEWS = "news"
PODCASTS = ["podcast1", "podcast2"]
NEWS_SHOWS = [{"show": NEWS, "count": 4}]
PODCAST_SHOWS = [{"show": show, "count": 1} for show in PODCASTS]


def track(number, minutes=4):
    return {"uri": f"spotify:track:{number}", "duration_ms": minutes * 60000}


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

    def test_long_podcasts_are_kept_with_music(self):
        podcasts = [{"uri": "spotify:episode:1", "duration_ms": 91 * 60000}]
        music = select_music([[track(1)], [], [], []], podcasts, random.Random(1))
        self.assertEqual(len(music), 1)

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

    def test_unavailable_configured_episodes_fail(self):
        class Fake:
            def get(self, path, **params):
                return {"items": [], "next": None}
        with self.assertRaisesRegex(SpotifyError, "Not enough playable episodes"):
            podcast_candidates(Fake(), NEWS_SHOWS, [])

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

    def test_episode_count_leaves_room_for_one_music_track(self):
        podcasts = [{"uri": f"spotify:episode:{i}", "duration_ms": 60000} for i in range(99)]
        music = select_music([[track(1)], [track(2)]], podcasts, random.Random(1))
        self.assertEqual(len(music), 1)

    def test_build_uses_six_episodes_and_all_music_sources(self):
        def song(number):
            return {"type": "track", "id": str(number), "uri": f"spotify:track:{number}",
                    "duration_ms": 4 * 60000, "is_playable": True}

        class Fake:
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
                    return {"items": [{"id": "album", "release_date": "2026-01-01"}]}
                if path == "/albums/album/tracks":
                    return {"items": [song(i) for i in range(20, 45)]}
                raise AssertionError(path)
            def pages(self, path, params, max_items=1000):
                if path == "/me/tracks":
                    return [{"track": song(i)} for i in range(50, 55)]
                return [{"item": song(i)} for i in range(45, 50)]

        items, episodes, detail = build(Fake(), ["source"], NEWS_SHOWS, PODCAST_SHOWS, random.Random(7))
        self.assertEqual(len(episodes), 6)
        self.assertEqual(len(items), len(set(items)))
        self.assertTrue(set(episodes).issubset(items))
        self.assertIn("minutes", detail)
        self.assertLessEqual(abs((len(items) - 6) * 4 + 42 - 90), 2)

    def test_liked_tracks_are_filtered_and_can_fill_the_mix(self):
        def song(number, playable=True):
            return {"type": "track", "id": str(number), "uri": f"spotify:track:{number}",
                    "duration_ms": 4 * 60000, "is_playable": playable}

        class Fake:
            def get(self, path, **params):
                return {"items": []}

            def pages(self, path, params, max_items=1000):
                self.request = (path, params, max_items)
                return ([{"track": song(i)} for i in range(30)] +
                        [{"track": song(99, False)}, {"track": None}])

        api = Fake()
        groups = music_candidates(api, [], random.Random(2))
        self.assertEqual(api.request, ("/me/tracks", {"market": "NL"}, None))
        self.assertEqual(len(groups[-1]), 30)
        podcasts = [{"uri": f"spotify:episode:{i}", "duration_ms": 7 * 60000} for i in range(5)]
        selected = select_music(groups, podcasts, random.Random(3))
        self.assertTrue(selected)
        self.assertTrue(all(item in groups[-1] for item in selected))
        self.assertLessEqual(abs(sum(i["duration_ms"] for i in selected + podcasts) - 90 * 60000), 2 * 60000)


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
        self.assertEqual(callback_code("http://127.0.0.1:8888/callback?code=abc&state=" + state, state), "abc")
        with self.assertRaises(ValueError):
            callback_code("http://127.0.0.1:8888/callback?code=abc&state=wrong", state)

    def test_saved_tracks_pagination_accepts_more_than_1000(self):
        api = Spotify.__new__(Spotify)
        def get(path, **params):
            offset = params["offset"]
            return {"items": list(range(offset, offset + 50)),
                    "next": "next" if offset < 1000 else None}
        api.get = get
        self.assertEqual(len(api.pages("/me/tracks", max_items=None)), 1050)
        with self.assertRaises(SpotifyError):
            api.pages("/me/tracks")

    def test_missing_library_permission_gives_reconnect_guidance(self):
        api = Spotify.__new__(Spotify)
        api.token = "token"
        with patch("app.spotify.requests.request") as request:
            request.return_value.ok = False
            request.return_value.status_code = 403
            with self.assertRaisesRegex(SpotifyError, "reconnect Spotify"):
                api.get("/me/tracks")


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
        class Recovered:
            def playlist_items(self, pid):
                return ["new"]
        self.assertTrue(self.service.reconcile(Recovered()))
        self.assertEqual(self.store.history()[0]["status"], "succeeded")
        self.assertEqual(self.store.used_episodes(), {"episode"})

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
