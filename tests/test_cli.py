import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import cli
from app.store import Store


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.client_id = "a" * 32
        self.target = "0" * 22
        self.path.write_text(json.dumps({"client_id": self.client_id, "playlist": self.target,
                                        "source_playlists": []}), encoding="utf-8")
        self.db = str(self.path.parent / ".daily-drive.sqlite3")

    def test_authorize_stores_refresh_token_without_printing_it(self):
        token = "private-refresh-token"
        callback = "http://127.0.0.1:8888/callback?code=abc&state=state"
        with patch("app.cli.authorization", return_value=("https://spotify.example", "verifier", "state")), \
             patch("app.cli.getpass", return_value=callback), \
             patch("app.cli.token_request", return_value={"refresh_token": token}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = cli.main(["--config", str(self.path), "--authorize"])
        self.assertEqual(result, 0)
        self.assertNotIn(token, output.getvalue())
        self.assertEqual(Store(self.db).get("refresh_token"), token)

    def test_preview_displays_items_without_writing_playlist(self):
        item = {"uri": "spotify:track:1", "name": "Song", "duration_ms": 180000,
                "artists": [{"name": "Artist"}]}
        with patch("app.cli.Spotify") as spotify, \
             patch("app.cli.build_items", return_value=([item], [], "120 minutes")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = cli.main(["--config", str(self.path)])
        self.assertEqual(result, 0)
        spotify.return_value.playlist_items.assert_called_once_with(self.target)
        spotify.return_value.replace.assert_not_called()
        self.assertIn("Artist - Song", output.getvalue())
        self.assertIn("not changed", output.getvalue())
        self.assertEqual(Store(self.db).history(), [])

    def test_apply_uses_refresh_and_persists_history(self):
        class FakeSpotify:
            def __init__(self, store):
                pass
            def playlist_items(self, pid):
                return ["old"]
            def replace(self, pid, items):
                self.replaced = (pid, items)
        fake = FakeSpotify(None)
        with patch("app.refresh.Spotify", return_value=fake), \
             patch("app.refresh.build", return_value=(["new"], ["episode"], "120 minutes")):
            result = cli.main(["--config", str(self.path), "--apply"])
        self.assertEqual(result, 0)
        self.assertEqual(fake.replaced, (self.target, ["new"]))
        store = Store(self.db)
        self.assertEqual(store.history()[0]["status"], "succeeded")
        self.assertEqual(store.used_episodes(), {"episode"})

    def test_invalid_config_does_not_open_database(self):
        self.path.write_text('{"client_id": "wrong"}', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as output:
            result = cli.main(["--config", str(self.path)])
        self.assertEqual(result, 1)
        self.assertIn("client_id", output.getvalue())
        self.assertFalse(Path(self.db).exists())

    def test_default_config_is_local_config_json(self):
        self.path.rename(self.path.parent / "local-config.json")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        result = subprocess.run([sys.executable, "-m", "app.cli", "--pending"],
                                cwd=self.temp.name, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No uncertain playlist change", result.stdout)

    def test_account_change_blocked_when_pending(self):
        store = Store(self.db)
        store.set_many({"client_id": "b" * 32})
        history = store.start()
        store.prepare(history, self.target, ["old"], ["new"], [], "detail")
        with contextlib.redirect_stderr(io.StringIO()) as output:
            result = cli.main(["--config", str(self.path)])
        self.assertEqual(result, 1)
        self.assertIn("uncertain", output.getvalue())
        self.assertIsNotNone(Store(self.db).pending())

    def test_pending_requires_confirmation_to_release(self):
        store = Store(self.db)
        store.set_many({"client_id": self.client_id})
        history = store.start()
        store.prepare(history, self.target, ["old"], ["new"], [], "detail")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = cli.main(["--config", str(self.path), "--pending"])
        self.assertEqual(result, 0)
        self.assertIn("old", output.getvalue())
        self.assertIn("new", output.getvalue())
        with patch("builtins.input", return_value="no"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--config", str(self.path), "--release-pending"]), 1)
        self.assertIsNotNone(Store(self.db).pending())
        with patch("builtins.input", return_value="CONFIRM"):
            self.assertEqual(cli.main(["--config", str(self.path), "--release-pending"]), 0)
        store = Store(self.db)
        self.assertIsNone(store.pending())
        self.assertEqual(store.history()[0]["status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
