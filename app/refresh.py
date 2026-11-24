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
        actual = api.playlist_items(pending["playlist_id"])
        if actual == pending["after_items"]:
            self.store.resolve(True, pending["detail"] + " (achteraf bevestigd)")
            return True
        elif actual == pending["before_items"]:
            self.store.resolve(False, "Spotify-wijziging niet doorgevoerd")
            return False
        else:
            raise SpotifyError("Uitkomst van eerdere wijziging onzeker; controleer de playlist en herstel deze handmatig")

    def run(self, scheduled_key=None):
        if not self.lock.acquire(blocking=False):
            log.warning("Verversen overgeslagen: er loopt al een verversing")
            raise SpotifyError("Er loopt al een verversing")
        history_id = None
        try:
            history_id = self.store.start(scheduled_key)
            if history_id is None:
                return
            api = Spotify(self.store)
            self.reconcile(api)
            pid = self.store.get("playlist_id")
            if not pid:
                raise SpotifyError("Stel eerst de Daily Drive-playlist in")
            source_ids = self.store.get("source_ids", [])
            if pid in source_ids:
                raise SpotifyError("Daily Drive mag niet ook een muziekbron zijn")
            before = api.playlist_items(pid)
            after, episodes, detail = build(api, source_ids)
            if len(after) > 100:
                raise SpotifyError("Spotify staat maximaal 100 items per vervanging toe")
            previously_used = self.store.used_episodes()
            repeated = sum(uri in previously_used for uri in episodes)
            if repeated:
                detail += f"; {repeated} eerder gebruikte nieuwste afleveringen"
            self.store.prepare(history_id, pid, before, after, episodes, detail)
            try:
                api.replace(pid, after)
            except Exception:
                # A lost response does not imply Spotify rejected the update.
                if self.reconcile(api):
                    return
                raise SpotifyError("Spotify-wijziging is niet doorgevoerd")
            self.store.resolve(True)
            log.info("Daily Drive ververst: %s", detail)
        except Exception as exc:
            log.exception("Daily Drive verversen mislukt")
            if history_id is not None:
                status = "onzeker" if self.store.pending() else "mislukt"
                self.store.finish(history_id, status, str(exc))
            raise
        finally:
            self.lock.release()
