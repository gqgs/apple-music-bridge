"""Opt-in Commons artist images with exact identity links and stored attribution."""

import base64
import json
import os
import re
import stat
import tempfile
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, build_opener

from ..artwork import MAX_BYTES, ArtworkRedirect, decode
from ..cache import safe_path
from ..runtime_install import private_dir
from .artwork import CoverUnavailable, png

UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
UA = "AppleMusicPlayerBridge/0.1 (+https://github.com/gqgs/apple-music-player)"
RECORD_LIMIT = 3 * 1024**2


class Missing(Exception):
    def __init__(self, reason="image-unavailable"):
        self.reason = reason


class ResolutionFailure(CoverUnavailable):
    def __init__(self, reason):
        self.reason = safe_reason(reason)
        super().__init__(self.reason)


def safe_reason(reason):
    # Only application-owned categories, never provider bodies, URLs or exception text.
    if isinstance(reason, str) and (
        reason
        in {
            "image-unavailable",
            "identity-missing",
            "identity-ambiguous",
            "attribution-unavailable",
            "wikidata-missing",
            "wikidata-ambiguous",
            "invalid-response",
            "lookup-failed",
            "lookup-expired",
            "lookup-pending",
            "queue-full",
            "shutdown",
            "cached-retry",
        }
        or re.fullmatch(
            r"(?:musicbrainz|wikidata|commons|image)-(?:network|invalid|cooldown|http-[1-5][0-9]{2})",
            reason,
        )
    ):
        return reason
    return "lookup-failed"


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(UUID, value) is not None


def resources(artist):
    if not re.fullmatch(r"[0-9]{1,30}", artist.id):
        raise ValueError("Invalid artist")
    result = [
        f"https://music.apple.com/artist/{artist.id}",
        f"https://itunes.apple.com/artist/id{artist.id}",
    ]
    # Use an actual returned Apple artist URL when available, but never arbitrary
    # external resources or a name inferred from another catalog's search result.
    countries = {"us"}
    if re.fullmatch(r"[a-z]{2}", artist.country):
        countries.add(artist.country)
        result.append(f"https://music.apple.com/{artist.country}/artist/{artist.id}")
    # US links are common in MusicBrainz; this is an identity reference only,
    # never an Apple catalog/media request or storefront remapping.
    result.append(f"https://music.apple.com/us/artist/{artist.id}")
    url = artist.store_url
    try:
        p = urlsplit(url)
        if (
            len(url) <= 2048
            and p.scheme == "https"
            and not p.username
            and not p.password
            and p.port in (None, 443)
            and p.hostname in {"music.apple.com", "itunes.apple.com"}
            and re.fullmatch(
                r"/(?:[a-z]{2}/)?artist/(?:[^/]+/)?(?:id)?" + re.escape(artist.id), p.path
            )
        ):
            result.append(p._replace(query="", fragment="").geturl())
            country = p.path.split("/")[1]
            country = country if re.fullmatch(r"[a-z]{2}", country) else "us"
            countries.add(country)
            prefix = "id" if p.hostname == "itunes.apple.com" else ""
            result.append(f"https://{p.hostname}/{country}/artist/{prefix}{artist.id}")
    except ValueError:
        pass
    # MusicBrainz canonicalizes iTunes URLs with both country and id prefix.
    # Its URL lookup compares stored URLs exactly; it does not follow Apple redirects.
    for country in sorted(countries):
        result.append(f"https://itunes.apple.com/{country}/artist/id{artist.id}")
        result.append(f"https://music.apple.com/{country}/artist/{artist.id}")
    return list(dict.fromkeys(result))


def secure_url(url, host, path_prefix):
    try:
        p = urlsplit(url)
        return (
            isinstance(url, str)
            and len(url) <= 2048
            and p.scheme == "https"
            and p.hostname == host
            and not p.username
            and not p.password
            and p.port in (None, 443)
            and not p.query
            and not p.fragment
            and p.path.startswith(path_prefix)
        )
    except (ValueError, TypeError):
        return False


def image_url(url):
    return secure_url(url, "upload.wikimedia.org", "/wikipedia/commons/")


def file_title(url):
    if secure_url(url, "commons.wikimedia.org", "/wiki/File:"):
        title = unquote(urlsplit(url).path.removeprefix("/wiki/"))
        if len(title) <= 300 and all(c.isprintable() for c in title) and "|" not in title:
            return title
    return ""


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value):
    if not isinstance(value, str) or len(value) > 10000:
        return ""
    parser = PlainText()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())[:1000]


