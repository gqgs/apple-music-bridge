"""Best-effort album availability from Apple's public, credential-free album page.

These are catalog badges, never measurements of downloaded or playing audio.
A changed page schema, wrong storefront/album, or network failure yields no badge.
"""

import threading
import time
from collections import OrderedDict
from urllib.error import HTTPError

from ..apple_album import album_descriptor, fetch_album_page, matching_nodes, server_data

BADGES = (
    ("dolbyAtmos", "dolby-atmos"),
    ("dolbyAudio", "dolby-audio"),
    ("hiResLossless", "hi-res-lossless"),
    ("lossless", "lossless"),
)


def parse_album_badges(page, country, identifier):
    """Return a tuple for a validated album header, or None when unknown."""
    headers = matching_nodes(server_data(page), "album-detail-header - " + identifier)
    if len(headers) != 1 or not album_descriptor(headers[0], country, identifier):
        return None
    badges = headers[0].get("audioBadges")
    if not isinstance(badges, dict) or not all(type(badges.get(key)) is bool for key, _ in BADGES):
        return None
    return tuple(value for key, value in BADGES if badges[key])


def fetch_album_badges(country, identifier):
    return parse_album_badges(fetch_album_page(country, identifier), country, identifier)


class AlbumQuality:
    """Bounded cache and single-flight lookup, independent of audio/catalog workers."""

    def __init__(self, fetch=None, clock=None):
        self.fetch = fetch or fetch_album_badges
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.flights = {}
        self.not_before = 0.0

    def get(self, country, identifier):
        key = (country, identifier)
        with self.lock:
            hit = self.cache.get(key)
            if hit and hit[0] > self.clock():
                self.cache.move_to_end(key)
                return list(hit[1])
            event = self.flights.get(key)
            owner = event is None
            if owner:
                if len(self.flights) >= 2 or self.clock() < self.not_before:
                    return []
                event = self.flights[key] = threading.Event()
        if not owner:
            event.wait(16)
            with self.lock:
                hit = self.cache.get(key)
                return list(hit[1]) if hit and hit[0] > self.clock() else []
        value = None
        try:
            value = self.fetch(country, identifier)
        except HTTPError as exc:
            if exc.code == 429:
                with self.lock:
                    self.not_before = self.clock() + 300
        except (OSError, ValueError, RecursionError):
            pass
        finally:
            with self.lock:
                # A failed lookup is unknown, not proof that the album is lossy.
                self.cache[key] = (
                    self.clock() + (86400 if value is not None else 300),
                    value or (),
                )
                self.cache.move_to_end(key)
                while len(self.cache) > 256:
                    self.cache.popitem(last=False)
                self.flights.pop(key).set()
        return list(value or ())
