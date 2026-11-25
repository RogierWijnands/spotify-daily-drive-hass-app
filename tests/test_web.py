import os
import tempfile
import unittest

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
        self.assertEqual(self.client.post("/refresh", environ_overrides=self.remote).status_code, 403)

    def test_configuration_accepts_urls_and_rejects_target_as_source(self):
        pid = "0123456789ABCDEFGHIJKL"
        data = {"csrf": web.csrf, "client_id": "a" * 32,
                "target": "https://open.spotify.com/playlist/" + pid, "sources": ""}
        response = self.client.post("/configure", data=data, environ_overrides=self.remote)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(web.store.get("playlist_id"), pid)
        data["sources"] = data["target"]
        response = self.client.post("/configure", data=data, environ_overrides=self.remote)
        self.assertEqual(response.status_code, 400)

    def test_manual_refresh_reports_running_job(self):
        web.refresh.lock.acquire()
        try:
            response = self.client.post("/refresh", data={"csrf": web.csrf}, environ_overrides=self.remote)
        finally:
            web.refresh.lock.release()
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
