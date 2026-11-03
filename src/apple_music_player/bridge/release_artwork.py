"""Artist release covers: cached reads and bounded, demand-driven catalog enrichment."""

import json
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import date
from urllib.parse import quote, urlsplit

from ..artwork import BADGE_URL, allowed_url
from ..catalog import Catalog, CatalogDeferred
from ..model import Album
from .artwork import CoverUnavailable


def safe_reason(reason):
    return (
        reason
        if reason
        in {
            "release-pending",
            "release-unavailable",
            "release-busy",
            "release-failed",
            "shutdown",
            "lookup-failed",
        }
        else "lookup-failed"
    )


class ReleaseUnavailable(CoverUnavailable):
    def __init__(self, reason):
        self.reason = safe_reason(reason)


class ReleaseArtwork:
    def __init__(self, actor, catalog=None):
        self.actor = actor
        self.core = actor.core
        self.catalog = catalog or Catalog(limiter=self.core.catalog.limiter)
        self.uri = "file:" + quote(str(self.core.cache.root / "index.sqlite3")) + "?mode=rw"
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.pending = {}
        self.starts = deque()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="artist-release")

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.uri, uri=True, timeout=0.2)) as db:
            with db:
                yield db

    def selection(self, artist):
        with self.connection() as db:
            rows = db.execute(
                "SELECT data FROM bridge_metadata WHERE json_extract(data, '$.artist_id')=? "
                "AND id LIKE ? ORDER BY json_extract(data, '$.release_date') DESC, id LIMIT 200",
                (artist.id, f"am:{artist.country}:{self.core.quality}:album:%"),
            ).fetchall()
        choices = []
        for (raw,) in rows:
            album = Album(**json.loads(raw))
            if album.country != artist.country or album.artist_id != artist.id:
                continue
            if not allowed_url(album.artwork_url) or album.artwork_url == BADGE_URL:
                continue
            try:
                released = (
                    date.fromisoformat(album.release_date[:10]) if album.release_date else date.min
                )
            except ValueError:
                continue
            if released > date.today():
                continue
            choices.append((released, album.id, album))
        return max(choices, key=lambda value: value[:2])[2] if choices else None

    def retry_after(self, identifier):
        with self.connection() as db:
            row = db.execute(
                "SELECT retry_after FROM bridge_release_checks WHERE id=?", (identifier,)
            ).fetchone()
        return row[0] if row else 0

    def info(self, identifier):
        artist = self.core.cached_artist(identifier)
        album = self.selection(artist)
        if album:
            url = album.store_url
            try:
                p = urlsplit(url)
                if (
                    p.scheme != "https"
                    or p.hostname not in {"music.apple.com", "itunes.apple.com"}
                    or p.username
                    or p.password
                    or p.port not in (None, 443)
                ):
                    url = ""
            except ValueError:
                url = ""
            return {
                "status": "ready",
                "imageKind": "release-artwork",
                "albumId": self.core.identity(album),
                "albumTitle": album.album_title or album.title,
                "releaseDate": album.release_date,
                "sourceUrl": url,
                "selection": "newest-known-available-release",
            }
        with self.lock:
            pending = identifier in self.pending
        return {"status": "pending" if pending else "unavailable", "imageKind": "release-artwork"}

    def get(self, identifier):
        artist = self.core.cached_artist(identifier)
        album = self.selection(artist)
        if album:
            return album
        if self.retry_after(identifier) > time.time():
            raise ReleaseUnavailable("release-unavailable")
        with self.lock:
            if self.stop.is_set():
                raise ReleaseUnavailable("shutdown")
            if identifier in self.pending:
                raise ReleaseUnavailable("release-pending")
            now = time.monotonic()
            while self.starts and self.starts[0] <= now - 60:
                self.starts.popleft()
            if len(self.pending) >= 4 or len(self.starts) >= 8:
                raise ReleaseUnavailable("release-busy")
            self.starts.append(now)
            future = self.pool.submit(self.enrich, identifier, artist)
            self.pending[identifier] = future

        def finished(_):
            with self.lock:
                self.pending.pop(identifier, None)

        future.add_done_callback(finished)
        # No HTTP worker waits for catalog enrichment. The client can retry later.
        raise ReleaseUnavailable("release-pending")

    def enrich(self, identifier, artist):
        delay = 300
        try:
            deadline = time.monotonic() + 30
            for _ in range(16):
                if self.stop.is_set() or time.monotonic() >= deadline:
                    return
                quiet_wait = self.actor.last_metadata + 1 - time.monotonic()
                if quiet_wait > 0:
                    if self.stop.wait(min(quiet_wait, max(0, deadline - time.monotonic()))):
                        return
                    continue
                try:
                    albums = self.catalog.artist_albums(artist.id, artist.country)
                    break
                except CatalogDeferred as exc:
                    if not 0 <= exc.seconds <= 30 or time.monotonic() + exc.seconds >= deadline:
                        return
                    if self.stop.wait(max(0.01, exc.seconds)):
                        return
            else:
                return
            if self.stop.is_set():
                return
            albums = [a for a in albums if a.artist_id == artist.id and a.country == artist.country]
            with self.connection() as db:
                for album in albums[:200]:
                    identity = self.core.identity(album)
                    self.core.parse(identity, "album")
                    db.execute(
                        "INSERT OR REPLACE INTO bridge_metadata VALUES (?,?)",
                        (identity, json.dumps(asdict(album))),
                    )
                db.execute(
                    "DELETE FROM bridge_metadata WHERE rowid NOT IN "
                    "(SELECT rowid FROM bridge_metadata ORDER BY rowid DESC LIMIT 5000)"
                )
            delay = 3600
        except Exception:
            pass  # No provider payloads, credentials, or URLs in diagnostics.
        finally:
            if not self.stop.is_set():
                try:
                    with self.connection() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO bridge_release_checks VALUES (?,?)",
                            (identifier, time.time() + delay),
                        )
                        db.execute(
                            "DELETE FROM bridge_release_checks WHERE id NOT IN "
                            "(SELECT id FROM bridge_release_checks "
                            "ORDER BY retry_after DESC LIMIT 1024)"
                        )
                except sqlite3.Error:
                    pass

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
