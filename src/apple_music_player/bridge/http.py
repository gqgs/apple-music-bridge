"""Bounded loopback HTTP subset. All paths, including cover art and stream, authenticate."""

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import struct
import threading
import xml.etree.ElementTree as ET
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..model import PlayerError
from .artwork import Covers, CoverUnavailable
from .core import BridgeError, Core

EXTENSIONS = [
    {"name": "apiKeyAuthentication", "versions": [1]},
    {"name": "formPost", "versions": [1]},
]


def auth_file(path, create=False):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError("Authentication path must not contain symlinks")
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump({"username": "bridge", "key": secrets.token_urlsafe(32)}, output)
        return
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or not 0 < info.st_size <= 2048
        ):
            raise ValueError("Authentication file must be a private owned regular file (0600)")
        data = json.loads(stream.read(2049))
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("key"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", data.get("key", ""))
        or data.get("username") != "bridge"
    ):
        raise ValueError("Invalid bridge authentication file")
    return data


def authenticate(params, credentials):
    present = set(params) & {"apiKey", "u", "p", "t", "s"}
    if "apiKey" in present:
        if present != {"apiKey"}:
            raise BridgeError(43, "Conflicting authentication mechanisms")
        valid = hmac.compare_digest(params["apiKey"], credentials["key"])
    else:
        if "p" in present and present & {"t", "s"}:
            raise BridgeError(43, "Conflicting authentication mechanisms")
        valid = params.get("u") == credentials["username"]
        if "p" in params:
            password = params["p"]
            if password.startswith("enc:"):
                try:
                    password = bytes.fromhex(password[4:]).decode("utf8")
                except (ValueError, UnicodeError):
                    password = ""
            valid = valid and hmac.compare_digest(password, credentials["key"])
        elif "t" in params and "s" in params and 6 <= len(params["s"]) <= 128:
            expected = hashlib.md5(
                (credentials["key"] + params["s"]).encode(), usedforsecurity=False
            ).hexdigest()
            valid = valid and hmac.compare_digest(params["t"], expected)
        else:
            valid = False
    if not valid:
        raise BridgeError(40, "Invalid bridge credentials")


def number(params, name, default, maximum=200):
    value = params.get(name, str(default))
    if not re.fullmatch(r"[0-9]{1,5}", value) or int(value) > maximum:
        raise BridgeError(10, "Invalid numeric parameter")
    return int(value)


def placeholder():
    # Original neutral 64px PNG; never masquerades as album/artist artwork.
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    rows = b"".join(b"\0" + bytes((42, 65, 76)) * 64 for _ in range(64))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 64, 64, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def static_response(method, params):
    if method in {"ping", "getOpenSubsonicExtensions"}:
        return {"openSubsonicExtensions": EXTENSIONS} if method != "ping" else {}
    if method == "getLicense":
        return {"license": {"valid": True}}
    if method == "getMusicFolders":
        return {
            "musicFolders": {"musicFolder": [{"id": 1, "name": "Apple catalog (discovered items)"}]}
        }
    if method == "getUser":
        if params.get("username", "bridge") != "bridge":
            raise BridgeError(70, "Unknown user")
        return {
            "user": {
                "username": "bridge",
                "streamRole": True,
                "downloadRole": False,
                "adminRole": False,
                "settingsRole": False,
                "scrobblingEnabled": False,
            }
        }
    if method == "tokenInfo":
        return {"tokenInfo": {"username": "bridge"}}
    if method == "getPlaylists":
        return {"playlists": {"playlist": []}}
    if method == "getGenres":
        return {"genres": {"genre": []}}
    if method == "getInternetRadioStations":
        return {"internetRadioStations": {"internetRadioStation": []}}
    return None


