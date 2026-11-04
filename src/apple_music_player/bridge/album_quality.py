"""Best-effort album availability from Apple's public, credential-free album page.

These are catalog badges, never measurements of downloaded or playing audio.
A changed page schema, wrong storefront/album, or network failure yields no badge.
"""

import json
import re
import threading
import time
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_PAGE_BYTES = 1024 * 1024
BADGES = (
    ("dolbyAtmos", "dolby-atmos"),
    ("dolbyAudio", "dolby-audio"),
    ("hiResLossless", "hi-res-lossless"),
    ("lossless", "lossless"),
)


def album_page_url(url, country, identifier):
    """Only the requested public Apple album in the exact storefront is allowed."""
    parts = urlsplit(url)
    path = parts.path.strip("/").split("/")
    return (
        parts.scheme == "https"
        and parts.netloc == "music.apple.com"
        and not parts.query
        and not parts.fragment
        and len(path) in {3, 4}
        and path[:2] == [country, "album"]
        and path[-1] == identifier
    )


class _AlbumRedirect(HTTPRedirectHandler):
    def __init__(self, country, identifier):
        self.country, self.identifier = country, identifier

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not album_page_url(newurl, self.country, self.identifier):
            raise ValueError("Unexpected album page redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _ServerData(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if (
            tag == "script"
            and attrs.get("id") == "serialized-server-data"
            and attrs.get("type") == "application/json"
        ):
            self.active = True
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.scripts[-1] += data


def parse_album_badges(page, country, identifier):
    """Return a tuple for a validated album header, or None when unknown."""
    if len(page) > MAX_PAGE_BYTES:
        return None
    parser = _ServerData()
    try:
        parser.feed(page.decode("utf-8"))
        if len(parser.scripts) != 1:
            return None
        data = json.loads(parser.scripts[0])
    except (ValueError, RecursionError):
        return None
    pending = [data]
    matches = []
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            descriptor = node.get("contentDescriptor")
            if isinstance(descriptor, dict) and node.get("id") == (
                "album-detail-header - " + identifier
            ):
                identifiers = descriptor.get("identifiers")
                badges = node.get("audioBadges")
                if (
                    descriptor.get("kind") == "album"
                    and isinstance(identifiers, dict)
                    and identifiers.get("storeAdamID") == identifier
                    and isinstance(descriptor.get("url"), str)
                    and album_page_url(descriptor["url"], country, identifier)
                    and isinstance(badges, dict)
                    and all(type(badges.get(key)) is bool for key, _ in BADGES)
                ):
                    matches.append(tuple(value for key, value in BADGES if badges[key]))
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return matches[0] if len(matches) == 1 else None


def fetch_album_badges(country, identifier):
    if not re.fullmatch(r"[a-z]{2}", country) or not re.fullmatch(r"[0-9]{1,30}", identifier):
        raise ValueError("Invalid album identity")
    url = f"https://music.apple.com/{country}/album/{identifier}"
    # No cookies, account tokens, downloader, previews, or audio requests.
    request = Request(url, headers={"Accept": "text/html"})
    opener = build_opener(_AlbumRedirect(country, identifier))
    with opener.open(request, timeout=15) as response:
        if not album_page_url(response.geturl(), country, identifier):
            return None
        if response.headers.get_content_type() != "text/html":
            return None
        page = response.read(MAX_PAGE_BYTES + 1)
    return parse_album_badges(page, country, identifier)


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
