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
from app.request_stats import RefreshRequestStats
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
        self.assertIn(b"08:15, 12:15, 16:15, 20:15", response.data)
        self.assertEqual(self.client.post("/refresh", environ_overrides=self.remote).status_code, 403)

    def test_automatic_refresh_can_be_toggled_and_is_persistent(self):
        store = self.isolated_store()
        store.set_many({"client_id": "a" * 32, "refresh_token": "saved"})
        response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b"Automatic refresh is enabled", response.data)
        self.assertIn(b"Disable automatic refresh", response.data)
        self.assertLess(response.data.index(b"<h2>Automatic refresh</h2>"),
                        response.data.index(b"<h2>Settings</h2>"))

        web.refresh.lock.acquire()
        try:
            response = self.client.post(
                "/automatic-refresh", data={"csrf": web.csrf, "enabled": "off"},
                environ_overrides=self.remote,
            )
        finally:
            web.refresh.lock.release()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Automatic refresh is disabled", response.data)
        self.assertIn(b"Enable automatic refresh", response.data)
        self.assertFalse(Store(store.path).automatic_refresh_enabled())
        self.assertEqual(store.get("refresh_token"), "saved")

        response = self.client.post(
            "/automatic-refresh", data={"csrf": web.csrf, "enabled": "invalid"},
            environ_overrides=self.remote,
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(store.automatic_refresh_enabled())
        self.assertEqual(self.client.post("/automatic-refresh", data={"enabled": "on"},
                                          environ_overrides=self.remote).status_code, 403)

        response = self.client.post(
            "/automatic-refresh", data={"csrf": web.csrf, "enabled": "on"},
            environ_overrides=self.remote,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Store(store.path).automatic_refresh_enabled())

    def test_schedule_can_be_changed_without_clearing_existing_data(self):
        store = self.isolated_store()
        store.set_many({"client_id": "client", "refresh_token": "saved"})
        store.set_cached("liked_tracks_cache", "2026-01-01", ["track"])
        response = self.client.post(
            "/refresh-times", data={"csrf": web.csrf, "refresh_times": " 20:05\n8:15\n00:00\n8:15\n"},
            environ_overrides=self.remote,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"00:00, 08:15, 20:05", response.data)
        self.assertEqual(Store(store.path).automatic_refresh_times(), ["00:00", "08:15", "20:05"])
        self.assertEqual(store.get("refresh_token"), "saved")
        self.assertEqual(store.get_cached("liked_tracks_cache", "2026-01-01"), ["track"])
        self.assertTrue(store.automatic_refresh_enabled())
        self.assertEqual(self.client.post("/refresh-times", data={"refresh_times": "09:00"},
                                          environ_overrides=self.remote).status_code, 403)
        store.set_many({"automatic_refresh_enabled": False})
        response = self.client.post(
            "/refresh-times", data={"csrf": web.csrf, "refresh_times": "07:00"},
            environ_overrides=self.remote,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(store.automatic_refresh_enabled())
        self.assertEqual(Store(store.path).automatic_refresh_times(), ["07:00"])

    def test_invalid_schedule_is_rejected_without_changing_existing_times(self):
        store = self.isolated_store()
        defaults = ["08:15", "12:15", "16:15", "20:15"]
        self.assertEqual(store.automatic_refresh_times(), defaults)
        for invalid in ("", "24:00", "12:60", "09:00\ninvalid", "12:3", "１２:３０"):
            with self.subTest(invalid=invalid):
                response = self.client.post(
                    "/refresh-times", data={"csrf": web.csrf, "refresh_times": invalid},
                    environ_overrides=self.remote,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(Store(store.path).automatic_refresh_times(), defaults)
                self.assertIn(b"Enter at least one valid time", response.data)
                self.assertIn(b'<details class="schedule-editor" open>', response.data)
                self.assertIn(invalid.encode(), response.data)

    def test_schedule_write_error_is_reported_without_losing_input(self):
        store = self.isolated_store()
        with patch.object(store, "set_many", side_effect=sqlite3.OperationalError("locked")), \
             self.assertLogs("app.web", level="ERROR"):
            response = self.client.post(
                "/refresh-times", data={"csrf": web.csrf, "refresh_times": "09:30"},
                environ_overrides=self.remote,
            )
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"Could not save refresh times", response.data)
        self.assertIn(b'<details class="schedule-editor" open>', response.data)
        self.assertIn(b"09:30", response.data)
        self.assertEqual(store.automatic_refresh_times(), ["08:15", "12:15", "16:15", "20:15"])

    def test_request_overview_shows_daily_counts_and_first_quota_between_sections(self):
        store = self.isolated_store()
        started = datetime.now().astimezone()
        store.start_request("Liked tracks", started, "GET")
        quota = store.start_request("Artist releases", started, "Read; artist ID artist123; offset 50")
        store.fail_request(quota, quota=True)
        store.start_request("Artist releases", started, "GET")

        response = self.client.get("/", environ_overrides=self.remote)
        html = response.data.decode()
        self.assertLess(html.index('id="history-heading"'), html.index('id="requests-heading"'))
        self.assertLess(html.index('id="requests-heading"'), html.index("<h2>Storage</h2>"))
        self.assertIn("<span>Requests</span><strong>3</strong>", html)
        self.assertIn("<span>Errors</span><strong>1</strong>", html)
        self.assertIn("<td>Liked tracks</td><td>1</td><td>0</td>", html)
        self.assertIn("<td>Artist releases</td><td>2</td><td>1</td>", html)
        self.assertIn("request 2 of the day", html)
        self.assertIn("request 1 for\nArtist releases", html)
        self.assertIn(started.strftime("%H:%M:%S"), html)
        self.assertIn("Read; artist ID artist123; offset 50", html)

    def test_request_overview_empty_day(self):
        self.isolated_store()
        response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b"No requests recorded today.", response.data)
        self.assertIn(b"No quota or rate-limit error recorded today.", response.data)

    def test_settings_disclosure_keeps_setup_and_connection_flow_accessible(self):
        store = self.isolated_store()
        page = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b'<details class="panel disclosure settings" open>', page.data)
        self.assertIn(b"No refreshes yet.", page.data)

        store.set_many({"client_id": "a" * 32, "refresh_token": "saved", "playlist_id": "0" * 22})
        page = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b'<details class="panel disclosure settings" >', page.data)
        self.assertIn(b'<details class="panel disclosure storage">', page.data)
        self.assertEqual(store.get("refresh_token"), "saved")

        page = self.client.post("/configure", data={"csrf": web.csrf, "client_id": "invalid"},
                                environ_overrides=self.remote)
        self.assertEqual(page.status_code, 400)
        self.assertIn(b'<details class="panel disclosure settings" open>', page.data)
        with patch.dict(web.oauth, {"url": "https://accounts.spotify.com/authorize"}, clear=True):
            page = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b'<details class="panel disclosure settings" open>', page.data)
        self.assertIn(b"Complete connection", page.data)

    def test_stylesheet_is_included_with_or_without_ingress_script_name(self):
        for script_name in ("", "/api/hassio_ingress/example"):
            with self.subTest(script_name=script_name):
                page = self.client.get("/", environ_overrides={
                    **self.remote, "SCRIPT_NAME": script_name,
                    "HTTP_X_INGRESS_PATH": "/api/hassio_ingress/example",
                })
                self.assertEqual(page.status_code, 200)
                self.assertIn(b"<style>", page.data)
                self.assertIn(b".control-panel", page.data)
                self.assertNotIn(b'rel="stylesheet"', page.data)

    def test_request_overview_displays_artist_release_requests(self):
        store = self.isolated_store()
        started = datetime.now().astimezone()
        info = store.start_request("Release tracks", started, "Read; artist release ID album123")
        store.fail_request(info, quota=True)
        response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b"<td>Artist release tracks</td><td>1</td><td>1</td>", response.data)
        self.assertIn(b"artist release ID album123", response.data)
        self.assertNotIn(b"<td>Release tracks</td>", response.data)

    def test_history_shows_latest_and_loads_older_entries_in_batches(self):
        store = self.isolated_store()
        for number in range(1, 26):
            store.finish(store.start(), "failed", f"refresh-{number:03d}-done")

        for pages, expected, has_more in ((1, 10, True), (2, 20, True), (3, 25, False)):
            with self.subTest(pages=pages):
                url = "/" if pages == 1 else f"/?history_pages={pages}"
                html = self.client.get(url, environ_overrides=self.remote).data.decode()
                disclosure = '<details id="earlier-refreshes" class="history-older"'
                self.assertIn(disclosure + (">" if pages == 1 else " open>"), html)
                latest, older = html.split(disclosure, 1)
                self.assertIn("refresh-025-done", latest)
                self.assertNotIn("refresh-024-done", latest)
                for number in range(1, 25):
                    self.assertEqual(f"refresh-{number:03d}-done" in older, number > 25 - expected)
                self.assertEqual(
                    [html.index(f"refresh-{number:03d}-done") for number in range(25, 25 - expected, -1)],
                    sorted(html.index(f"refresh-{number:03d}-done") for number in range(25, 25 - expected, -1)),
                )
                self.assertEqual(html.count('<tr class="history-failed"'), expected)
                self.assertEqual("Load more</a>" in html, has_more)
                if has_more:
                    self.assertIn(f'href="./?history_pages={pages + 1}#history-entry-{expected}"', html)
                if pages > 1:
                    self.assertIn(f'id="history-entry-{(pages - 1) * 10}"', older)
        self.assertEqual(len(store.history(26)), 25)

    def test_history_pagination_boundaries_and_ingress(self):
        store = self.isolated_store()
        html = self.client.get("/", environ_overrides=self.remote).data
        self.assertIn(b"No refreshes yet.", html)
        self.assertNotIn(b"Earlier refreshes", html)

        for number in range(1, 21):
            store.finish(store.start(), "failed", f"entry {number}")
            if number in (1, 10, 11, 20):
                response = self.client.get("/", environ_overrides={
                    **self.remote, "SCRIPT_NAME": "/api/hassio_ingress/example",
                })
                self.assertEqual(b"Earlier refreshes" in response.data, number > 1)
                self.assertEqual(b"Load more</a>" in response.data, number > 10)
        response = self.client.get("/?history_pages=2", environ_overrides=self.remote)
        self.assertNotIn(b"Load more</a>", response.data)
        for invalid in ("0", "-1", "abc", "123456789012345678"):
            self.assertEqual(
                self.client.get(f"/?history_pages={invalid}", environ_overrides=self.remote).status_code, 400,
            )

    def test_loaded_history_keeps_error_links_and_request_counts(self):
        store = self.isolated_store()
        entry = store.start()
        store.finish(
            entry, "succeeded",
            "older refresh\nSelection:\n"
            "Top tracks short term: 0 results because the fetch failed.\n"
            "Source error: Skipping short-term top tracks: unavailable",
        )
        store.save_request_stats(entry, RefreshRequestStats(5, 1, 2))
        for _ in range(10):
            store.finish(store.start(), "failed", "newer refresh")

        html = self.client.get("/?history_pages=2", environ_overrides=self.remote).data.decode()
        self.assertIn('href="#history-10-error-1"', html)
        self.assertIn('id="history-10-error-1"', html)
        self.assertIn("Spotify API requests: 5;\ntoken requests: 1; cache hits: 2.", html)

    def test_history_links_source_playlist_error_to_matching_playlist(self):
        entry = {"started": "2026-01-01", "status": "succeeded",
                 "detail": "90 minutes\nSelection:\n"
                           "Source playlist AbC: Spotify fetch failed; no data retrieved from cache; "
                           "0 results because the fetch failed.\n"
                           "Source error: Skipping playlist Other: unavailable\n"
                           "Source error: Skipping playlist AbC: unavailable"}
        with patch.object(web.store, "history", return_value=[entry]):
            html = self.client.get("/", environ_overrides=self.remote).data.decode()
        self.assertIn('<a href="#history-0-error-2">0 results because the fetch failed</a>', html)

    def test_history_displays_rate_limit_retry_after(self):
        entry = {"started": "2026-01-01", "status": "failed",
                 "detail": "Spotify GET /me/top/artists failed (HTTP 429); "
                           "Message: quota <exceeded>; Reason: QUOTA_EXCEEDED; Retry-After: '45'"}
        with patch.object(web.store, "history", return_value=[entry]):
            response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b"HTTP 429", response.data)
        self.assertIn(b"Message: quota &lt;exceeded&gt;; Reason: QUOTA_EXCEEDED", response.data)
        self.assertIn(b"Retry-After: &#39;45&#39;", response.data)

    def test_history_shows_available_request_stats_without_inventing_missing_counts(self):
        store = self.isolated_store()
        store.finish(store.start(), "failed", "failed entry")
        entry_id = store.start()
        store.finish(entry_id, "succeeded", "new entry\nSelection:\nLiked tracks: cached.")
        store.save_request_stats(entry_id, RefreshRequestStats(12, 1, 3))
        store.start()

        html = self.client.get("/", environ_overrides=self.remote).data.decode()
        self.assertIn("Spotify API requests: 12;\ntoken requests: 1; cache hits: 3.", html)
        self.assertLess(html.index("Spotify API requests: 12;"),
                        html.index("<details><summary>How this playlist was selected</summary>"))
        self.assertEqual(html.count("Spotify API requests:"), 1)
        self.assertIn("failed entry", html)

    def test_failed_history_displays_cache_origins(self):
        entry = {"started": "2026-01-01", "status": "failed",
                 "detail": "No suitable music found\nSelection:\n"
                           "Liked tracks: retrieved from today\'s cache.\n"
                           "Recently played tracks: Spotify fetch failed; no data retrieved from cache."}
        with patch.object(web.store, "history", return_value=[entry]):
            response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b"No suitable music found", response.data)
        self.assertIn(b"Liked tracks: retrieved from today&#39;s cache.", response.data)
        self.assertIn(b"Recently played tracks: Spotify fetch failed; no data retrieved from cache.", response.data)

    def test_history_displays_cached_sources_and_current_errors(self):
        entry = {"started": "2026-01-01", "status": "succeeded",
                 "detail": "90 minutes\nSelection:\n"
                           "News show: 1 previously fetched episode reused from today's cache.\n"
                           "Liked tracks: Spotify fetch failed; reused cache from 2025-12-31.\n"
                           "Source error: Spotify failed (HTTP 429); Retry-After: '45'"}
        with patch.object(web.store, "history", return_value=[entry]):
            response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b"News show: 1 previously fetched episode reused from today&#39;s cache.", response.data)
        self.assertIn(b"Liked tracks: Spotify fetch failed; reused cache from 2025-12-31.", response.data)
        self.assertIn(b"HTTP 429", response.data)
        self.assertIn(b"Retry-After: &#39;45&#39;", response.data)

    def test_successful_history_status_distinguishes_errors_and_episode_shortfalls(self):
        store = self.isolated_store()
        entries = [
            ("succeeded", "clean\nSelection:\nSource warning: Show news has 0 of 1 requested playable episodes"),
            ("succeeded", "single\nSelection:\n"
             "Refresh outcome: error (news source error; no news selected; playlist contains music)\n"
             "Source error: Failed to fetch remaining episodes for show news: unavailable\n"
             "Source warning: Show news has 0 of 1 requested playable episodes"),
            ("succeeded", "multiple\nSelection:\n"
             "Refresh outcome: error (music source error; playlist contains music)\n"
             "Source error: Skipping short-term top tracks: unavailable\n"
             "Source error: Skipping artist releases for artist favorite: unavailable "
             "(during an earlier discovery fetch today)"),
            ("failed", "failed detail\nSelection:\nSource error: Skipping liked tracks: unavailable"),
            ("uncertain", "uncertain detail\nSelection:\nSource error: Skipping liked tracks: unavailable"),
        ]
        for status, detail in entries:
            store.finish(store.start(), status, detail)

        response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b"<td>succeeded</td><td>clean", response.data)
        self.assertIn(b"<td>error (news source error; no news selected; playlist contains music)</td><td>single",
                      response.data)
        self.assertIn(b"<td>error (music source error; playlist contains music)</td><td>multiple", response.data)
        self.assertIn(b"<td>failed</td><td>failed detail", response.data)
        self.assertIn(b"<td>uncertain</td><td>uncertain detail", response.data)
        self.assertIn(b"Source warning: Show news has 0 of 1 requested playable episodes", response.data)
        self.assertIn(b"Source error: Skipping short-term top tracks: unavailable", response.data)
        self.assertNotIn(b"Source warning: Skipping short-term top tracks", response.data)
        self.assertTrue(all(entry["status"] in ("succeeded", "failed", "uncertain")
                            for entry in Store(store.path).history()))

    def test_history_shows_recorded_outcome_without_masking_failed_or_uncertain(self):
        store = self.isolated_store()
        outcome = ("Refresh outcome: error (news source error; cached results retrieved; "
                   "podcast source error; no podcast selected; playlist contains music, news)")
        for status in ("succeeded", "failed", "uncertain"):
            store.finish(store.start(), status, f"detail {status}\nSelection:\n{outcome}\n"
                         "Source error: Failed to fetch remaining episodes for show news: offline")
        html = self.client.get("/", environ_overrides=self.remote).data.decode()
        self.assertIn(f'<td>{outcome.removeprefix("Refresh outcome: ")}</td><td>detail succeeded', html)
        self.assertIn('<td>failed</td><td>detail failed', html)
        self.assertIn('<td>uncertain</td><td>detail uncertain', html)
        self.assertNotIn("Refresh outcome:", html)

    def test_confirmed_playlist_retains_recorded_error_outcome(self):
        store = self.isolated_store()
        entry = store.start()
        detail = ("90 minutes\nSelection:\n"
                  "Refresh outcome: error (news source error; cached results retrieved; playlist contains news)\n"
                  "Source error: Failed to fetch remaining episodes for show news: offline")
        store.prepare(entry, "target", [], ["episode"], ["episode"], detail)
        store.finish(entry, "uncertain", "response lost")
        store.resolve(True, "Confirmed afterwards; " + store.pending()["detail"])

        html = self.client.get("/", environ_overrides=self.remote).data.decode()
        self.assertIn("<td>error (news source error; cached results retrieved; playlist contains news)</td>", html)
        self.assertIn("Confirmed afterwards; 90 minutes", html)

    def test_history_links_cached_discovery_failures_to_earlier_warning(self):
        entry = {
            "started": "2026-01-01",
            "status": "succeeded",
            "detail": (
                "90 minutes\nSelection:\n"
                "Discovery tracks: reused from today's cache; the earlier discovery fetch encountered errors; "
                "0 artist tracks and 0 collaborator tracks; no artist release or artist release track requests "
                "during this refresh.\n"
                "Selected music by candidate group (duplicates credited to the first group): "
                "artist discoveries: 0 (no candidates because source retrieval failed).\n"
                "Source error: Skipping artist releases for artist favorite: Spotify failed "
                "(during an earlier discovery fetch today)"
            ),
        }
        with patch.object(web.store, "history", return_value=[entry]):
            response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b'<a href="#history-0-error-2">no candidates because source retrieval failed</a>',
                      response.data)
        self.assertIn(b'during an earlier discovery fetch today', response.data)

    def test_history_links_zero_result_errors_to_source_warnings(self):
        entry = {
            "started": "2026-01-01",
            "status": "succeeded",
            "detail": (
                "90 minutes\nSelection:\n"
                "Top tracks short term: Spotify fetch failed; no data retrieved from cache; "
                "0 results because the fetch failed.\n"
                "News show bulletin: 0/1 requested episodes selected, using available playable episodes "
                "and excluding duplicates. 0 episodes selected because Spotify requests failed.\n"
                "Source error: Skipping short-term top tracks: Spotify unavailable\n"
                "Source error: Failed to fetch remaining episodes for show bulletin: Spotify unavailable"
            ),
        }
        with patch.object(web.store, "history", return_value=[entry]):
            response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(
            b'<a href="#history-0-error-2">0 results because the fetch failed</a>',
            response.data,
        )
        self.assertIn(
            b'<a href="#history-0-error-3">0 episodes selected because Spotify requests failed</a>',
            response.data,
        )
        self.assertIn(b'id="history-0-error-2"', response.data)
        self.assertIn(b'id="history-0-error-3"', response.data)

    def test_history_expands_playlist_items_and_details(self):
        store = self.isolated_store()
        store.finish(store.start(), "failed", "Refresh error message")
        history_id = store.start()
        store.prepare(history_id, "target", [], ["track1", "track2"], [],
                      "90 minutes; 2 music tracks\nItems:\nArtist - Song\nOther & Artist - <Song>")
        store.resolve(True)
        history_id = store.start()
        store.prepare(history_id, "target", [], ["track1", "episode"], ["episode"],
                      "90 minutes; 1 music tracks\nSelection:\nArtist & <choice>\n"
                      "News show: 1/1 episodes\nItems:\nArtist - Song\n[Episode] News & <Update>")
        store.resolve(True)

        response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b"90 minutes; 2 music tracks", response.data)
        self.assertIn(b"<details><summary>Show playlist items</summary>", response.data)
        self.assertIn(b"<details><summary>How this playlist was selected</summary>", response.data)
        self.assertIn(b"<li>Artist &amp; &lt;choice&gt;</li>", response.data)
        self.assertIn(b"<li>News show: 1/1 episodes</li>", response.data)
        self.assertIn(b"<li>Artist - Song</li>", response.data)
        self.assertIn(b"<li>Other &amp; Artist - &lt;Song&gt;</li>", response.data)
        self.assertIn(b"<li>[Episode] News &amp; &lt;Update&gt;</li>", response.data)
        self.assertIn(b"Refresh error message", response.data)
        self.assertEqual(response.data.count(b"<details>"), 3)

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

    def test_market_and_timezone_settings_are_saved_and_validated(self):
        store = self.isolated_store()
        store.set_many({"client_id": "a" * 32, "refresh_token": "saved",
                        "followed_artists_cache": {"artists": {"old": {}}}})
        response = self.client.get("/", environ_overrides=self.remote)
        self.assertIn(b'name="market" required maxlength="2" value="NL"', response.data)
        self.assertIn(b'name="timezone" required value="system"', response.data)

        data = {"csrf": web.csrf, "client_id": "a" * 32, "target": "0" * 22,
                "market": "us", "timezone": "Asia/Tokyo"}
        store.set_cached("liked_tracks_cache", "2026-01-01", ["old"])
        store.set_cached("discovery_tracks_cache", "2026-01-01", {"discoveries": ["old"]})
        store.start_request("Liked tracks", datetime(2026, 1, 1, tzinfo=timezone.utc), "Read")
        response = self.client.post("/configure", data=data, environ_overrides=self.remote)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((store.get("market"), store.get("timezone")), ("US", "Asia/Tokyo"))
        self.assertIn(b'name="market" required maxlength="2" value="US"', response.data)
        self.assertIn(b'name="timezone" required value="Asia/Tokyo"', response.data)
        self.assertIsNone(store.get_cached("liked_tracks_cache", "2026-01-01"))
        self.assertIsNone(store.get_cached("discovery_tracks_cache", "2026-01-01"))
        self.assertEqual(store.request_overview("2026-01-01")["requests"], 0)
        self.assertEqual(store.get("refresh_token"), "saved")
        self.assertIsNotNone(store.get("followed_artists_cache"))

        for field, invalid in (("market", "123"), ("market", ""), ("timezone", "Invalid/Zone")):
            with self.subTest(field=field):
                response = self.client.post(
                    "/configure", data=dict(data, **{field: invalid}), environ_overrides=self.remote
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual((store.get("market"), store.get("timezone")), ("US", "Asia/Tokyo"))

        without_new_fields = {key: value for key, value in data.items() if key not in ("market", "timezone")}
        self.client.post("/configure", data=without_new_fields, environ_overrides=self.remote)
        self.assertEqual((store.get("market"), store.get("timezone")), ("US", "Asia/Tokyo"))

    def test_market_change_is_blocked_while_playlist_write_is_uncertain(self):
        store = self.isolated_store()
        store.set_many({"client_id": "a" * 32, "market": "NL"})
        history = store.start()
        store.prepare(history, "0" * 22, ["old"], ["new"], [], "detail")
        response = self.client.post(
            "/configure", data={"csrf": web.csrf, "client_id": "a" * 32, "target": "0" * 22,
                                "market": "US", "timezone": "system"},
            environ_overrides=self.remote,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"keep the original market", response.data)
        self.assertEqual(store.get("market"), "NL")

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
        self.assertIsNotNone(web.store.get_previous_cached("liked_tracks_cache", "2026-01-02"))
        self.assertIsNotNone(web.store.get("followed_artists_cache"))
        with patch("app.web.token_request", return_value={"refresh_token": "new"}):
            response = self.client.post("/callback", data=data, environ_overrides=self.remote)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(web.store.get_cached("liked_tracks_cache", "2026-01-01"))
        self.assertIsNone(web.store.get_previous_cached("liked_tracks_cache", "2026-01-02"))
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

    def test_scheduler_runs_four_times_in_amsterdam_time(self):
        amsterdam = ZoneInfo("Europe/Amsterdam")
        self.isolated_store().set_many({"timezone": "Europe/Amsterdam"})

        for date in ((2026, 1, 15), (2026, 7, 15)):
            with self.subTest(date=date):
                local_times = [datetime(*date, hour, 0, 0, tzinfo=amsterdam) for hour in range(24)]
                local_times += [datetime(*date, hour, minute, tzinfo=amsterdam)
                                for hour in (8, 12, 16, 20) for minute in (14, 15, 16)]
                utc_times = [value.astimezone(timezone.utc) for value in local_times]

                with patch("app.web.datetime") as clock, patch("app.web.run_refresh") as run, \
                     patch("app.web.time.sleep", side_effect=[None] * 35 + [StopIteration]):
                    clock.now.side_effect = lambda tz: utc_times.pop(0).astimezone(tz)
                    with self.assertRaises(StopIteration):
                        web.scheduler()

                self.assertEqual(run.call_args_list, [
                    call(f"{date[0]:04}-{date[1]:02}-{date[2]:02} {hour:02}:15")
                    for hour in (8, 12, 16, 20)
                ])
                self.assertEqual(clock.now.call_count, 36)
                self.assertTrue(all(c.args[0] == timezone.utc for c in clock.now.call_args_list))

    def test_scheduler_uses_timezone_changes_without_restart(self):
        store = self.isolated_store()
        store.set_many({"timezone": "UTC"})
        utc_times = [
            datetime(2026, 1, 15, 8, 15, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 23, 15, tzinfo=timezone.utc),
        ]

        def change_timezone(key):
            store.set_many({"timezone": "Asia/Tokyo"})

        with patch("app.web.datetime") as clock, \
             patch("app.web.run_refresh", side_effect=change_timezone) as run, \
             patch("app.web.time.sleep", side_effect=[None, StopIteration]):
            clock.now.side_effect = lambda tz: utc_times.pop(0)
            with self.assertRaises(StopIteration):
                web.scheduler()
        self.assertEqual(run.call_args_list, [call("2026-01-15 08:15"), call("2026-01-16 08:15")])

    def test_scheduler_uses_changed_times_without_restart(self):
        store = self.isolated_store()
        store.set_many({"timezone": "UTC"})
        utc_times = [
            datetime(2026, 1, 15, 8, 15, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 12, 15, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
        ]
        iteration = 0

        def change_schedule(key):
            if key == "2026-01-15 08:15":
                response = self.client.post(
                    "/refresh-times", data={"csrf": web.csrf, "refresh_times": "09:30"},
                    environ_overrides=self.remote,
                )
                self.assertEqual(response.status_code, 200)

        def sleep(interval):
            nonlocal iteration
            iteration += 1
            if iteration == len(utc_times):
                raise StopIteration

        with patch("app.web.datetime") as clock, \
             patch("app.web.run_refresh", side_effect=change_schedule) as run, \
             patch("app.web.time.sleep", side_effect=sleep):
            clock.now.side_effect = lambda tz: utc_times[iteration]
            with self.assertRaises(StopIteration):
                web.scheduler()
        self.assertEqual(run.call_args_list, [call("2026-01-15 08:15"), call("2026-01-15 09:30")])

    def test_scheduler_logs_and_skips_invalid_stored_times(self):
        store = self.isolated_store()
        store.set_many({"automatic_refresh_times": ["24:00"]})
        with patch("app.web.run_refresh") as run, \
             patch("app.web.time.sleep", side_effect=StopIteration), \
             self.assertLogs("app.web", level="ERROR") as logged:
            with self.assertRaises(StopIteration):
                web.scheduler()
        run.assert_not_called()
        self.assertIn("Could not read the scheduler settings", "\n".join(logged.output))

    def test_scheduler_skips_disabled_runs_and_responds_to_enabling(self):
        store = self.isolated_store()
        store.set_many({"timezone": "UTC", "automatic_refresh_enabled": False})
        sleep_calls = []

        def sleep(interval):
            sleep_calls.append(interval)
            if len(sleep_calls) == 1:
                store.set_many({"automatic_refresh_enabled": True})
            else:
                raise StopIteration

        with patch("app.web.datetime") as clock, patch("app.web.run_refresh") as run, \
             patch("app.web.time.sleep", side_effect=sleep):
            clock.now.return_value = datetime(2026, 1, 15, 8, 15, tzinfo=timezone.utc)
            with self.assertRaises(StopIteration):
                web.scheduler()
        run.assert_called_once_with("2026-01-15 08:15")
        self.assertEqual(clock.now.call_count, 1)


if __name__ == "__main__":
    unittest.main()
