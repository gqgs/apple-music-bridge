"""Serialized catalog/cache ownership; completed files are leased to HTTP workers."""

import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import closing
from dataclasses import asdict
from datetime import date
from urllib.parse import quote

from ..cache import Cache
from ..catalog import Catalog, CatalogDeferred, grouped_releases
from ..downloader import Downloader
from ..model import Album, Artist, PlayerError, Track
from ..runtime import ManagedRuntime
from ..runtime_install import RuntimeStore
from .metrics import Metrics
from .release_types import release_types


class BridgeError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class MediaMissing(BridgeError):
    pass


class Core:
    def __init__(
        self,
        root,
        limit,
        country="br",
        quality="lossless",
        catalog=None,
        downloader_factory=None,
        runtime_root=None,
        cancel=None,
    ):
        if not re.fullmatch(r"[a-z]{2}", country) or quality not in {"lossless", "atmos"}:
            raise ValueError("Invalid storefront or quality")
        self.country, self.quality = country, quality
        self.cancel = cancel or threading.Event()
        self.cache = Cache(root, limit)
        self.catalog = catalog or Catalog()
        self.runtime = self.downloader = None
        self.pins = {}
        self.operation_deadline = None
        self._counts = None
        self.cache.db.execute(
            "CREATE TABLE IF NOT EXISTS bridge_metadata (id TEXT PRIMARY KEY, data TEXT)"
        )
        self.cache.db.commit()
        try:
            if runtime_root is not None:
                self.runtime = ManagedRuntime(RuntimeStore(runtime_root))
                if not self.runtime.signed_in():
                    raise PlayerError("Complete managed setup in the player first.")
                self.runtime.start()
                self.downloader = Downloader(str(self.runtime.store.downloader), self.cache.staging)
            elif downloader_factory:
                self.downloader = downloader_factory(self.cache.staging)
        except Exception:
            self.close()
            raise

    def identity(self, item):
        kind = {Artist: "artist", Album: "album", Track: "song"}[type(item)]
        return f"am:{item.country}:{self.quality}:{kind}:{item.id}"

    def parse(self, identifier, kind=None):
        match = re.fullmatch(
            r"am:([a-z]{2}):(lossless|atmos):(artist|album|song):([0-9]{1,30})", identifier
        )
        if (
            not match
            or match[1] != self.country
            or match[2] != self.quality
            or (kind and match[3] != kind)
        ):
            raise BridgeError(70, "Unknown ID or different storefront/quality profile")
        return match[3], match[4]

    def remember(self, items):
        items = list(items)
        for item in items:
            identifier = self.identity(item)
            self.parse(identifier)
            self.cache.db.execute(
                "INSERT OR REPLACE INTO bridge_metadata VALUES (?,?)",
                (identifier, json.dumps(asdict(item))),
            )
        # Metadata is separate from media eviction and bounded independently.
        self.cache.db.execute(
            "DELETE FROM bridge_metadata WHERE rowid NOT IN "
            "(SELECT rowid FROM bridge_metadata ORDER BY rowid DESC LIMIT 5000)"
        )
        self.cache.db.commit()
        self._counts = None
        return list(items)

    def known(self, kind):
        rows = self.cache.db.execute(
            "SELECT id,data FROM bridge_metadata ORDER BY rowid DESC"
        ).fetchall()
        cls = {"artist": Artist, "album": Album, "song": Track}[kind]
        prefix = f"am:{self.country}:{self.quality}:{kind}:"
        return [
            cls(**json.loads(data)) for identifier, data in rows if identifier.startswith(prefix)
        ]

    def check_deadline(self):
        if self.operation_deadline is not None and time.monotonic() >= self.operation_deadline:
            raise BridgeError(0, "Catalog request expired; retry when ready")

    def cached_artwork(self, identifier):
        # Read-only, per-request SQLite connection: covers never queue behind a
        # catalog lookup/download. Unknown/evicted metadata gets a placeholder.
        kind, _ = self.parse(identifier)
        if kind == "artist":
            return ""
        uri = "file:" + quote(str(self.cache.root / "index.sqlite3")) + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as db:
                row = db.execute(
                    "SELECT data FROM bridge_metadata WHERE id=?", (identifier,)
                ).fetchone()
                data = json.loads(row[0]) if row else {}
                if kind == "song" and not data.get("artwork_url") and data.get("album_id"):
                    album_id = f"am:{self.country}:{self.quality}:album:{data['album_id']}"
                    self.parse(album_id, "album")
                    row = db.execute(
                        "SELECT data FROM bridge_metadata WHERE id=?", (album_id,)
                    ).fetchone()
                    data = json.loads(row[0]) if row else {}
                return data.get("artwork_url", "")
        except sqlite3.OperationalError:
            raise BridgeError(0, "Artwork metadata temporarily unavailable; retry later") from None

    def request(self, function, *args, **kwargs):
        budget = 30.0
        for attempt in range(4):
            self.check_deadline()
            if self.cancel.is_set():
                raise BridgeError(0, "Bridge stopping")
            try:
                result = function(*args, **kwargs)
                self.check_deadline()
                return result
            except CatalogDeferred as exc:
                if attempt == 3 or not 0 <= exc.seconds <= budget:
                    raise BridgeError(0, "Catalog rate limit; retry later") from None
                if (
                    self.operation_deadline is not None
                    and time.monotonic() + exc.seconds >= self.operation_deadline
                ):
                    raise BridgeError(0, "Catalog request expired; retry when ready") from None
                if self.operation_deadline is not None:
                    # HTTP callers defer outside the sole catalog/cache worker.
                    raise
                self.cancel.wait(exc.seconds)
                budget -= exc.seconds
        raise AssertionError("unreachable")

    def resolve(self, identifier, kind=None):
        actual, numeric = self.parse(identifier, kind)
        row = self.cache.db.execute(
            "SELECT data FROM bridge_metadata WHERE id=?", (identifier,)
        ).fetchone()
        cls = {"artist": Artist, "album": Album, "song": Track}[actual]
        if row:
            return cls(**json.loads(row[0]))
        # Stable IDs survive cache eviction and server restarts without guessing names.
        data = self.request(self.catalog.request, "lookup", id=numeric, country=self.country)
        parse = {
            "artist": self.catalog.artists,
            "album": self.catalog.albums,
            "song": self.catalog.tracks,
        }[actual]
        result = [item for item in parse(data, self.country) if item.id == numeric]
        if not result:
            raise BridgeError(70, "Catalog item unavailable")
        return self.remember(result)[0]

    def albums(self, artist):
        values = self.remember(self.request(self.catalog.artist_albums, artist.id, artist.country))
        return [album for _, group in grouped_releases(values) for album in group]

    def songs(self, album):
        return self.remember(self.request(self.catalog.album, album.id, album.country))

    def search(self, query, kind, count, offset):
        if not count:
            return []
        if not query.strip():
            values = self.known(kind)
        else:
            entity = {"artist": "musicArtist", "album": "album", "song": "song"}[kind]
            values = self.remember(self.request(self.catalog.search, query, self.country, entity))
        return values[offset : offset + count]

    def child_counts(self):
        if self._counts is None:
            self._counts = (
                Counter(a.artist_id for a in self.known("album")),
                Counter(t.album_id for t in self.known("song")),
            )
        return self._counts

    def describe(self, item):
        result = {"id": self.identity(item), "name": item.title, "coverArt": self.identity(item)}
        if isinstance(item, Artist):
            result["albumCount"] = self.child_counts()[0][item.id]
            return result
        result.update(
            name=item.album_title if isinstance(item, Album) else item.track_name or item.title,
            artist=item.artist,
        )
        # Catalog releaseDate may describe a reissue, not the original release.
        # Missing/invalid dates remain unknown rather than becoming a fake year.
        try:
            released = date.fromisoformat(item.release_date[:10])
        except ValueError:
            pass
        else:
            result["year"] = released.year
            if isinstance(item, Album):
                result["releaseDate"] = {
                    "year": released.year,
                    "month": released.month,
                    "day": released.day,
                }
        if item.artist_id:
            result["artistId"] = f"am:{item.country}:{self.quality}:artist:{item.artist_id}"
        if isinstance(item, Album):
            result.update(
                songCount=self.child_counts()[1][item.id],
                duration=0,
                releaseTypes=release_types(item.album_title),
            )
        else:
            result.update(
                title=result.pop("name"),
                isDir=False,
                isVideo=False,
                type="music",
                album=item.album_title,
                duration=item.duration_ms // 1000,
                track=item.track_number,
                discNumber=item.disc_number,
            )
            if item.album_id:
                result["albumId"] = f"am:{item.country}:{self.quality}:album:{item.album_id}"
            hit = self.cache.get(item.key(self.quality))
            if hit:
                result.update(
                    size=hit[0].stat().st_size,
                    bitDepth=hit[1].bits,
                    samplingRate=hit[1].rate,
                    channelCount=hit[1].channels,
                )
        return result

    def acquire(self, identifier, cached_only=False):
        track = self.resolve(identifier, "song")
        key = track.key(self.quality)
        hit = self.cache.get(key)
        if not hit:
            if cached_only:
                raise MediaMissing(70, "Media not cached")
            job = None
            try:
                job, source, media = self.download(track)
                self.cache.put(key, source, media)
            finally:
                if job:
                    shutil.rmtree(job)
            hit = self.cache.get(key)
        path, media = hit
        self.validate_quality(media)
        self.cache.touch(key)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_size:
            os.close(fd)
            raise BridgeError(70, "Cached file is not a private nonempty regular file")
        stream = os.fdopen(fd, "rb")
        self.pins[key] = self.pins.get(key, 0) + 1
        self.cache.protected.add(key)
        return key, stream, os.fstat(fd).st_size, media

    def download(self, track):
        # Called on the dedicated download worker; no SQLite/cache mutations here.
        if not self.downloader:
            raise BridgeError(
                70, "Not cached; explicitly start the configured managed runtime for downloads"
            )
        if self.cancel.is_set():
            raise BridgeError(0, "Bridge stopping")
        if self.runtime:
            for _ in range(100):
                self.runtime.poll()
                if self.runtime.ready:
                    break
                if self.cancel.wait(0.1):
                    raise BridgeError(0, "Bridge stopping")
            if not self.runtime.ready or self.country not in self.runtime.regions:
                raise BridgeError(0, "Managed backend not ready for this storefront")
        job, source, media = self.downloader.run(track, self.quality, self.cancel)
        try:
            self.validate_quality(media)
        except Exception:
            shutil.rmtree(job)
            raise
        return job, source, media

    def commit_download(self, track, source, media):
        # Commit and lease atomically with respect to other cache mutations.
        self.validate_quality(media)
        self.cache.put(track.key(self.quality), source, media)
        return self.acquire(self.identity(track), cached_only=True)

    def validate_quality(self, media):
        if self.quality == "lossless" and media.codec not in {"alac", "flac"}:
            raise BridgeError(0, "Requested lossless but received lossy media; fallback refused")
        if self.quality == "atmos" and media.codec != "eac3":
            raise BridgeError(0, "Requested Atmos but received non-E-AC-3 media; fallback refused")

    def release(self, key):
        self.pins[key] -= 1
        if not self.pins[key]:
            del self.pins[key]
            self.cache.protected.discard(key)
        self.cache.evict()

    def close(self):
        try:
            if self.runtime:
                self.runtime.close()
        finally:
            self.cache.close()


