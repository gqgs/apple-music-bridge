"""Bounded public artwork fetching; no account state, URL rewriting or raw logs."""

import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QImageReader

from .cache import safe_path
from .runtime_install import private_dir

BADGE_URL = (
    "https://marketing.services.apple/api/storage/images/"
    "64e3de01fe821600071349d4/en-us-large%401x.png"
)
MAX_BYTES = 2 * 1024**2


def allowed_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        p = urlsplit(url)
        return bool(
            p.scheme == "https"
            and not p.username
            and not p.password
            and p.port in (None, 443)
            and not p.fragment
            and (url == BADGE_URL or re.fullmatch(r"is[0-9]+-ssl\.mzstatic\.com", p.hostname or ""))
        )
    except ValueError:
        return False


class ArtworkRedirect(HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise ValueError("Unsafe artwork redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def decode(data):
    if len(data) > MAX_BYTES:
        raise ValueError("Artwork too large")
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if (
        bytes(reader.format()) not in (b"png", b"jpeg", b"webp")
        or not size.isValid()
        or size.width() > 2048
        or size.height() > 2048
    ):
        raise ValueError("Unsupported artwork")
    image = reader.read()
    if image.isNull():
        raise ValueError("Corrupt artwork")
    if max(image.width(), image.height()) > 256:
        image = image.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return image


class ArtworkCache:
    def __init__(self, root, limit=32 * 1024**2):
        self.root = private_dir(root)
        self.limit = limit
        self.lock = threading.Lock()
        with self.lock:
            for path in self.root.glob(".part-*"):
                safe_path(self.root, path)
                if path.is_file() and path.stat().st_nlink == 1:
                    path.unlink()
            self.evict()

    def evict(self, incoming=0):
        files = []
        for path in self.root.iterdir():
            if re.fullmatch(r"[0-9a-f]{64}\.img", path.name):
                safe_path(self.root, path)
                if path.is_file() and path.stat().st_nlink == 1:
                    files.append(path)
        total = sum(p.stat().st_size for p in files)
        files.sort(key=lambda p: p.stat().st_mtime)
        while files and (total + incoming > self.limit or len(files) >= 512):
            path = files.pop(0)
            total -= path.stat().st_size
            path.unlink()

    def load(self, url):
        if not allowed_url(url):
            return QImage()
        path = self.root / (sha256(url.encode()).hexdigest() + ".img")
        try:
            with self.lock:
                safe_path(self.root, path)
                if path.exists():
                    if path.stat().st_nlink != 1:
                        return QImage()
                    try:
                        if time.time() - path.stat().st_mtime < 7 * 86400:
                            with path.open("rb") as stream:
                                image = decode(stream.read(MAX_BYTES + 1))
                            os.utime(path, None)
                            return image
                    except (ValueError, OSError):
                        pass
                    path.unlink()
            opener = build_opener(ArtworkRedirect())
            request = Request(
                url, headers={"User-Agent": "NativeMusicPlayer/0.1", "Accept": "image/*"}
            )
            with opener.open(request, timeout=8) as response:
                if not allowed_url(response.url):
                    return QImage()
                if response.headers.get_content_type() not in (
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                ):
                    return QImage()
                chunks, size = [], 0
                deadline = time.monotonic() + 15
                read = getattr(response, "read1", response.read)
                while size <= MAX_BYTES:
                    if time.monotonic() > deadline:
                        return QImage()
                    chunk = read(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                data = b"".join(chunks)
            image = decode(data)
            if len(data) <= self.limit:
                with self.lock:
                    safe_path(self.root, path)
                    self.evict(len(data))
                    fd, temporary = tempfile.mkstemp(prefix=".part-", dir=self.root)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(data)
                        os.replace(temporary, path)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
            return image
        except Exception:
            return QImage()  # Never expose response bodies, URLs or network diagnostics.


class Artwork:
    def __init__(self, root):
        self.cache = ArtworkCache(root)
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.pending = {}
        self.images = OrderedDict()
        self.closed = False
        self.revision = 0

    def get(self, url):
        if self.closed or not allowed_url(url):
            return QImage()
        if url in self.images:
            self.images.move_to_end(url)
            return self.images[url]
        if url not in self.pending and len(self.pending) < 16:
            self.pending[url] = self.pool.submit(self.cache.load, url)
        return QImage()

    def poll(self):
        for url, future in list(self.pending.items()):
            if future.done():
                self.images[url] = future.result()
                del self.pending[url]
                self.revision += 1
        while len(self.images) > 64:
            self.images.popitem(last=False)

    def close(self):
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
