import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import call, patch
from zoneinfo import ZoneInfo

# The module normally writes to Home Assistant's private /data directory.
temp = tempfile.TemporaryDirectory()
os.environ["DAILY_DRIVE_DB"] = os.path.join(temp.name, "web.sqlite")
from app import web
from app.store import Store


class WebTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        self.remote = {"REMOTE_ADDR": "172.30.32.2"}

    def isolated_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(os.path.join(directory.name, "storage.sqlite"))
        patcher = patch.object(web, "store", store)
        patcher.start()
        self.addCleanup(patcher.stop)
        return store

    def test_ingress_and_csrf_required(self):
        self.assertEqual(self.client.get("/").status_code, 403)
        response = self.client.get("/", environ_overrides=self.remote)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<html lang="en">', response.data)
        self.assertIn(b"Connect Spotify", response.data)
        self.assertIn(b"06:00, 10:00, 14:00, 18:00, 22:00", response.data)
        self.assertEqual(self.client.post("/refresh", environ_overrides=self.remote).status_code, 403)

    def test_history_displays_rate_limit_retry_after(self):
        entry = {"started": "2026-01-01", "status": "failed",
                 "detail": "Spotify GET /me/top/artists failed (HTTP 429); Retry-After: '45'"}
        with patch.object(web.store, "history", return_value=[entry]):
            response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b"HTTP 429", response.data)
        self.assertIn(b"Retry-After: &#39;45&#39;", response.data)

    def test_configuration_accepts_urls_and_rejects_target_as_source(self):
        pid = "0123456789ABCDEFGHIJKL"
        data = {
            "csrf": web.csrf, "client_id": "a" * 32,
            "target": "https://open.spotify.com/playlist/" + pid, "sources": "",
        }
        response = self.client.post("/configure", data=data, environ_overrides=self.remote)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(web.store.get("playlist_id"), pid)

        data["sources"] = data["target"]
        response = self.client.post("/configure", data=data, environ_overrides=self.remote)
        self.assertEqual(response.status_code, 400)

    def test_show_configuration_persists_and_rejects_invalid_entries(self):
        news = "1" * 22
        podcast = "2" * 22
        data = {
            "csrf": web.csrf, "client_id": "a" * 32, "target": "0" * 22, "sources": "",
            "news_shows": f"https://open.spotify.com/show/{news}?si=test, 4",
            "podcast_shows": f"spotify:show:{podcast}, 1",
        }
        response = self.client.post("/configure", data=data, environ_overrides=self.remote)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(web.store.get("news_shows"), [{"show": news, "count": 4}])
        self.assertEqual(web.store.get("podcast_shows"), [{"show": podcast, "count": 1}])
        self.assertIn(f"https://open.spotify.com/show/{news}, 4".encode(), response.data)

        for invalid in (f"{news}, 1", f"{podcast}, 0", f"{podcast}, 96", f"{podcast}"):
            with self.subTest(invalid=invalid):
                data["podcast_shows"] = invalid
                response = self.client.post("/configure", data=data, environ_overrides=self.remote)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(web.store.get("podcast_shows"), [{"show": podcast, "count": 1}])

        data["news_shows"] = data["podcast_shows"] = ""
        response = self.client.post("/configure", data=data, environ_overrides=self.remote)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"refreshes will contain only music", response.data)

    def test_manual_refresh_reports_running_job(self):
        web.refresh.lock.acquire()
        try:
            response = self.client.post("/refresh", data={"csrf": web.csrf}, environ_overrides=self.remote)
        finally:
            web.refresh.lock.release()

        self.assertEqual(response.status_code, 409)

    def test_reconnecting_clears_cache_and_waits_for_refresh(self):
        web.store.set_many({"client_id": "a" * 32})
        web.store.set_cached("liked_tracks_cache", "2026-01-01", [{"track": "old"}])
        web.store.set_many({"followed_artists_cache": {"artists": {"old": {"id": "old"}}}})
        web.oauth.update(state="state", client_id="a" * 32, verifier="verifier", created=time.monotonic())
        data = {"csrf": web.csrf, "callback": "http://127.0.0.1:8888/callback?code=abc&state=state"}
        # Connecting must wait until the active refresh releases the account lock.
        web.refresh.lock.acquire()
        try:
            response = self.client.post("/callback", data=data, environ_overrides=self.remote)
        finally:
            web.refresh.lock.release()
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(web.store.get_cached("liked_tracks_cache", "2026-01-01"))
        self.assertIsNotNone(web.store.get("followed_artists_cache"))
        with patch("app.web.token_request", return_value={"refresh_token": "new"}):
            response = self.client.post("/callback", data=data, environ_overrides=self.remote)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(web.store.get_cached("liked_tracks_cache", "2026-01-01"))
        self.assertIsNone(web.store.get("followed_artists_cache"))

    def test_changing_client_id_clears_cache(self):
        web.store.set_many({"client_id": "a" * 32})
        web.store.set_cached("liked_tracks_cache", "2026-01-01", [])
        web.store.set_many({"followed_artists_cache": {"artists": {}}})
        data = {"csrf": web.csrf, "client_id": "b" * 32, "target": "0" * 22, "sources": ""}
        response = self.client.post("/configure", data=data, environ_overrides=self.remote)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(web.store.get_cached("liked_tracks_cache", "2026-01-01"))
        self.assertIsNone(web.store.get("followed_artists_cache"))

    def test_storage_size_and_confirmed_clear_preserve_settings(self):
        store = self.isolated_store()
        store.set_many({"client_id": "client", "refresh_token": "token", "playlist_id": "target",
                        "followed_artists_cache": {"artists": {"artist": "x" * 100000}}})
        store.set_cached("liked_tracks_cache", "2026-01-01", ["x" * 100000])
        history_id = store.start()
        store.prepare(history_id, "target", ["old"], ["new"], ["episode"], "detail")
        store.resolve(True)
        before_size = os.path.getsize(store.path)

        page = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(f"SQLite database: {before_size} bytes".encode(), page.data)
        self.assertIn(b"does not change the Spotify playlist", page.data)
        response = self.client.post("/clear-storage", data={"csrf": web.csrf, "confirm": "on"},
                                    environ_overrides=self.remote)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Stored data cleared", response.data)
        self.assertEqual(store.get("client_id"), "client")
        self.assertEqual(store.get("refresh_token"), "token")
        self.assertEqual(store.get("playlist_id"), "target")
        self.assertIsNone(store.get("followed_artists_cache"))
        self.assertIsNone(store.get_cached("liked_tracks_cache", "2026-01-01"))
        self.assertEqual(store.history(), [])
        self.assertEqual(store.used_episodes(), set())
        self.assertLess(os.path.getsize(store.path), before_size)
        self.assertIn(f"SQLite database: {os.path.getsize(store.path)} bytes".encode(), response.data)

    def test_clear_storage_requires_confirmation_and_no_active_refresh(self):
        store = self.isolated_store()
        store.set_cached("liked_tracks_cache", "2026-01-01", ["track"])
        self.assertEqual(self.client.post("/clear-storage", data={"confirm": "on"},
                                          environ_overrides=self.remote).status_code, 403)
        self.assertEqual(self.client.post("/clear-storage", data={"csrf": web.csrf},
                                          environ_overrides=self.remote).status_code, 400)
        web.refresh.lock.acquire()
        try:
            response = self.client.post("/clear-storage", data={"csrf": web.csrf, "confirm": "on"},
                                        environ_overrides=self.remote)
        finally:
            web.refresh.lock.release()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(store.get_cached("liked_tracks_cache", "2026-01-01"), ["track"])

    def test_clear_storage_keeps_pending_change_and_history(self):
        store = self.isolated_store()
        store.set_cached("liked_tracks_cache", "2026-01-01", ["track"])
        history_id = store.start()
        store.prepare(history_id, "target", ["old"], ["new"], [], "detail")

        response = self.client.post("/clear-storage", data={"csrf": web.csrf, "confirm": "on"},
                                    environ_overrides=self.remote)
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"Resolve the uncertain playlist change", response.data)
        self.assertIsNotNone(store.pending())
        self.assertEqual(len(store.history()), 1)
        self.assertEqual(store.get_cached("liked_tracks_cache", "2026-01-01"), ["track"])

    def test_clear_storage_reports_compaction_failure(self):
        store = self.isolated_store()
        store.set_cached("liked_tracks_cache", "2026-01-01", ["track"])
        with patch.object(store, "compact", side_effect=sqlite3.OperationalError("disk full")), \
             self.assertLogs("app.web", level="ERROR"):
            response = self.client.post("/clear-storage", data={"csrf": web.csrf, "confirm": "on"},
                                        environ_overrides=self.remote)
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"Data was cleared, but storage could not be reclaimed", response.data)
        self.assertIsNone(store.get_cached("liked_tracks_cache", "2026-01-01"))

    def test_clear_storage_rolls_back_if_deletion_fails(self):
        store = self.isolated_store()
        store.set_many({"followed_artists_cache": {"artists": {"artist": {}}}})
        store.set_cached("liked_tracks_cache", "2026-01-01", ["track"])
        history_id = store.start()
        store.prepare(history_id, "target", ["old"], ["new"], ["episode"], "detail")
        store.resolve(True)
        with store.connect() as db:
            db.execute("""
                CREATE TRIGGER reject_episode_deletion BEFORE DELETE ON episodes
                BEGIN SELECT RAISE(ABORT, 'delete failed'); END
            """)

        with self.assertLogs("app.web", level="ERROR"):
            response = self.client.post("/clear-storage", data={"csrf": web.csrf, "confirm": "on"},
                                        environ_overrides=self.remote)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(store.get_cached("liked_tracks_cache", "2026-01-01"), ["track"])
        self.assertIsNotNone(store.get("followed_artists_cache"))
        self.assertEqual(store.used_episodes(), {"episode"})
        self.assertEqual(len(store.history()), 1)

    def test_scheduler_runs_five_times_in_amsterdam_time(self):
        amsterdam = ZoneInfo("Europe/Amsterdam")

        for date in ((2026, 1, 15), (2026, 7, 15)):
            with self.subTest(date=date):
                local_times = [datetime(*date, hour, 0, 0, tzinfo=amsterdam) for hour in range(24)]
                local_times += [
                    datetime(*date, 8, 1, tzinfo=amsterdam),
                    datetime(*date, 20, 59, tzinfo=amsterdam),
                ]
                utc_times = [value.astimezone(timezone.utc) for value in local_times]

                with patch("app.web.datetime") as clock, patch("app.web.run_refresh") as run, \
                     patch("app.web.time.sleep", side_effect=[None] * 25 + [StopIteration]):
                    clock.now.side_effect = lambda tz: utc_times.pop(0).astimezone(tz)
                    with self.assertRaises(StopIteration):
                        web.scheduler()

                self.assertEqual(run.call_args_list, [
                    call(f"{date[0]:04}-{date[1]:02}-{date[2]:02} {hour:02}:00")
                    for hour in (6, 10, 14, 18, 22)
                ])
                self.assertEqual(clock.now.call_count, 26)
                self.assertTrue(all(c.args[0].key == "Europe/Amsterdam" for c in clock.now.call_args_list))


if __name__ == "__main__":
    unittest.main()