def provenance(info, mbid, origin):
    metadata = info.get("extmetadata", {})

    def value(key):
        return plain(metadata.get(key, {}).get("value", ""))

    creator, license_name = value("Artist"), value("LicenseShortName")
    license_url = value("LicenseUrl")
    if license_url.startswith("http://creativecommons.org/"):
        license_url = "https://" + license_url.removeprefix("http://")
    match = re.fullmatch(r"CC BY(-SA)? (1\.0|2\.0|2\.5|3\.0|4\.0)", license_name)
    expected = ""
    if match:
        expected = f"https://creativecommons.org/licenses/by{'-sa' if match[1] else ''}/{match[2]}/"
    elif license_name == "CC0":
        expected = "https://creativecommons.org/publicdomain/zero/1.0/"
    elif license_name == "Public domain":
        expected = "https://creativecommons.org/publicdomain/mark/1.0/"
    source = info.get("descriptionurl", "")
    if (
        not creator
        or not expected
        or license_url.rstrip("/") != expected.rstrip("/")
        or not file_title(source)
    ):
        raise Missing("attribution-unavailable")
    return dict(
        creator=creator,
        license=license_name,
        licenseUrl=expected,
        sourceUrl=source,
        credit=value("Credit"),
        artistMbid=mbid,
        imageKind="artist-image",
        origin=origin,
        changes="Resized and converted to PNG",
    )


def checked_credits(value):
    fields = {
        "creator",
        "license",
        "licenseUrl",
        "sourceUrl",
        "credit",
        "artistMbid",
        "imageKind",
        "origin",
        "changes",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or not all(isinstance(v, str) and len(v) <= 2048 for v in value.values())
        or not valid_id(value["artistMbid"])
        or value["imageKind"] != "artist-image"
        or value["origin"] not in {"MusicBrainz image relationship", "Wikidata P18"}
        or value["changes"] != "Resized and converted to PNG"
    ):
        raise ValueError("Invalid image credits")
    info = {
        "descriptionurl": value["sourceUrl"],
        "extmetadata": {
            key: {"value": value[field]}
            for key, field in (
                ("Artist", "creator"),
                ("LicenseShortName", "license"),
                ("LicenseUrl", "licenseUrl"),
                ("Credit", "credit"),
            )
        },
    }
    # Metadata is already plain text; avoid interpreting literal creator <...>.
    from html import escape

    for field in info["extmetadata"].values():
        field["value"] = escape(field["value"])
    try:
        clean = provenance(info, value["artistMbid"], value["origin"])
    except Missing:
        raise ValueError("Invalid image credits") from None
    return clean


class Gate:
    """Shared across fallback instances in this process, including retry cooldown."""

    def __init__(self):
        self.lock = threading.Lock()
        self.next = 0.0
        self.blocked = 0.0

    def enter(self, stop):
        with self.lock:
            now = time.monotonic()
            if now < self.blocked:
                raise CoverUnavailable()
            if stop.wait(max(0, self.next - now)):
                raise CoverUnavailable()
            self.next = time.monotonic() + 1.1

    def throttle(self, value):
        delay = 60.0
        try:
            delay = (
                float(value)
                if value.isdigit()
                else parsedate_to_datetime(value).timestamp() - time.time()
            )
        except (ValueError, TypeError, OverflowError):
            pass
        with self.lock:
            # Fail closed on an excessive Retry-After rather than retrying early.
            self.blocked = max(self.blocked, time.monotonic() + max(60, delay))


MB_GATE, WIKI_GATE = Gate(), Gate()


