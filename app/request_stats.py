from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class RefreshRequestStats:
    api_requests: int = 0
    token_requests: int = 0
    cache_hits: int = 0


current_refresh_stats = ContextVar("current_refresh_stats", default=None)


def record_request(*, token=False):
    stats = current_refresh_stats.get()
    if stats is not None:
        if token:
            stats.token_requests += 1
        else:
            stats.api_requests += 1


def record_cache_hit():
    stats = current_refresh_stats.get()
    if stats is not None:
        stats.cache_hits += 1
