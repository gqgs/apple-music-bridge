"""Artist release covers: cached reads and bounded, demand-driven catalog enrichment."""

import json
import math
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict
from urllib.parse import quote, urlsplit

from ..catalog import Catalog, CatalogDeferred
from ..model import Album
from .artwork import CoverUnavailable
from .artwork_retention import rank, retain, saved_album
from .artwork_seed import ArtworkSeed


def safe_reason(reason):
    return (
        reason
        if reason
        in {
            "release-pending",
            "release-unavailable",
            "release-busy",
            "release-failed",
            "release-deferred",
            "release-resolved",
            "shutdown",
            "lookup-failed",
        }
        else "lookup-failed"
    )


class ReleaseUnavailable(CoverUnavailable):
    def __init__(self, reason, retry_after=3):
        self.reason = safe_reason(reason)
        self.retry_after = max(1, min(3600, math.ceil(retry_after)))
        self.status = 404 if reason == "release-unavailable" else 503


class ReleaseArtwork:
    def __init__(self, actor, catalog=None):
        self.actor = actor
        self.core = actor.core
        self.catalog = catalog or Catalog(limiter=self.core.catalog.limiter)
        self.uri = "file:" + quote(str(self.core.cache.root / "index.sqlite3")) + "?mode=rw"
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.pending = {}
        self.metrics = actor.metrics
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="artist-release")
        self.seed = ArtworkSeed(self)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.uri, uri=True, timeout=0.2)) as db:
            with db:
                yield db

    def selection(self, artist):
        with self.connection() as db:
            rows = db.execute(
                "SELECT m.data,r.album FROM bridge_metadata m "
                "LEFT JOIN bridge_artist_releases r ON r.album=m.id AND r.artist=? "
                "WHERE (json_extract(m.data, '$.artist_id')=? OR r.album IS NOT NULL) "
                "AND m.id LIKE ? ORDER BY json_extract(m.data, '$.release_date') "
                "DESC,m.id LIMIT 200",
                (
                    self.core.identity(artist),
                    artist.id,
                    f"am:{artist.country}:{self.core.quality}:album:%",
                ),
            ).fetchall()
            saved = db.execute(
                "SELECT album FROM bridge_artist_artwork WHERE id=?",
                (self.core.identity(artist),),
            ).fetchone()
            if saved and saved[0]:
                album = saved_album(saved[0], self.core.identity(artist), self.core.identity)
                if album:
                    rows.append((saved[0], self.core.identity(album)))
        choices = []
        for raw, associated in rows:
            album = Album(**json.loads(raw))
            if album.country != artist.country or (
                album.artist_id != artist.id and associated != self.core.identity(album)
            ):
                continue
            candidate_rank = rank(album)
            if candidate_rank is not None:
                choices.append((*candidate_rank, album))
        return max(choices, key=lambda value: value[:2])[2] if choices else None

    def check(self, identifier):
        with self.connection() as db:
            row = db.execute(
                "SELECT retry_after,reason FROM bridge_release_checks WHERE id=?", (identifier,)
            ).fetchone()
        return row if row and row[1] != "release-resolved" else (0, "release-failed")

    def retry_after(self, identifier):
        return self.check(identifier)[0]

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
            self.metrics.record("artist_release", "hit")
            return album
        after, reason = self.check(identifier)
        after -= time.time()
        if after > 0:
            self.metrics.record("artist_release", "negative")
            raise ReleaseUnavailable(reason, after)
        with self.lock:
            if self.stop.is_set():
                raise ReleaseUnavailable("shutdown")
            if identifier in self.pending:
                self.metrics.record("artist_release", "joined")
                raise ReleaseUnavailable("release-pending", self.seed.retry_after())
            if len(self.pending) >= 4:
                self.metrics.record("artist_release", "busy")
                raise ReleaseUnavailable("release-busy", self.seed.retry_after())
            self.metrics.record("artist_release", "miss")
            future = self.pool.submit(self.enrich, identifier, artist)
            self.pending[identifier] = future

        def finished(_):
            with self.lock:
                self.pending.pop(identifier, None)

        future.add_done_callback(finished)
        # No HTTP worker waits for catalog enrichment. The client can retry later.
        raise ReleaseUnavailable("release-pending", self.seed.retry_after())

    def enrich(self, identifier, artist):
        started = time.monotonic()
        delay = 300
        reason = "release-failed"
        try:
            deadline = time.monotonic() + 30
            for _ in range(16):
                if self.stop.is_set() or time.monotonic() >= deadline:
                    return
                self.seed.run()
                while not self.actor.metadata_idle.is_set():
                    reason, delay = "release-deferred", 3
                    if time.monotonic() >= deadline or self.stop.wait(0.1):
                        return
                quiet_wait = self.actor.last_metadata + 1 - time.monotonic()
                if quiet_wait > 0:
                    reason, delay = "release-deferred", 3
                    if self.stop.wait(min(quiet_wait, max(0, deadline - time.monotonic()))):
                        return
                    continue
                if self.stop.is_set() or time.monotonic() >= deadline:
                    reason, delay = "release-deferred", 3
                    return
                # Recheck after foreground/quiet/limiter waits, immediately before admission.
                if self.selection(artist):
                    reason, delay = "release-resolved", 3600
                    return
                try:
                    albums = self.catalog.artist_albums(artist.id, artist.country)
                    break
                except CatalogDeferred as exc:
                    reason = "release-deferred"
                    delay = (
                        max(3, min(3600, math.ceil(exc.seconds)))
                        if math.isfinite(exc.seconds)
                        else 300
                    )
                    if not 0 <= exc.seconds <= 30 or time.monotonic() + exc.seconds >= deadline:
                        return
                    if self.stop.wait(max(0.01, exc.seconds)):
                        return
            else:
                return
            if self.stop.is_set():
                return
            albums = [a for a in albums if a.country == artist.country]
            self.store_albums(albums, artist)
            delay = 3600
            reason = "release-resolved" if self.selection(artist) else "release-unavailable"
        except Exception:
            reason, delay = "release-failed", 300
            # No provider payloads, credentials, or URLs in diagnostics.
        finally:
            outcome = (
                "cancelled"
                if self.stop.is_set()
                else {"release-failed": "failed", "release-deferred": "deferred"}.get(reason, "ok")
            )
            self.metrics.record("artist_release_work", outcome, time.monotonic() - started)
            if not self.stop.is_set():
                try:
                    with self.connection() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO bridge_release_checks "
                            "(id,retry_after,reason) VALUES (?,?,?)",
                            (identifier, time.time() + delay, reason),
                        )
                        db.execute(
                            "DELETE FROM bridge_release_checks WHERE id NOT IN "
                            "(SELECT id FROM bridge_release_checks "
                            "ORDER BY retry_after DESC LIMIT 1024)"
                        )
                except sqlite3.Error:
                    pass

    def store_albums(self, albums, artist=None, query=None):
        """Publish bounded metadata; only exact lookups grant collaborator provenance."""
        with self.connection() as db:
            for album in albums[:200]:
                identity = self.core.identity(album)
                self.core.parse(identity, "album")
                db.execute(
                    "INSERT OR REPLACE INTO bridge_metadata VALUES (?,?)",
                    (identity, json.dumps(asdict(album))),
                )
                if artist is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO bridge_artist_releases VALUES (?,?)",
                        (self.core.identity(artist), identity),
                    )
            retain(db, ([artist] if artist else []) + albums[:200], self.core.identity)
            db.execute(
                "DELETE FROM bridge_artist_releases WHERE rowid NOT IN "
                "(SELECT rowid FROM bridge_artist_releases ORDER BY rowid DESC LIMIT 10000)"
            )
            db.execute(
                "DELETE FROM bridge_metadata WHERE rowid NOT IN "
                "(SELECT rowid FROM bridge_metadata ORDER BY rowid DESC LIMIT 5000)"
            )
            if query is not None:
                # Only the final commit holds the generation lock, not SQLite
                # writer admission, parsing, or retention work.
                with self.seed.lock:
                    if (
                        query is not self.seed.current
                        or query.cancelled.is_set()
                        or self.stop.is_set()
                    ):
                        db.rollback()
                        return False
                    db.commit()
        return True

    def close(self):
        self.stop.set()
        self.seed.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