def fetch(url, stop, *, image=False):
    validator = image_url if image else lambda u: u == url
    if image and not image_url(url):
        raise ValueError("Unsafe cover URL")
    host = urlsplit(url).hostname
    if not image and not (
        url.startswith("https://musicbrainz.org/ws/2/")
        or url.startswith("https://www.wikidata.org/w/api.php?")
        or url.startswith("https://commons.wikimedia.org/w/api.php?")
    ):
        raise ValueError("Unsafe metadata URL")
    gate = MB_GATE if host == "musicbrainz.org" else WIKI_GATE
    provider = {
        "musicbrainz.org": "musicbrainz",
        "www.wikidata.org": "wikidata",
        "commons.wikimedia.org": "commons",
    }.get(host, "image")
    try:
        gate.enter(stop)
    except CoverUnavailable:
        raise ResolutionFailure(provider + "-cooldown") from None
    cap = MAX_BYTES if image else 256 * 1024
    request = Request(
        url, headers={"User-Agent": UA, "Accept": "image/*" if image else "application/json"}
    )
    try:
        timeout = min(5, max(0.05, getattr(stop, "end", time.monotonic() + 5) - time.monotonic()))
        with build_opener(ArtworkRedirect(validator)).open(request, timeout=timeout) as response:
            if not validator(response.url):
                raise ValueError("Unsafe redirect")
            allowed = {"image/jpeg", "image/png", "image/webp"} if image else {"application/json"}
            if response.headers.get_content_type() not in allowed:
                raise ValueError("Unexpected content")
            data = bytearray()
            deadline = min(time.monotonic() + 8, getattr(stop, "end", float("inf")))
            read = getattr(response, "read1", response.read)
            while len(data) <= cap:
                if stop.is_set() or time.monotonic() >= deadline:
                    raise CoverUnavailable()
                chunk = read(min(65536, cap + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > cap:
                raise ValueError("Response too large")
            return bytes(data)
    except HTTPError as exc:
        try:
            if exc.code == 404:
                raise Missing(provider + "-http-404") from None
            if exc.code in (429, 503):
                gate.throttle((exc.headers.get("Retry-After") or "")[:128])
            raise ResolutionFailure(provider + "-http-" + str(exc.code)) from None
        finally:
            exc.close()
    except (URLError, OSError):
        raise ResolutionFailure(provider + "-network") from None
    except ValueError:
        raise ResolutionFailure(provider + "-invalid") from None


def metadata(url, stop):
    data = json.loads(fetch(url, stop))
    if not isinstance(data, dict):
        raise ValueError("Invalid metadata")
    if "error" in data:
        if data["error"].get("code") in {"maxlag", "ratelimited"}:
            WIKI_GATE.throttle("60")
        raise CoverUnavailable()
    return data


def commons(title, mbid, origin, stop):
    if (
        not isinstance(title, str)
        or not title.startswith("File:")
        or len(title) > 300
        or "|" in title
    ):
        raise Missing()
    query = urlencode(
        dict(
            action="query",
            prop="imageinfo",
            titles=title,
            redirects=1,
            iiprop="url|mime|size|extmetadata",
            iiurlwidth=500,
            iiextmetadatafilter="Artist|Credit|LicenseShortName|LicenseUrl",
            format="json",
            maxlag=5,
        )
    )
    data = metadata("https://commons.wikimedia.org/w/api.php?" + query, stop)
    pages = data.get("query", {}).get("pages", {})
    if not isinstance(pages, dict) or len(pages) != 1:
        raise Missing()
    images = next(iter(pages.values())).get("imageinfo", [])
    if not images:
        raise Missing()
    info = images[0]
    credits = provenance(info, mbid, origin)
    url = info.get("thumburl", "")
    if not image_url(url):
        raise Missing()
    raw = fetch(url, stop, image=True)
    return png(decode(raw, 500)), credits


def resolve(artist, stop):
    urls = resources(artist)
    query = urlencode([*(("resource", u) for u in urls), ("inc", "artist-rels"), ("fmt", "json")])
    data = metadata("https://musicbrainz.org/ws/2/url?" + query, stop)
    matches = set()
    for row in data.get("urls", []):
        if not isinstance(row, dict) or row.get("resource") not in urls:
            continue
        for rel in row.get("relations", []):
            if rel.get("target-type") == "artist":
                identifier = rel.get("artist", {}).get("id")
                if valid_id(identifier):
                    matches.add(identifier)
    if len(matches) != 1:
        raise Missing("identity-missing" if not matches else "identity-ambiguous")
    mbid = matches.pop()
    data = metadata(f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=url-rels&fmt=json", stop)
    if data.get("id") != mbid:
        raise Missing()
    titles, wikidata = set(), set()
    for rel in data.get("relations", []):
        url = rel.get("url", {}).get("resource", "")
        if rel.get("type") == "image" and file_title(url):
            titles.add(file_title(url))
        if rel.get("type") == "wikidata" and secure_url(url, "www.wikidata.org", "/wiki/Q"):
            qid = urlsplit(url).path.removeprefix("/wiki/")
            if re.fullmatch(r"Q[1-9][0-9]{0,15}", qid):
                wikidata.add(qid)
    # Multiple linked photos are not competing identities; choose deterministically.
    direct_failure = None
    if titles:
        try:
            return commons(sorted(titles)[0], mbid, "MusicBrainz image relationship", stop)
        except Missing as exc:
            direct_failure = exc
    if not wikidata and direct_failure is not None:
        raise direct_failure
    if len(wikidata) != 1:
        raise Missing("wikidata-missing" if not wikidata else "wikidata-ambiguous")
    qid = wikidata.pop()
    query = urlencode(
        dict(action="wbgetentities", ids=qid, props="claims", format="json", maxlag=5)
    )
    data = metadata("https://www.wikidata.org/w/api.php?" + query, stop)
    entity = data.get("entities", {}).get(qid, {})
    if entity.get("id") != qid:
        raise Missing()
    claims = [c for c in entity.get("claims", {}).get("P18", []) if c.get("rank") != "deprecated"]
    preferred = [c for c in claims if c.get("rank") == "preferred"]
    titles = set()
    for claim in preferred or claims:
        snak = claim.get("mainsnak", {})
        value = snak.get("datavalue", {}).get("value")
        if snak.get("snaktype") == "value" and isinstance(value, str):
            titles.add("File:" + value)
    if len(titles) != 1:
        raise Missing("wikidata-missing" if not titles else "wikidata-ambiguous")
    return commons(titles.pop(), mbid, "Wikidata P18", stop)


class Deadline:
    def __init__(self, stop, end):
        self.stop, self.end = stop, end

    def is_set(self):
        return self.stop.is_set() or time.monotonic() >= self.end

    def wait(self, seconds):
        self.stop.wait(min(seconds, max(0, self.end - time.monotonic())))
        return self.is_set()


class ArtistImages:
    def __init__(self, root):
        self.root = private_dir(root)
        for path in self.root.glob(".part-*"):
            safe_path(self.root, path)
            if path.is_file() and path.stat().st_nlink == 1:
                path.unlink()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="artist-image")
        self.pending = {}

    def read(self, key):
        path = safe_path(self.root, self.root / (key + ".json"))
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > RECORD_LIMIT
                ):
                    return None
                value = json.load(stream)
            if not 0 < value["until"] - time.time() <= 7 * 86400:
                return None
            if value["status"] == "missing":
                return b"", {"reason": safe_reason(value.get("reason", "image-unavailable"))}
            if value["status"] == "retry":
                raise ResolutionFailure(value.get("reason", "cached-retry"))
            if value["status"] != "ok" or not valid_id(value["credits"]["artistMbid"]):
                return None
            data = base64.b64decode(value["image"], validate=True)
            image = decode(data, 500)
            return png(image), checked_credits(value["credits"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def write(self, key, value):
        data = json.dumps(value).encode()
        if len(data) > RECORD_LIMIT:
            return
        with self.lock:
            files = []
            for path in self.root.iterdir():
                if re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
                    safe_path(self.root, path)
                    info = path.stat()
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        files.append((info.st_mtime, info.st_size, path))
            files.sort()
            total = sum(size for _, size, _ in files)
            while files and (len(files) >= 128 or total + len(data) > 32 * 1024**2):
                _, size, path = files.pop(0)
                path.unlink()
                total -= size
            target = safe_path(self.root, self.root / (key + ".json"))
            fd, temporary = tempfile.mkstemp(prefix=".part-", dir=self.root)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def run(self, key, artist, deadline):
        try:
            stop = Deadline(self.stop, deadline)
            if stop.is_set():
                raise ResolutionFailure("lookup-expired")
            data, credits = resolve(artist, stop)
            if stop.is_set():
                raise ResolutionFailure("lookup-expired")
            if not data:
                raise ValueError("Invalid image")
            credits = checked_credits(credits)
            self.write(
                key,
                dict(
                    status="ok",
                    until=time.time() + 7 * 86400,
                    image=base64.b64encode(data).decode(),
                    credits=credits,
                ),
            )
            return data, credits
        except Missing as exc:
            self.write(
                key,
                dict(status="missing", until=time.time() + 86400, reason=safe_reason(exc.reason)),
            )
            return b"", {"reason": safe_reason(exc.reason)}
        except Exception as exc:
            reason = safe_reason(
                getattr(
                    exc,
                    "reason",
                    "invalid-response" if isinstance(exc, ValueError) else "lookup-failed",
                )
            )
            self.write(key, dict(status="retry", until=time.time() + 300, reason=reason))
            raise ResolutionFailure(reason) from None

    def key(self, artist):
        return sha256(
            json.dumps([artist.country, artist.id, resources(artist)]).encode()
        ).hexdigest()

    def info(self, artist):
        try:
            cached = self.read(self.key(artist))
        except CoverUnavailable as exc:
            return {
                "status": "retry",
                "reason": safe_reason(getattr(exc, "reason", "cached-retry")),
            }
        if cached is None:
            return {"status": "pending"}
        data, credits = cached
        return (
            {"status": "ready", "credits": credits}
            if data
            else {"status": "unavailable", "reason": credits.get("reason", "image-unavailable")}
        )

    def get(self, artist):
        key = self.key(artist)
        cached = self.read(key)
        if cached is not None:
            return cached
        with self.lock:
            if self.stop.is_set():
                raise ResolutionFailure("shutdown")
            future = self.pending.get(key)
            created = False
            if future is None:
                if len(self.pending) >= 8:
                    raise ResolutionFailure("queue-full")
                future = self.pool.submit(self.run, key, artist, time.monotonic() + 25)
                self.pending[key] = future
                created = True
        if created:

            def finished(_):
                with self.lock:
                    self.pending.pop(key, None)

            future.add_done_callback(finished)
        try:
            return future.result(timeout=8)
        except TimeoutError:
            raise ResolutionFailure("lookup-pending") from None
        except CancelledError:
            raise ResolutionFailure("shutdown") from None

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
