import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        with self.connect() as db:
            # Keep configuration, library caches, refresh history and pending writes separate.
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY, started TEXT NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL, scheduled_key TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS episodes (
                    uri TEXT PRIMARY KEY, last_used TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending (
                    id INTEGER PRIMARY KEY CHECK (id = 1), history_id INTEGER NOT NULL,
                    playlist_id TEXT NOT NULL, before_items TEXT NOT NULL, after_items TEXT NOT NULL,
                    episodes TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_cache (
                    key TEXT PRIMARY KEY, day TEXT NOT NULL, value TEXT NOT NULL
                );
            """)
            # Only a persisted pending write can have reached Spotify before a restart.
            db.execute("""
                UPDATE history SET status='failed', detail='App stopped before replacing the playlist'
                WHERE status='in_progress' AND id NOT IN (SELECT history_id FROM pending)
            """)
            db.execute("""
                UPDATE history SET status='uncertain', detail='App stopped during playlist replacement'
                WHERE status='in_progress' AND id IN (SELECT history_id FROM pending)
            """)
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            # Commit successful operations and roll back if an exception occurs.
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_many(self, values, *, clear_cache=False):
        # Credential changes and cache invalidation share one transaction.
        with self.connect() as db:
            if clear_cache:
                db.execute("DELETE FROM daily_cache")
                db.execute("DELETE FROM settings WHERE key='followed_artists_cache'")
            db.executemany(
                "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                [(k, json.dumps(v)) for k, v in values.items()],
            )

    def get_cached(self, key, day):
        # Expired days are removed lazily when the cache is next accessed.
        with self.connect() as db:
            db.execute("DELETE FROM daily_cache WHERE day != ?", (day,))
            row = db.execute("SELECT value FROM daily_cache WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_cached(self, key, day, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO daily_cache (key, day, value) VALUES (?, ?, ?)",
                (key, day, json.dumps(value)),
            )

    def history(self):
        # The web UI shows the latest 20 entries; older entries remain in SQLite.
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT started, status, detail FROM history ORDER BY id DESC LIMIT 20")]

    def start(self, scheduled_key=None):
        now = datetime.now(timezone.utc).isoformat()

        # A manual refresh has no scheduled key; scheduled refreshes use a unique key.
        with self.connect() as db:
            try:
                cursor = db.execute(
                    "INSERT INTO history (started,status,detail,scheduled_key) VALUES (?,?,?,?)",
                    (now, "in_progress", "", scheduled_key),
                )
            except sqlite3.IntegrityError:
                # The unique scheduled key prevents multiple runs in the same minute.
                return None
            return cursor.lastrowid

    def finish(self, history_id, status, detail):
        with self.connect() as db:
            db.execute("UPDATE history SET status=?,detail=? WHERE id=?", (status, detail, history_id))

    def used_episodes(self):
        with self.connect() as db:
            return {row["uri"] for row in db.execute("SELECT uri FROM episodes")}

    def pending(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM pending WHERE id=1").fetchone()
        if not row:
            return None

        result = dict(row)
        for key in ("before_items", "after_items", "episodes"):
            result[key] = json.loads(result[key])
        return result

    def prepare(self, history_id, playlist_id, before, after, episodes, detail):
        # Save both playlist versions before sending the replacement to Spotify.
        with self.connect() as db:
            db.execute(
                "INSERT INTO pending VALUES (1,?,?,?,?,?,?)",
                (history_id, playlist_id, json.dumps(before), json.dumps(after),
                 json.dumps(episodes), detail),
            )

    def resolve(self, success, detail=None):
        now = datetime.now(timezone.utc).isoformat()

        # Update the history and remove the pending write in one transaction.
        with self.connect() as db:
            row = db.execute("SELECT * FROM pending WHERE id=1").fetchone()
            if not row:
                raise RuntimeError("No pending playlist change")

            if success:
                db.executemany(
                    "INSERT OR REPLACE INTO episodes VALUES (?,?)",
                    [(uri, now) for uri in json.loads(row["episodes"])],
                )
            db.execute(
                "UPDATE history SET status=?,detail=? WHERE id=?",
                ("succeeded" if success else "failed",
                 detail or row["detail"], row["history_id"]),
            )
            db.execute("DELETE FROM pending WHERE id=1")

    def release_pending(self):
        # Manual release does not claim that the Spotify write succeeded or failed.
        with self.connect() as db:
            row = db.execute("SELECT history_id FROM pending WHERE id=1").fetchone()
            if not row:
                raise ValueError("There is no uncertain change")

            db.execute(
                "UPDATE history SET status=?,detail=? WHERE id=?",
                ("uncertain", "Manually released; outcome not determined", row["history_id"]),
            )
            db.execute("DELETE FROM pending WHERE id=1")
