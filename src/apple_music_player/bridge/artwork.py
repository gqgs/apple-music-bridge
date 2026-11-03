"""Bounded artwork workers, separate from serialized catalog/media ownership."""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage

from ..artwork import BADGE_URL, ArtworkCache, allowed_url


def sized_url(url, size):
    """Best-effort CDN convention, not an iTunes API guarantee."""
    if not allowed_url(url) or url == BADGE_URL:
        return url
    parts = urlsplit(url)
    match = re.fullmatch(r"(.*/)([1-9][0-9]{0,3})x\2(bb\.(?:jpg|jpeg|png|webp))", parts.path)
    if parts.query or not parts.path.startswith("/image/thumb/") or not match:
        return url
    bucket = next(n for n in (128, 256, 512, 1024) if n >= size)
    if int(match[2]) >= size:
        return url
    return urlunsplit(parts._replace(path=f"{match[1]}{bucket}x{bucket}{match[3]}"))


def png(image):
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    return bytes(buffer.data()) if image.save(buffer, "PNG") else b""


class Covers:
    def __init__(self, root):
        self.cache = ArtworkCache(
            root, validator=lambda url: allowed_url(url) and url != BADGE_URL, max_dimension=1024
        )
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bridge-art")
        self.lock = threading.Lock()
        self.waiters = threading.BoundedSemaphore(4)
        self.pending = {}
        self.failures = {}
        self.closed = False

    def load(self, url):
        image = self.cache.load(url)
        if image.isNull():
            return b""
        # Decode first, then encode a client-supported format. Neither the URL
        # suffix nor the remote Content-Type proves the format of the bytes.
        return png(image)

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
                retry = time.monotonic() + 30 if reason in {"network", "server"} else float("inf")
                self.failures[url] = (attempts, retry)

    def get(self, url, size=512):
        # Leave HTTP capacity for ping, browsing and streaming during art stalls.
        if not self.waiters.acquire(blocking=False):
            return b""
        try:
            size = max(1, min(1024, int(size) or 512))
            deadline = time.monotonic() + 20
            candidate = sized_url(url, size)
            data = self._get(candidate, deadline)
            if not data and candidate != url and time.monotonic() < deadline:
                data = self._get(url, deadline)
            if data:
                image = QImage.fromData(data)
                if max(image.width(), image.height()) > size:
                    return png(
                        image.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    )
            return data
        finally:
            self.waiters.release()

    def _get(self, url, deadline):
        if not allowed_url(url) or url == BADGE_URL:
            return b""
        with self.lock:
            attempts, after = self.failures.get(url, (0, 0))
            if self.closed or attempts >= 3 or time.monotonic() < after:
                return b""
            future = self.pending.get(url)
            if future is None:
                if len(self.pending) >= 8 or len(self.failures) >= 256:
                    return b""
                future = self.pool.submit(self.load, url)
                self.pending[url] = future
                # Register outside the lock: completed futures call back inline.
                created = True
            else:
                created = False
        if created:
            future.add_done_callback(lambda f: self.finished(url, f))
        try:
            return future.result(timeout=max(0, deadline - time.monotonic()))
        except Exception:
            return b""  # Sanitized placeholder, never HTTP bodies or URLs.

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
