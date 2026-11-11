"""Apple line lyrics over verified HTTPS; no wrapper HTTP request or persisted lyrics."""

import copy
import http.client
import json
import math
import os
import re
import ssl
import stat
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict

MAX_BYTES = 2_000_000
HOST = "amp-api.music.apple.com"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def read_token(directory, name):
    path = directory / name
    if path.resolve() != path.absolute():
        raise ValueError("Unsafe session file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or not 0 < info.st_size <= 65536
        ):
            raise ValueError("Unsafe session file")
        token = stream.read(65537).decode("ascii").strip()
    if not token or len(token) > 65536 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("Invalid session file")
    return token


def timestamp(value, *, apple_timing=False):
    """TTML media-clock and offset times, converted to OpenSubsonic milliseconds."""
    if not isinstance(value, str):
        return None
    if apple_timing and re.fullmatch(r"\d+(?:\.\d+)?", value):
        value += "s"
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    if match:
        seconds = float(match[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]]
    else:
        match = re.fullmatch(r"(?:(\d+):)?([0-5]?\d):([0-5]\d(?:\.\d+)?)", value)
        if not match:
            return None
        seconds = int(match[1] or 0) * 3600 + int(match[2]) * 60 + float(match[3])
    return round(seconds * 1000) if math.isfinite(seconds) and seconds <= 86400 else None


def text_content(element):
    # Preserve word/syllable spacing; br is an explicit line break.
    parts = [element.text or ""]
    for child in element:
        parts.append("\n" if child.tag.split("}")[-1] == "br" else text_content(child))
        parts.append(child.tail or "")
    return "".join(parts)


def parse_ttml(raw, *, apple_timing=False):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_BYTES:
        raise ValueError("Invalid lyrics")
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise ValueError("Invalid lyrics")
    root = ET.fromstring(raw)
    if root.tag not in {"tt", "{http://www.w3.org/ns/ttml}tt"}:
        raise ValueError("Invalid lyrics")
    lines = []
    # Only body content is displayed: translation/annotation metadata in head must
    # not disable synchronization or appear as additional lyric lines.
    namespace = root.tag.removesuffix("tt")

    def is_ttml(node):
        return node.tag.startswith(namespace) if namespace else "}" not in node.tag

    bodies = [node for node in root if node.tag == namespace + "body"]
    if len(bodies) != 1:
        return []
    body = bodies[0]

    def when(value):
        return timestamp(value, apple_timing=apple_timing)

    def collect(node, base=0, valid=True, inherited=False):
        if not is_ttml(node):
            return
        # TTML begin times are relative to the containing time container.
        # Sequential/frame/tick timing is not interpreted as media-clock timing.
        valid = valid and node.get("timeContainer", "par") == "par"
        begin = node.get("begin")
        offset = when(begin) if begin is not None else 0
        valid = valid and offset is not None
        # Apple lyric TTML repeats absolute song times on div/p/span elements.
        # Generic TTML containers instead use relative offsets.
        base = (offset or 0) if apple_timing else base + (offset or 0)
        timed = begin is not None if apple_timing else inherited or begin is not None
        if node.tag.split("}")[-1] == "p":
            value = " ".join(text_content(node).split())
            start = base if valid and timed else None
            if start is None and valid and begin is None:
                starts = []

                def span_time(child, origin):
                    if not is_ttml(child):
                        return
                    offset = when(child.get("begin"))
                    if child.get("begin") is not None and offset is None:
                        return
                    origin = (offset or 0) if apple_timing else origin + (offset or 0)
                    if offset is not None:
                        starts.append(origin)
                    for nested in child:
                        span_time(nested, origin)

                for child in node:
                    span_time(child, base)
                start = min(starts) if starts else None
            if value:
                lines.append((start, value))
                if len(lines) > 5000:
                    raise ValueError("Too many lyric lines")
            return
        for child in node:
            collect(child, base, valid, timed)

    collect(body)
    if not lines:
        return []
    timing_supported = (
        all(
            node.get("timeContainer", "par") == "par"
            and (node.get("begin") is None or when(node.get("begin")) is not None)
            for node in body.iter()
            if is_ttml(node)
        )
        and root.get("{http://www.w3.org/ns/ttml#parameter}timeBase", "media") == "media"
    )
    synced = timing_supported and all(start is not None for start, _ in lines)
    if synced:
        lines.sort(key=lambda row: row[0])
    language = root.get(XML_LANG, "und")
    if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language):
        language = "und"
    return [
        {
            "lang": language,
            "synced": synced,
            "line": [
                dict(value=value, **({"start": start} if synced else {})) for start, value in lines
            ],
        }
    ]


