"""Bounded public artwork fetching; no account state, URL rewriting or raw logs."""

import os
import re
import ssl
import tempfile
import threading
import time
from hashlib import sha256
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, UnidentifiedImageError

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

    def __init__(self, validator=allowed_url):
        super().__init__()
        self.validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.validator(newurl):
            raise ValueError("Unsafe artwork redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def decode(data, max_dimension=256, *, max_bytes=MAX_BYTES):
    if len(data) > max_bytes:
        raise ValueError("Artwork too large")
    try:
        with Image.open(BytesIO(data), formats=["PNG", "JPEG", "WEBP"]) as source:
            if not all(0 < n <= 2048 for n in source.size):
                raise ValueError("Unsupported artwork dimensions")
            # Decode completely while the input is owned here; never share lazy readers.
            source.load()
            image = source.convert("RGBA")
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        return image
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Corrupt or unsupported artwork") from exc


def png(image):
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class ArtworkCache:
    def __init__(self, root, limit=32 * 1024**2, validator=allowed_url, max_dimension=256):
        self.root = private_dir(root)
        self.allowed_url = validator
        self.max_dimension = max_dimension
        self.limit = limit
        self.lock = threading.Lock()
        self.failures = {}
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

    def load(self, url, cache_only=False):
        if not self.allowed_url(url):
            return None
        path = self.root / (sha256(url.encode()).hexdigest() + ".img")
        try:
            with self.lock:
                if not cache_only:
                    self.failures.pop(url, None)
                safe_path(self.root, path)
                if path.exists():
                    if path.stat().st_nlink != 1:
                        return None
                    try:
                        if time.time() - path.stat().st_mtime < 7 * 86400:
                            with path.open("rb") as stream:
                                image = decode(stream.read(MAX_BYTES + 1), self.max_dimension)
                            os.utime(path, None)
                            return image
                    except (ValueError, OSError):
                        pass
                    path.unlink()
            if cache_only:
                return None
            opener = build_opener(ArtworkRedirect(self.allowed_url))
            request = Request(
                url, headers={"User-Agent": "AppleMusicBridge/0.1", "Accept": "image/*"}
            )
            with opener.open(request, timeout=8) as response:
                if not self.allowed_url(response.url):
                    raise ValueError("Unsafe redirect")
                if response.headers.get_content_type() not in (
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                ):
                    raise ValueError("Unsupported media type")
                chunks, size = [], 0
                deadline = time.monotonic() + 15
                read = getattr(response, "read1", response.read)
                while size <= MAX_BYTES:
                    if time.monotonic() > deadline:
                        raise TimeoutError()
                    chunk = read(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                data = b"".join(chunks)
            image = decode(data, self.max_dimension)
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
        except HTTPError as exc:
            code = exc.code
            exc.close()
            reason = "denied" if code in (401, 403) else "server" if 500 <= code < 600 else "http"
        except URLError as exc:
            # A proxy CONNECT rejection may arrive wrapped as URLError rather than HTTPError.
            reason = "denied" if re.search(r"\b(401|403)\b", str(exc.reason)) else "network"
            if isinstance(exc.reason, ssl.SSLError):
                reason = "tls"
        except (TimeoutError, ConnectionError):
            reason = "network"
        except ssl.SSLError:
            reason = "tls"
        except ValueError:
            reason = "invalid"
        except Exception:
            reason = "unavailable"
        with self.lock:
            self.failures[url] = reason
        return None  # Never expose response bodies, URLs or network diagnostics.
