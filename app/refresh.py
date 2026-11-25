import logging
import threading

from .selection import build
from .spotify import Spotify, SpotifyError

log = logging.getLogger(__name__)


class Refresh:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()

    def reconcile(self, api):
        pending = self.store.pending()
        if not pending:
            return None

        # Compare with both snapshots before deciding whether a lost write succeeded.
        actual = api.playlist_items(pending["playlist_id"])
        if actual == pending["after_items"]:
            self.store.resolve(True, pending["detail"] + " (confirmed afterwards)")
            return True
        elif actual == pending["before_items"]:
            self.store.resolve(False, "Spotify change was not applied")
            return False
        else:
            raise SpotifyError("Previous change has an uncertain outcome; check and restore the playlist manually")

    def run(self, scheduled_key=None):
        if not self.lock.acquire(blocking=False):
            log.warning("Refresh skipped: another refresh is already running")
            raise SpotifyError("A refresh is already running")

        history_id = None
        try:
            history_id = self.store.start(scheduled_key)
            if history_id is None:
                return

            api = Spotify(self.store)
            self.reconcile(api)
            pid = self.store.get("playlist_id")
            if not pid:
                raise SpotifyError("Configure the Daily Drive playlist first")
            source_ids = self.store.get("source_ids", [])
            if pid in source_ids:
                raise SpotifyError("Daily Drive cannot also be a music source")

            before = api.playlist_items(pid)
            after, episodes, detail = build(
                api, source_ids, self.store.get("news_shows", []),
                self.store.get("podcast_shows", []),
            )
            if len(after) > 100:
                raise SpotifyError("Spotify allows at most 100 items per replacement")

            previously_used = self.store.used_episodes()
            repeated = sum(uri in previously_used for uri in episodes)
            if repeated:
                detail += f"; {repeated} latest episodes used previously"

            # Persist the snapshots before sending the single replacement request.
            self.store.prepare(history_id, pid, before, after, episodes, detail)
            try:
                api.replace(pid, after)
            except Exception:
                # A lost response does not imply Spotify rejected the update.
                if self.reconcile(api):
                    return
                raise SpotifyError("Spotify change was not applied")
            self.store.resolve(True)
            log.info("Daily Drive refreshed: %s", detail)
        except Exception as exc:
            log.exception("Daily Drive refresh failed")
            if history_id is not None:
                status = "uncertain" if self.store.pending() else "failed"
                self.store.finish(history_id, status, str(exc))
            raise
        finally:
            self.lock.release()