class Actor:
    """One catalog/cache owner and one bounded download worker; shared catalog limiter."""

    def __init__(self, factory):
        self.cancel = threading.Event()
        self.metrics = Metrics()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bridge-core")
        self.metadata_pending = threading.BoundedSemaphore(4)
        future = self.pool.submit(factory, self.cancel)
        try:
            self.core = future.result()
        except BaseException:
            self.cancel.set()
            try:
                core = future.result()
                self.pool.submit(core.close).result()
            except Exception:
                pass
            finally:
                self.pool.shutdown()
            raise

        self.download_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bridge-download")
        self.download_slots = threading.BoundedSemaphore(4)

    def acquire(self, identifier, cached_only=False):
        try:
            return self.call("acquire", identifier, True)
        except MediaMissing:
            if cached_only:
                raise
        if not self.download_slots.acquire(blocking=False):
            self.metrics.record("download", "busy")
            raise BridgeError(0, "Download queue busy; retry later")

        def run():
            start = time.monotonic()
            job = None
            try:
                if self.cancel.is_set():
                    raise BridgeError(0, "Bridge stopping")
                try:
                    return self.call("acquire", identifier, True)
                except MediaMissing:
                    pass
                track = self.call("resolve", identifier, "song")
                job, source, media = self.core.download(track)
                result = self.call("commit_download", track, source, media)
                self.metrics.record("download", "ok", time.monotonic() - start)
                return result
            except Exception:
                self.metrics.record(
                    "download",
                    "cancelled" if self.cancel.is_set() else "failed",
                    time.monotonic() - start,
                )
                raise
            finally:
                if job:
                    shutil.rmtree(job)

        try:
            future = self.download_pool.submit(run)
        except Exception:
            self.download_slots.release()
            raise
        future.add_done_callback(lambda _: self.download_slots.release())
        return future.result()

    def call(self, method, *args, **kwargs):
        return self.pool.submit(getattr(self.core, method), *args, **kwargs).result()

    def metadata(self, method, params, timeout=20):
        if not self.metadata_pending.acquire(blocking=False):
            self.metrics.record("metadata_queue", "busy")
            raise BridgeError(0, "Catalog busy; retry later")
        queued = time.monotonic()
        deadline = queued + timeout
        future = None

        def invoke(submitted):
            started = time.monotonic()
            self.metrics.record("metadata_queue", "ok", started - submitted)
            self.core.operation_deadline = deadline
            outcome = "ok"
            try:
                self.core.check_deadline()
                if self.cancel.is_set():
                    raise BridgeError(0, "Bridge stopping")
                return self.core.dispatch(method, params)
            except CatalogDeferred:
                outcome = "deferred"
                raise
            except Exception:
                outcome = "failed"
                raise
            finally:
                self.metrics.record("metadata_work", outcome, time.monotonic() - started)
                self.core.operation_deadline = None

        try:
            # Replay dispatch after deferral: completed catalog responses are cached.
            # Admission spans all attempts; neither queued time nor Retry-After resets
            # the original deadline. Bound wakeups even for an invalid zero delay.
            for _ in range(32):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if self.cancel.is_set():
                    raise BridgeError(0, "Bridge stopping")
                future = self.pool.submit(invoke, time.monotonic())
                try:
                    result = future.result(timeout=remaining)
                except CatalogDeferred as exc:
                    delay = max(0.01, exc.seconds)
                    if not 0 <= exc.seconds <= 30 or time.monotonic() + delay >= deadline:
                        raise BridgeError(0, "Catalog rate limit; retry later") from None
                    started = time.monotonic()
                    stopped = self.cancel.wait(delay)
                    self.metrics.record(
                        "catalog_wait",
                        "rate_limited" if exc.rate_limited else "deferred",
                        time.monotonic() - started,
                    )
                    if stopped:
                        raise BridgeError(0, "Bridge stopping") from None
                    continue
                self.metrics.record("metadata", "ok", time.monotonic() - queued)
                return result
            raise BridgeError(0, "Catalog rate limit; retry later")
        except TimeoutError:
            self.metrics.record("metadata", "timeout", time.monotonic() - queued)
            raise BridgeError(0, "Catalog busy or timed out; retry later") from None
        except Exception:
            self.metrics.record("metadata", "failed", time.monotonic() - queued)
            raise
        finally:
            # Timed-out work retains admission until drained. Cancelling a Future
            # leaves its executor work item queued and could permit unbounded buildup.
            if future is None:
                self.metadata_pending.release()
            else:
                future.add_done_callback(lambda _: self.metadata_pending.release())

    def close(self):
        self.cancel.set()
        self.download_pool.shutdown(wait=True, cancel_futures=True)
        try:
            self.pool.submit(self.core.close).result()
        finally:
            self.pool.shutdown()