def parse_response(data):
    if not isinstance(data, dict) or data.get("errors"):
        raise ValueError("Invalid lyrics response")
    rows = data.get("data")
    if not isinstance(rows, list):
        raise ValueError("Invalid lyrics response")
    if not rows:
        return []
    attrs = rows[0].get("attributes", {}) if isinstance(rows[0], dict) else {}
    if not isinstance(attrs, dict):
        raise ValueError("Invalid lyrics response")
    for field in ("ttmlLocalizations", "ttml"):
        if isinstance(attrs.get(field), str) and attrs[field].strip():
            return parse_ttml(attrs[field], apple_timing=True)
    plain = attrs.get("text")
    if isinstance(plain, str):
        if re.match(r"\s*(?:<\?xml\b|<tt(?:\s|>))", plain):
            return parse_ttml(plain, apple_timing=True)
        if any(
            not (
                c in "\t\n\r"
                or 0x20 <= ord(c) <= 0xD7FF
                or 0xE000 <= ord(c) <= 0xFFFD
                or 0x10000 <= ord(c) <= 0x10FFFF
            )
            for c in plain
        ):
            raise ValueError("Invalid lyric text")
        lines = [line.strip() for line in plain.splitlines() if line.strip()]
        if len(lines) > 5000:
            raise ValueError("Too many lyric lines")
        return (
            [{"lang": "und", "synced": False, "line": [{"value": s} for s in lines]}]
            if lines
            else []
        )
    return []


def fetch_lyrics(directory, country, identifier):
    # Fixed origin, no proxies or redirects. Credentials never cross to another host.
    if not re.fullmatch(r"[a-z]{2}", country) or not re.fullmatch(r"[0-9]{1,30}", identifier):
        raise ValueError("Invalid song")
    dev = read_token(directory, "DEV_TOKEN")
    music = read_token(directory, "MUSIC_TOKEN")
    connection = http.client.HTTPSConnection(HOST, timeout=10, context=ssl.create_default_context())
    try:
        connection.request(
            "GET",
            f"/v1/catalog/{country}/songs/{identifier}/syllable-lyrics?extend=ttmlLocalizations",
            headers={
                "Authorization": "Bearer " + dev,
                "media-user-token": music,
                "Origin": "https://music.apple.com",
                "User-Agent": "Music/5.7 Android/10 model/Pixel6GR1YH build/1234 (dt:66)",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        if response.status == 404:
            return []
        if response.status != 200:
            raise ValueError("Lyrics unavailable")
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Lyrics too large")
        return parse_response(json.loads(raw))
    finally:
        connection.close()


class Lyrics:
    """Small in-memory cache with bounded single-flight requests, separate from audio."""

    def __init__(self, directory=None, fetch=None, clock=time.monotonic):
        self.directory = directory
        self.fetch = fetch or (
            lambda country, identifier: fetch_lyrics(directory, country, identifier)
        )
        self.enabled = directory is not None or fetch is not None
        self.clock = clock
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.flights = {}
        self.not_before = 0.0

    def get(self, country, identifier):
        if not self.enabled:
            return []
        key = (country, identifier)
        with self.lock:
            hit = self.cache.get(key)
            if hit and hit[0] > self.clock():
                self.cache.move_to_end(key)
                return copy.deepcopy(hit[1])
            event = self.flights.get(key)
            owner = event is None
            if owner:
                if len(self.flights) >= 2 or self.clock() < self.not_before:
                    return []
                event = self.flights[key] = threading.Event()
        if not owner:
            event.wait(12)
            with self.lock:
                hit = self.cache.get(key)
                return copy.deepcopy(hit[1]) if hit and hit[0] > self.clock() else []
        value = []
        try:
            value = self.fetch(country, identifier)
        except (OSError, ValueError, http.client.HTTPException, ET.ParseError, RecursionError):
            # Do not leak upstream payloads, tokens or exceptions into HTTP responses/logs.
            with self.lock:
                self.not_before = self.clock() + 30
        finally:
            with self.lock:
                self.cache[key] = (self.clock() + (3600 if value else 300), value)
                self.cache.move_to_end(key)
                while len(self.cache) > 64:
                    self.cache.popitem(last=False)
                self.flights.pop(key).set()
        return copy.deepcopy(value)