class ProtocolCore(Core):
    def dispatch(self, method, params):
        ready = static_response(method, params)
        if ready is not None:
            return ready
        if method == "search3":
            ranges = {
                kind: (number(params, kind + "Count", 20), number(params, kind + "Offset", 0))
                for kind in ("artist", "album", "song")
            }
            values = {
                kind: self.search(params.get("query", "").strip(), kind, count, offset)
                for kind, (count, offset) in ranges.items()
            }
            return {
                "searchResult3": {
                    kind: [self.describe(value) for value in items]
                    for kind, items in values.items()
                }
            }
        if method == "getArtists":
            artists = sorted(self.known("artist"), key=lambda item: item.title.casefold())
            return {
                "artists": {
                    "ignoredArticles": "",
                    "index": [
                        {"name": "Discovered", "artist": [self.describe(a) for a in artists]}
                    ],
                }
            }
        if method == "getArtistInfo2":
            # The API accepts artist, album or song IDs. Validate existence, but
            # do not invent biography, portraits or similar-artist suggestions.
            self.resolve(params.get("id", ""))
            return {"artistInfo2": {"similarArtist": []}}
        if method == "getArtist":
            artist = self.resolve(params.get("id", ""), "artist")
            albums = self.albums(artist)
            return {
                "artist": {
                    **self.describe(artist),
                    "albumCount": len(albums),
                    "album": [self.describe(a) for a in albums],
                }
            }
        if method == "getAlbum":
            album = self.resolve(params.get("id", ""), "album")
            songs = self.songs(album)
            return {
                "album": {
                    **self.describe(album),
                    "songCount": len(songs),
                    "duration": sum(t.duration_ms for t in songs) // 1000,
                    "song": [self.describe(t) for t in songs],
                }
            }
        if method == "getSong":
            return {"song": self.describe(self.resolve(params.get("id", ""), "song"))}
        if method == "getAlbumList2":
            ordering = params.get("type", "alphabeticalByName")
            if ordering not in {"alphabeticalByName", "newest"}:
                raise BridgeError(0, "Only alphabeticalByName and newest are supported")
            albums = sorted(
                self.known("album"),
                key=lambda a: (
                    a.album_title.casefold() if ordering == "alphabeticalByName" else a.release_date
                ),
                reverse=ordering == "newest",
            )
            offset, count = number(params, "offset", 0, 5000), number(params, "size", 20)
            return {
                "albumList2": {"album": [self.describe(a) for a in albums[offset : offset + count]]}
            }
        if method == "getCoverArt":
            return self.cached_artwork(params.get("id", ""))
        raise BridgeError(0, "Endpoint not implemented by this read-only proof of concept")


def byte_range(value, size):
    if not value:
        return 0, size - 1
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)
    if not match or not any(match.groups()) or not size:
        raise ValueError("Unsupported byte range")
    first, last = match.groups()
    if not first:
        if int(last) == 0:
            raise ValueError("Empty suffix")
        return max(0, size - int(last)), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable byte range")
    return start, end


