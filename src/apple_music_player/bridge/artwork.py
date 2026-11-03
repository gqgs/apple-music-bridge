"""Bounded artwork workers, separate from serialized catalog/media ownership."""

import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage

from ..artwork import BADGE_URL, ArtworkCache, allowed_url
from .metrics import Metrics


def sized_url(url, size):
    """Best-effort CDN convention, not an iTunes API guarantee."""
    if not allowed_url(url) or url == BADGE_URL:
        return url
    parts = urlsplit(url)
    match = re.fullmatch(r"(.*/)([1-9][0-9]{0,3})x\2(bb\.(?:jpg|jpeg|png|webp))", parts.path)
    if parts.query or not parts.path.startswith("/image/thumb/") or not match:
        return url
    bucket = next(n for n in (128, 256, 400, 512, 1024) if n >= size)
    if int(match[2]) >= size:
        return url
    return urlunsplit(parts._replace(path=f"{match[1]}{bucket}x{bucket}{match[3]}"))


def png(image):
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    return bytes(buffer.data()) if image.save(buffer, "PNG") else b""


class CoverUnavailable(Exception):
    """A temporary/error response, never a successful placeholder image."""


class Covers:
    def __init__(self, root, metrics=None):
        self.cache = ArtworkCache(
            root, validator=lambda url: allowed_url(url) and url != BADGE_URL, max_dimension=1024
        )
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bridge-art")
        self.lock = threading.Lock()
        self.pending = {}
        self.failures = OrderedDict()
        self.metrics = metrics or Metrics()
        self.closed = False

    def load(self, url):
        start = time.monotonic()
        image = self.cache.load(url)
        self.metrics.record(
            "cover_fetch",
            self.cache.failures.get(url, "unavailable") if image.isNull() else "ok",
            time.monotonic() - start,
        )
        return b"" if image.isNull() else png(image)

    def finished(self, url, future):
        try:
            success = bool(future.result())
        except Exception:
            success = False
        with self.lock:
            self.pending.pop(url, None)
            if success:
                self.failures.pop(url, None)
            else:
                attempts = self.failures.get(url, (0, 0))[0] + 1
                reason = self.cache.failures.get(url, "unavailable")
                retry = time.monotonic() + (
                    30 if reason in {"network", "server"} and attempts < 3 else 300
                )
                self.failures[url] = (attempts, retry)
                self.failures.move_to_end(url)
                while len(self.failures) > 256:
                    old, _ = self.failures.popitem(last=False)
                    with self.cache.lock:
                        self.cache.failures.pop(old, None)

    def get(self, url, size=400):
        if not url:
            self.metrics.record("cover_wait", "missing")
            return b""  # Only genuinely absent metadata gets a successful placeholder.
        start = time.monotonic()
        size = max(1, min(1024, int(size) or 400))
        deadline = start + 20
        candidate = sized_url(url, size)
        try:
            try:
                data = self._get(candidate, deadline)
            except CoverUnavailable:
                if candidate == url or time.monotonic() >= deadline:
                    raise
                data = self._get(url, deadline)
            image = QImage.fromData(data)
            if max(image.width(), image.height()) > size:
                data = png(image.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.metrics.record("cover_wait", "ok", time.monotonic() - start)
            return data
        except CoverUnavailable:
            self.metrics.record("cover_wait", "failed", time.monotonic() - start)
            raise

    def _get(self, url, deadline):
        if not allowed_url(url) or url == BADGE_URL:
            raise CoverUnavailable()
        # Warm disk hits don't consume a network worker or pending admission slot.
        image = self.cache.load(url, cache_only=True)
        if not image.isNull():
            self.metrics.record("cover_cache", "hit")
            return png(image)
        self.metrics.record("cover_cache", "miss")
        with self.lock:
            attempts, after = self.failures.get(url, (0, 0))
            if self.closed:
                raise CoverUnavailable()
            if time.monotonic() < after:
                self.metrics.record("cover_cache", "negative")
                raise CoverUnavailable()
            if attempts >= 3:
                self.failures.pop(url, None)
            future = self.pending.get(url)
            if future is None:
                if len(self.pending) >= 8:
                    self.metrics.record("cover_wait", "busy")
                    raise CoverUnavailable()
                future = self.pool.submit(self.load, url)
                self.pending[url] = future
                created = True
            else:
                created = False
        if created:
            future.add_done_callback(lambda f: self.finished(url, f))
        try:
            data = future.result(timeout=max(0, deadline - time.monotonic()))
            if not data:
                raise CoverUnavailable()
            return data
        except TimeoutError:
            self.metrics.record("cover_wait", "timeout")
            raise CoverUnavailable() from None
        except Exception:
            raise CoverUnavailable() from None

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
