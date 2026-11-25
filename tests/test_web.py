import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import call, patch
from zoneinfo import ZoneInfo

# The module normally writes to Home Assistant's private /data directory.
temp = tempfile.TemporaryDirectory()
os.environ["DAILY_DRIVE_DB"] = os.path.join(temp.name, "web.sqlite")
from app import web


class WebTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        self.remote = {"REMOTE_ADDR": "172.30.32.2"}

    def test_ingress_and_csrf_required(self):
        self.assertEqual(self.client.get("/").status_code, 403)
        response = self.client.get("/", environ_overrides=self.remote)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<html lang="en">', response.data)
        self.assertIn(b"Connect Spotify", response.data)
        self.assertIn(b"00:00, 04:00, 08:00, 12:00, 16:00, 20:00", response.data)
        self.assertEqual(self.client.post("/refresh", environ_overrides=self.remote).status_code, 403)

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

    def test_scheduler_runs_six_times_in_amsterdam_time(self):
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
                    for hour in (0, 4, 8, 12, 16, 20)
                ])
                self.assertEqual(clock.now.call_count, 26)
                self.assertTrue(all(c.args[0].key == "Europe/Amsterdam" for c in clock.now.call_args_list))


if __name__ == "__main__":
    unittest.main()
