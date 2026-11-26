import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .constants import (
    CACHE_KEY_SUFFIX, DATABASE_CONNECTION_TIMEOUT_SECONDS, FOLLOWED_ARTISTS_CACHE_KEY,
    HISTORY_ENTRY_LIMIT, HISTORY_STATUS_FAILED, HISTORY_STATUS_IN_PROGRESS,
    HISTORY_STATUS_SUCCEEDED, HISTORY_STATUS_UNCERTAIN, ITEMS_DETAIL_SEPARATOR,
    NO_UNCERTAIN_CHANGE_ERROR,
    SELECTION_DETAIL_SEPARATOR, SHOW_EPISODE_CACHE_PREFIX,
)


def selection_details(detail):
    _, separator, selection = detail.partition(SELECTION_DETAIL_SEPARATOR)
    if not separator:
        return ""
    return SELECTION_DETAIL_SEPARATOR + selection.partition(ITEMS_DETAIL_SEPARATOR)[0]


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
                CREATE TABLE IF NOT EXISTS daily_requests (
                    day TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0, first_quota_number INTEGER,
                    first_quota_category TEXT, first_quota_category_number INTEGER,
                    first_quota_at TEXT, first_quota_detail TEXT
                );
                CREATE TABLE IF NOT EXISTS daily_request_categories (
                    day TEXT NOT NULL, category TEXT NOT NULL,
                    requests INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, category)
                );
            """)
            # Only a persisted pending write could have reached Spotify before a restart.
            db.execute(
                """
                UPDATE history SET status=?, detail=?
                WHERE status=? AND id NOT IN (SELECT history_id FROM pending)
                """,
                (
                    HISTORY_STATUS_FAILED,
                    "App stopped before replacing the playlist",
                    HISTORY_STATUS_IN_PROGRESS,
                ),
            )
            pending = db.execute("SELECT history_id, detail FROM pending").fetchone()
            if pending:
                db.execute(
                    """
                    UPDATE history SET status=?, detail=?
                    WHERE status=? AND id=?
                    """,
                    (
                        HISTORY_STATUS_UNCERTAIN,
                        "App stopped during playlist replacement"
                        + selection_details(pending["detail"]),
                        HISTORY_STATUS_IN_PROGRESS,
                        pending["history_id"],
                    ),
                )
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=DATABASE_CONNECTION_TIMEOUT_SECONDS)
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

    def set_many(self, values, *, clear_cache=False, clear_daily_cache=False,
                 clear_request_stats=False):
        # Credential changes and cache invalidation share one transaction.
        with self.connect() as db:
            if clear_cache or clear_daily_cache:
                db.execute("DELETE FROM daily_cache")
            if clear_cache:
                db.execute("DELETE FROM settings WHERE key=?", (FOLLOWED_ARTISTS_CACHE_KEY,))
            if clear_request_stats:
                # Counts from different day boundaries cannot be combined reliably.
                db.execute("DELETE FROM daily_request_categories")
                db.execute("DELETE FROM daily_requests")
            db.executemany(
                "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                [(k, json.dumps(v)) for k, v in values.items()],
            )

    def get_cached(self, key, day):
        # Episodes are usable only on their fetch day; music can serve as an older fallback.
        with self.connect() as db:
            db.execute(
                "DELETE FROM daily_cache WHERE day != ? AND key GLOB ?",
                (day, f"{SHOW_EPISODE_CACHE_PREFIX}*{CACHE_KEY_SUFFIX}"),
            )
            row = db.execute(
                "SELECT value FROM daily_cache WHERE key=? AND day=?", (key, day)
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def get_previous_cached(self, key, day):
        with self.connect() as db:
            row = db.execute(
                "SELECT day, value FROM daily_cache WHERE key=? AND day<?", (key, day)
            ).fetchone()
        return (row["day"], json.loads(row["value"])) if row else None

    def set_cached(self, key, day, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO daily_cache (key, day, value) VALUES (?, ?, ?)",
                (key, day, json.dumps(value)),
            )

    def start_request(self, category, started, detail):
        day = started.date().isoformat()
        with self.connect() as db:
            # Allocate both numbers together so concurrent requests cannot share a number.
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO daily_requests (day) VALUES (?)", (day,))
            db.execute(
                "INSERT OR IGNORE INTO daily_request_categories (day, category) VALUES (?, ?)",
                (day, category),
            )
            db.execute("UPDATE daily_requests SET requests=requests+1 WHERE day=?", (day,))
            db.execute(
                "UPDATE daily_request_categories SET requests=requests+1 WHERE day=? AND category=?",
                (day, category),
            )
            total = db.execute(
                "SELECT requests FROM daily_requests WHERE day=?", (day,)
            ).fetchone()["requests"]
            category_total = db.execute(
                "SELECT requests FROM daily_request_categories WHERE day=? AND category=?",
                (day, category),
            ).fetchone()["requests"]
        return day, category, total, category_total, started.isoformat(), detail

    def fail_request(self, request_info, quota=False):
        day, category, number, category_number, started, detail = request_info
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE daily_requests SET errors=errors+1 WHERE day=?", (day,))
            db.execute(
                "UPDATE daily_request_categories SET errors=errors+1 WHERE day=? AND category=?",
                (day, category),
            )
            if quota:
                # The first quota response wins even when requests finish out of order.
                db.execute(
                    """
                    UPDATE daily_requests
                    SET first_quota_number=?, first_quota_category=?,
                        first_quota_category_number=?, first_quota_at=?, first_quota_detail=?
                    WHERE day=? AND first_quota_number IS NULL
                    """,
                    (number, category, category_number, started, detail, day),
                )

    def request_overview(self, day):
        with self.connect() as db:
            # Keep the totals and categories from the same SQLite snapshot.
            db.execute("BEGIN")
            total = db.execute("SELECT * FROM daily_requests WHERE day=?", (day,)).fetchone()
            categories = db.execute(
                "SELECT category, requests, errors FROM daily_request_categories "
                "WHERE day=? ORDER BY category",
                (day,),
            ).fetchall()
        return {
            "requests": total["requests"] if total else 0,
            "errors": total["errors"] if total else 0,
            "first_quota": {
                "number": total["first_quota_number"],
                "category": total["first_quota_category"],
                "category_number": total["first_quota_category_number"],
                "started": total["first_quota_at"],
                "detail": total["first_quota_detail"],
            } if total and total["first_quota_number"] is not None else None,
            "categories": [dict(row) for row in categories],
        }

    def clear_data(self):
        with self.connect() as db:
            # Check and clear in one write transaction so a pending write is never deleted.
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM pending LIMIT 1").fetchone():
                raise ValueError("Resolve the uncertain playlist change before clearing data")
            db.execute("DELETE FROM daily_cache")
            db.execute("DELETE FROM settings WHERE key=?", (FOLLOWED_ARTISTS_CACHE_KEY,))
            db.execute("DELETE FROM episodes")
            db.execute("DELETE FROM history")
            db.execute("DELETE FROM daily_request_categories")
            db.execute("DELETE FROM daily_requests")

    def compact(self):
        # VACUUM must run outside the data-clearing transaction.
        with self.connect() as db:
            db.execute("VACUUM")

    def history(self):
        # Keep older entries in SQLite; the web UI only needs the most recent ones.
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT started, status, detail FROM history ORDER BY id DESC LIMIT ?",
                (HISTORY_ENTRY_LIMIT,),
            )]

    def start(self, scheduled_key=None):
        now = datetime.now(timezone.utc).isoformat()

        # A manual refresh has no scheduled key; scheduled refreshes use a unique key.
        with self.connect() as db:
            try:
                cursor = db.execute(
                    "INSERT INTO history (started,status,detail,scheduled_key) VALUES (?,?,?,?)",
                    (now, HISTORY_STATUS_IN_PROGRESS, "", scheduled_key),
                )
            except sqlite3.IntegrityError:
                # The unique scheduled key prevents multiple runs in the same minute.
                return None
            return cursor.lastrowid

    def finish(self, history_id, status, detail):
        with self.connect() as db:
            db.execute(
                "UPDATE history SET status=?,detail=? WHERE id=?", (status, detail, history_id)
            )

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
                (HISTORY_STATUS_SUCCEEDED if success else HISTORY_STATUS_FAILED,
                 detail or row["detail"], row["history_id"]),
            )
            db.execute("DELETE FROM pending WHERE id=1")

    def release_pending(self):
        # Manual release does not claim that the Spotify write succeeded or failed.
        with self.connect() as db:
            row = db.execute("SELECT history_id, detail FROM pending WHERE id=1").fetchone()
            if not row:
                raise ValueError(NO_UNCERTAIN_CHANGE_ERROR)

            db.execute(
                "UPDATE history SET status=?,detail=? WHERE id=?",
                (
                    HISTORY_STATUS_UNCERTAIN,
                    "Manually released; outcome not determined" + selection_details(row["detail"]),
                    row["history_id"],
                ),
            )
            db.execute("DELETE FROM pending WHERE id=1")