def encoded_response(payload, fmt):
    if fmt == "json":
        return json.dumps({"subsonic-response": payload}).encode(), "application/json"
    root = ET.Element("subsonic-response", xmlns="http://subsonic.org/restapi")

    def fill(node, values):
        for key, value in values.items():
            if isinstance(value, dict):
                fill(ET.SubElement(node, key), value)
            elif isinstance(value, list):
                for item in value:
                    child = ET.SubElement(node, key)
                    if isinstance(item, dict):
                        fill(child, item)
                    else:
                        child.text = str(item)
            else:
                node.set(key, str(value).lower() if isinstance(value, bool) else str(value))

    fill(root, payload)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), "application/xml"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # Request URLs may contain bridge credentials. Never access-log them.

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def reply(self, status, body=b"", mime="application/octet-stream", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def stream(self, params):
        if (
            params.get("format", "raw") != "raw"
            or params.get("maxBitRate", "0") != "0"
            or params.get("timeOffset", "0") != "0"
        ):
            raise BridgeError(
                0, "Transcoding and time offsets are unsupported; request raw and use byte ranges"
            )
        key, file, size, media = self.server.actor.acquire(
            params.get("id", ""), self.command == "HEAD"
        )
        try:
            info = os.fstat(file.fileno())
            etag = (
                '"' + hashlib.sha256(f"{key}:{size}:{info.st_mtime_ns}".encode()).hexdigest() + '"'
            )
            requested = self.headers.get("Range", "")
            if self.headers.get("If-Range") not in (None, etag):
                requested = ""
            try:
                start, end = byte_range(requested, size)
            except ValueError:
                self.reply(416, extra={"Content-Range": f"bytes */{size}"})
                return
            prefix = file.read(12)
            mime = (
                "audio/mp4"
                if prefix[4:8] == b"ftyp"
                else "audio/flac"
                if prefix[:4] == b"fLaC"
                else "audio/eac3"
                if prefix[:2] == b"\x0b\x77" and media.codec == "eac3"
                else "application/octet-stream"
            )
            self.send_response(206 if requested else 200)
            headers = {
                "Content-Type": mime,
                "Content-Length": str(end - start + 1),
                "Accept-Ranges": "bytes",
                "ETag": etag,
                "Cache-Control": "no-store",
                "Connection": "close",
                "X-Content-Type-Options": "nosniff",
                "X-Media-Codec": media.codec,
                "X-Media-Atmos": "JOC-verified" if media.atmos else "unverified",
                "X-Requested-Quality": self.server.quality,
            }
            if requested:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            file.seek(start)
            remaining = end - start + 1
            while remaining and self.command != "HEAD" and not self.server.actor.cancel.is_set():
                chunk = file.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        finally:
            file.close()
            self.server.actor.call("release", key)

    def handle_api(self):
        fmt = "xml"
        try:
            if (
                len(self.path) > 8192
                or self.headers.get("Host") not in self.server.hosts
                or self.headers.get("Origin")
            ):
                self.reply(403)
                return
            parsed = urlsplit(self.path)
            match = re.fullmatch(r"/rest/([A-Za-z0-9]+)(?:\.view)?", parsed.path)
            if parsed.scheme or parsed.netloc or not match:
                self.reply(404)
                return
            data = parsed.query
            if self.command == "POST":
                if (
                    self.headers.get("Transfer-Encoding")
                    or self.headers.get("Content-Type", "").split(";")[0]
                    != "application/x-www-form-urlencoded"
                ):
                    self.reply(415)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 8192:
                    self.reply(413)
                    return
                data += "&" + self.rfile.read(length).decode("utf8")
            pairs = parse_qs(data, keep_blank_values=True, max_num_fields=32)
            if any(len(values) != 1 or len(values[0]) > 512 for values in pairs.values()):
                raise BridgeError(10, "Duplicate or oversized parameters")
            params = {key: values[0] for key, values in pairs.items()}
            fmt = params.get("f", "xml")
            if fmt not in {"xml", "json"}:
                raise BridgeError(10, "Only XML and JSON are supported")
            method = match[1]
            # Static extension discovery is the sole public endpoint; no catalog data.
            if method != "getOpenSubsonicExtensions":
                authenticate(params, self.server.credentials)
            if not params.get("c") or not re.fullmatch(r"1\.[0-9]+\.[0-9]+", params.get("v", "")):
                raise BridgeError(10, "Client name and API version are required")
            if tuple(map(int, params["v"].split("."))) > (1, 16, 1):
                raise BridgeError(30, "Requested API version is newer than this proof of concept")
            if method == "stream":
                self.stream(params)
                return
            if method == "getBridgeDiagnostics":
                self.api_response(
                    {"bridgeDiagnostics": {"metric": self.server.metrics.snapshot()}}, fmt
                )
                return
            payload = static_response(method, params)
            if method == "getCoverArt":
                payload = self.server.actor.core.cached_artwork(params.get("id", ""))
            elif method == "search3":
                # Actor admission counts unique flights, not duplicate HTTP waiters.
                payload = self.server.actor.metadata(method, params)
            elif payload is None:
                if not self.server.metadata_slots.acquire(blocking=False):
                    raise BridgeError(0, "Catalog busy; retry later")
                try:
                    payload = self.server.actor.metadata(method, params)
                finally:
                    self.server.metadata_slots.release()
            if method == "getCoverArt":
                image = self.server.covers.get(payload, number(params, "size", 400, 4096))
                self.reply(
                    200,
                    image or placeholder(),
                    "image/png",
                    {
                        "X-Artwork-Status": "available" if image else "placeholder",
                        "Cache-Control": "private, max-age=86400" if image else "no-store",
                    },
                )
                return
            self.api_response(payload, fmt)
        except CoverUnavailable:
            self.reply(
                503,
                extra={
                    "Retry-After": "30",
                    "Cache-Control": "no-store",
                    "X-Artwork-Status": "unavailable",
                },
            )
        except BridgeError as exc:
            self.api_response({"error": {"code": exc.code, "message": exc.message}}, fmt)
        except (PlayerError, OSError, ValueError, TypeError):
            self.api_response(
                {
                    "error": {
                        "code": 0,
                        "message": "Bridge operation unavailable; check local setup or retry later",
                    }
                },
                fmt,
            )
        except Exception:
            self.api_response({"error": {"code": 0, "message": "Bridge operation failed"}}, fmt)

    def api_response(self, data, fmt):
        payload = {
            "status": "failed" if "error" in data else "ok",
            "version": "1.16.1",
            "type": "AppleMusicBridgePoC",
            "serverVersion": "0.1",
            "openSubsonic": True,
            **data,
        }
        body, mime = encoded_response(payload, fmt)
        self.reply(200, body, mime)

    def do_GET(self):
        try:
            self.handle_api()
        except (OSError, ConnectionError):
            pass

    do_HEAD = do_GET
    do_POST = do_GET


class Server(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True

    def __init__(self, actor, credentials, port=4533, quality="lossless"):
        self.actor, self.credentials, self.quality = actor, credentials, quality
        self.slots = threading.BoundedSemaphore(16)
        self.metadata_slots = threading.BoundedSemaphore(4)
        self.metrics = actor.metrics
        self.covers = Covers(actor.core.cache.root / "artwork", self.metrics)
        try:
            super().__init__(("127.0.0.1", port), Handler)
        except Exception:
            self.covers.close()
            raise
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}

    def server_close(self):
        self.covers.close()
        super().server_close()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Retry-After: 1\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            finally:
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never expose exception payloads or credentials.
