"""One replaceable, optional album search on the artist-artwork worker.

Query generations outlive HTTP search flights. No timers or extra network workers
are created; a queued drain and artist jobs share the same single executor.
"""

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from ..catalog import CatalogDeferred


@dataclass
class Query:
    key: tuple
    created: float
    cancelled: threading.Event = field(default_factory=threading.Event)
    ready: bool = False


class ArtworkSeed:
    debounce = 1.0

    def __init__(self, service):
        self.service = service
        self.lock = threading.Lock()
        self.current = None
        self.queued = False
        self.attempted = OrderedDict()
        self.active = None

    def observe(self, params):
        # Only the explicit Artists-only request opts into optional enrichment.
        query = params.get("query", "").strip()[:200]
        eligible = (
            query
            and params.get("artistCount", "20") != "0"
            and params.get("albumCount") == "0"
            and params.get("songCount") == "0"
        )
        core = self.service.core
        key = (query, core.country, core.quality) if eligible else None
        with self.lock:
            if self.current and self.current.key == key:
                return self.current
            if self.current:
                self.current.cancelled.set()
            self.current = Query(key, time.monotonic()) if key else None
            return self.current

    def ready(self, query, payload):
        with self.lock:
            if (
                query is None
                or query is not self.current
                or self.service.stop.is_set()
                or not payload.get("searchResult3", {}).get("artist")
            ):
                return
            query.ready = True
            if not self.queued:
                self.queued = True
                self.service.pool.submit(self.drain)

    def drain(self):
        try:
            self.run()
        finally:
            with self.lock:
                self.queued = False
                # A replacement arriving during run's final check still needs a drain.
                query = self.current
                if query and query.ready and not self.service.stop.is_set():
                    self.queued = True
                    self.service.pool.submit(self.drain)

    def wait(self, query, seconds):
        return query.cancelled.wait(seconds)

    def retry_after(self):
        with self.lock:
            if self.active:
                started, until = self.active
                now = time.monotonic()
                if now - started >= 6:
                    return max(3, min(45, math.ceil(until - now)))
        return 3

    def run(self):
        """Called only on the shared artwork worker, also before queued lookups."""
        service = self.service
        with self.lock:
            query = self.current
            if not query or not query.ready or service.stop.is_set():
                return False
            query.ready = False
            previous = self.attempted.get(query.key)
            if previous is not None and time.monotonic() - previous < 3600:
                return False
        started = time.monotonic()
        deadline = started + 30
        with self.lock:
            self.active = (started, deadline + 15)
        outcome = "cancelled"
        try:
            for _ in range(320):
                if query.cancelled.is_set() or service.stop.is_set():
                    return True
                now = time.monotonic()
                if now >= deadline:
                    outcome = "deferred"
                    return True
                delay = max(query.created + self.debounce, service.actor.last_metadata + 1) - now
                if not service.actor.metadata_idle.is_set():
                    delay = max(delay, 0.1)
                if delay > 0:
                    self.wait(query, min(delay, 0.1))
                    continue
                try:
                    with self.lock:
                        self.active = (started, time.monotonic() + 15)
                    albums = service.catalog.search(query.key[0], query.key[1], "album", limit=200)
                    break
                except CatalogDeferred as exc:
                    with self.lock:
                        self.active = (started, deadline + 15)
                    if exc.rate_limited or not 0 <= exc.seconds < deadline - time.monotonic():
                        outcome = "deferred"
                        return True
                    # Poll cancellation and foreground state during limiter waits.
                    self.wait(query, min(max(exc.seconds, 0.01), 0.1))
            else:
                outcome = "deferred"
                return True
            # Database writer waits do not hold the query-generation lock.
            if not service.store_albums(
                [a for a in albums[:200] if a.country == query.key[1]], query=query
            ):
                return True
            outcome = "ok"
        except Exception:
            outcome = "failed"
        finally:
            with self.lock:
                self.active = None
            if outcome != "cancelled":
                with self.lock:
                    self.attempted[query.key] = time.monotonic()
                    self.attempted.move_to_end(query.key)
                    while len(self.attempted) > 128:
                        self.attempted.popitem(last=False)
            service.metrics.record("artist_seed", outcome)
        return True

    def close(self):
        with self.lock:
            if self.current:
                self.current.cancelled.set()
