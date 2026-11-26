import logging
import threading

from .selection import build
from .spotify import Spotify, SpotifyError, SpotifyRateLimitError
from .store import selection_details

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
            self.store.resolve(True, "Confirmed afterwards; " + pending["detail"])
            return True
        elif actual == pending["before_items"]:
            self.store.resolve(False, "Spotify change was not applied" + selection_details(pending["detail"]))
            return False
        else:
            raise SpotifyError("Previous change has an uncertain outcome; check and restore the playlist manually")

    def run(self, scheduled_key=None):
        # Prevent manual and scheduled refreshes from replacing the playlist at once.
        if not self.lock.acquire(blocking=False):
            log.warning("Refresh skipped: another refresh is already running")
            raise SpotifyError("A refresh is already running")

        history_id = None
        detail = None
        try:
            history_id = self.store.start(scheduled_key)
            if history_id is None:
                return

            # Resolve any uncertain previous write before preparing a new one.
            api = Spotify(self.store)
            self.reconcile(api)
            pid = self.store.get("playlist_id")
            if not pid:
                raise SpotifyError("Configure the Daily Drive playlist first")
            source_ids = self.store.get("source_ids", [])
            if pid in source_ids:
                raise SpotifyError("Daily Drive cannot also be a music source")

            # Build the complete replacement before making any playlist changes.
            before = api.playlist_items(pid)
            after, episodes, detail = build(
                api, source_ids, self.store.get("news_shows", []),
                self.store.get("podcast_shows", []),
            )
            if len(after) > 100:
                raise SpotifyError("Spotify allows at most 100 items per replacement")

            # Report repeated episodes without excluding the latest available ones.
            previously_used = self.store.used_episodes()
            repeated = sum(uri in previously_used for uri in episodes)
            if repeated:
                detail = f"{repeated} latest episodes used previously; {detail}"

            # Persist the snapshots before sending the single replacement request.
            self.store.prepare(history_id, pid, before, after, episodes, detail)
            try:
                api.replace(pid, after)
            except Exception as exc:
                # A lost response does not imply Spotify rejected the update.
                try:
                    confirmed = self.reconcile(api)
                except Exception as check_error:
                    if isinstance(exc, SpotifyRateLimitError):
                        raise SpotifyError(f"{exc}; playlist verification failed: {check_error}") from check_error
                    raise
                if confirmed:
                    return
                if isinstance(exc, SpotifyRateLimitError):
                    raise
                raise SpotifyError("Spotify change was not applied")
            self.store.resolve(True)
            log.info("Daily Drive refreshed: %s", detail)
        except Exception as exc:
            log.exception("Daily Drive refresh failed")
            if history_id is not None:
                # A pending write means the actual playlist contents are still unknown.
                status = "uncertain" if self.store.pending() else "failed"
                error = str(exc)
                if detail:
                    error += selection_details(detail)
                self.store.finish(history_id, status, error)
            raise
        finally:
            self.lock.release()
