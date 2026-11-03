"""Serialized catalog/cache ownership; completed files are leased to HTTP workers."""

import json
import os
import re
import shutil
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date

from ..cache import Cache
from ..catalog import Catalog, CatalogDeferred, grouped_releases
from ..downloader import Downloader
from ..model import Album, Artist, PlayerError, Track
from ..runtime import ManagedRuntime
from ..runtime_install import RuntimeStore


class BridgeError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


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

    def request(self, function, *args, **kwargs):
        budget = 30.0
        for attempt in range(4):
            if self.cancel.is_set():
                raise BridgeError(0, "Bridge stopping")
            try:
                return function(*args, **kwargs)
            except CatalogDeferred as exc:
                if attempt == 3 or not 0 <= exc.seconds <= budget:
                    raise BridgeError(0, "Catalog rate limit; retry later") from None
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

    def describe(self, item):
        result = {"id": self.identity(item), "name": item.title, "coverArt": self.identity(item)}
        if isinstance(item, Artist):
            result["albumCount"] = len([a for a in self.known("album") if a.artist_id == item.id])
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
                songCount=len([t for t in self.known("song") if t.album_id == item.id]), duration=0
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
            if cached_only or not self.downloader:
                raise BridgeError(
                    70, "Not cached; explicitly start the configured managed runtime for downloads"
                )
            if self.runtime:
                for _ in range(100):
                    self.runtime.poll()
                    if self.runtime.ready:
                        break
                    if self.cancel.wait(0.1):
                        raise BridgeError(0, "Bridge stopping")
                if not self.runtime.ready or self.country not in self.runtime.regions:
                    raise BridgeError(0, "Managed backend not ready for this storefront")
            job = None
            try:
                job, source, media = self.downloader.run(track, self.quality, self.cancel)
                self.validate_quality(media)
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
    """SQLite and subprocess ownership stay on a single worker, including close."""

    def __init__(self, factory):
        self.cancel = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bridge-core")
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

    def call(self, method, *args, **kwargs):
        return self.pool.submit(getattr(self.core, method), *args, **kwargs).result()

    def close(self):
        self.cancel.set()
        try:
            self.pool.submit(self.core.close).result()
        finally:
            self.pool.shutdown()
