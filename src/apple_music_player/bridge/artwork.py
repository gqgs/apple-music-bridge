"""Bounded artwork workers, separate from serialized catalog/media ownership."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QBuffer, QIODevice

from ..artwork import BADGE_URL, ArtworkCache, allowed_url


class Covers:
    def __init__(self, root):
        self.cache = ArtworkCache(root, validator=lambda url: allowed_url(url) and url != BADGE_URL)
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
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if not image.save(buffer, "PNG"):
            return b""
        return bytes(buffer.data())

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

    def get(self, url):
        # Leave HTTP capacity for ping, browsing and streaming during art stalls.
        if not self.waiters.acquire(blocking=False):
            return b""
        try:
            return self._get(url)
        finally:
            self.waiters.release()

    def _get(self, url):
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
            return future.result(timeout=20)
        except Exception:
            return b""  # Sanitized placeholder, never HTTP bodies or URLs.

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
