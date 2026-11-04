import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from test_bridge import bridge, payload, request  # noqa: F401

from apple_music_player import apple_album
from apple_music_player.apple_album import MAX_PAGE_BYTES, _AlbumRedirect, album_page_url
from apple_music_player.bridge.album_quality import (
    AlbumQuality,
    fetch_album_badges,
    parse_album_badges,
)


def page(identifier="2", country="br", **badges):
    return {
        "id": "album-detail-header - " + identifier,
        "audioBadges": {
            "dolbyAtmos": False,
            "dolbyAudio": False,
            "hiResLossless": False,
            "lossless": False,
            **badges,
        },
        "contentDescriptor": {
            "kind": "album",
            "identifiers": {"storeAdamID": identifier},
            "url": f"https://music.apple.com/{country}/album/example/{identifier}",
        },
    }


def document(node):
    return (
        '<html><script type="application/json" id="serialized-server-data">'
        + json.dumps({"data": [{"data": {"sections": [{"items": [node]}]}}]})
        + "</script></html>"
    ).encode()


@pytest.mark.parametrize(
    "badges,expected",
    [
        ({"lossless": True, "hiResLossless": True}, ("hi-res-lossless", "lossless")),
        ({"lossless": True, "dolbyAtmos": True}, ("dolby-atmos", "lossless")),
        ({"dolbyAudio": True}, ("dolby-audio",)),
        ({}, ()),
    ],
)
def test_album_header_badges(badges, expected):
    # Shape/values checked against public Punisher/Sunbather headers, 2026-11-04.
    assert parse_album_badges(document(page(**badges)), "br", "2") == expected


@pytest.mark.parametrize(
    "change",
    [
        lambda n: n.update(id="recommendation - 2"),
        lambda n: n["contentDescriptor"].update(kind="song"),
        lambda n: n["contentDescriptor"]["identifiers"].update(storeAdamID="3"),
        lambda n: n["contentDescriptor"].update(url="https://music.apple.com/us/album/2"),
        lambda n: n["contentDescriptor"].update(url="https://evil.example/br/album/2"),
        lambda n: n["audioBadges"].update(lossless="true"),
        lambda n: n["audioBadges"].pop("lossless"),
        lambda n: n.update(audioBadges=None),
    ],
)
def test_changed_or_unrelated_header_is_unknown(change):
    node = page(lossless=True)
    change(node)
    assert parse_album_badges(document(node), "br", "2") is None


def test_untrusted_or_oversized_page_is_unknown():
    assert parse_album_badges(b"not json", "br", "2") is None
    assert parse_album_badges(b"\xff", "br", "2") is None
    assert parse_album_badges(b"x" * (MAX_PAGE_BYTES + 1), "br", "2") is None
    assert parse_album_badges(document(page()) * 2, "br", "2") is None
    assert parse_album_badges(document([page(), page()]), "br", "2") is None
    assert parse_album_badges(
        document([page("3", dolbyAtmos=True), page(lossless=True)]), "br", "2"
    ) == ("lossless",)


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://music.apple.com/br/album/2", True),
        ("https://music.apple.com/br/album/record/2", True),
        ("http://music.apple.com/br/album/2", False),
        ("https://music.apple.com.evil.example/br/album/2", False),
        ("https://music.apple.com@evil.example/br/album/2", False),
        ("https://music.apple.com:443/br/album/2", False),
        ("https://music.apple.com/us/album/2", False),
        ("https://music.apple.com/br/song/2", False),
        ("https://music.apple.com/br/album/3", False),
        ("https://music.apple.com/br/album/2?token=x", False),
        ("https://music.apple.com/br/album/2#2", False),
    ],
)
def test_url_and_redirect_boundaries(url, allowed):
    assert album_page_url(url, "br", "2") is allowed
    redirect = _AlbumRedirect("br", "2")
    req = Request("https://music.apple.com/br/album/2")
    if allowed:
        assert redirect.redirect_request(req, None, 302, "Found", {}, url).full_url == url
    else:
        with pytest.raises(ValueError):
            redirect.redirect_request(req, None, 302, "Found", {}, url)


def test_fetch_is_metadata_only_bounded_and_credential_free(monkeypatch):
    class Response(io.BytesIO):
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"

        def geturl(self):
            return "https://music.apple.com/br/album/example/2"

        def read(self, size=-1):
            assert size == MAX_PAGE_BYTES + 1
            return super().read(size)

    class Opener:
        def open(self, req, timeout):
            assert req.full_url == "https://music.apple.com/br/album/2"
            assert timeout == 15
            assert {k.lower(): v for k, v in req.header_items()} == {"accept": "text/html"}
            return Response(document(page(dolbyAtmos=True)))

    monkeypatch.setattr(apple_album, "build_opener", lambda *handlers: Opener())
    assert fetch_album_badges("br", "2") == ("dolby-atmos",)
    for country, identifier in (("../us", "2"), ("br", "2?secret=1")):
        with pytest.raises(ValueError):
            fetch_album_badges(country, identifier)


def test_cache_expires_failure_recovers_and_retention_is_bounded():
    now = [0]
    calls = []

    def fetch(country, identifier):
        calls.append((country, identifier))
        if identifier == "2" and len(calls) == 1:
            raise OSError("unavailable")
        return ("lossless",)

    service = AlbumQuality(fetch=fetch, clock=lambda: now[0])
    assert service.get("br", "2") == []
    assert service.get("br", "2") == []
    assert len(calls) == 1
    now[0] = 301
    assert service.get("br", "2") == ["lossless"]
    assert service.get("br", "2") == ["lossless"]
    assert len(calls) == 2
    now[0] += 86401
    assert service.get("br", "2") == ["lossless"]
    assert len(calls) == 3
    for i in range(300):
        service.get("br", str(i))
    assert len(service.cache) == 256


def test_rate_limit_cools_down_all_albums():
    now = [0]
    calls = []

    def fetch(*args):
        calls.append(args)
        raise HTTPError("https://music.apple.com", 429, "Too many requests", {}, None)

    service = AlbumQuality(fetch=fetch, clock=lambda: now[0])
    assert service.get("br", "2") == []
    assert service.get("br", "3") == []
    assert len(calls) == 1
    now[0] = 301
    assert service.get("br", "3") == []
    assert len(calls) == 2


def test_duplicate_requests_share_work_and_parallel_work_is_bounded():
    started = threading.Barrier(3)
    release = threading.Event()
    calls = []

    def fetch(country, identifier):
        calls.append(identifier)
        started.wait(timeout=3)
        release.wait(timeout=3)
        return ("lossless",)

    service = AlbumQuality(fetch=fetch)
    with ThreadPoolExecutor(4) as pool:
        a = pool.submit(service.get, "br", "2")
        b = pool.submit(service.get, "br", "3")
        started.wait(timeout=3)
        duplicate = pool.submit(service.get, "br", "2")
        assert service.get("br", "4") == []
        release.set()
        assert a.result() == b.result() == duplicate.result() == ["lossless"]
    assert sorted(calls) == ["2", "3"]
    assert not service.flights


def test_quality_extension_auth_identity_and_no_media_downloads(bridge):  # noqa: F811
    server, downloads = bridge
    calls = []
    server.album_quality = AlbumQuality(
        fetch=lambda country, identifier: calls.append((country, identifier)) or ("lossless",)
    )
    ext = payload(server, "getOpenSubsonicExtensions", auth=False)["openSubsonicExtensions"]
    assert {"name": "appleMusicAlbumAudioQuality", "versions": [1]} in ext
    args = {"id": "am:br:lossless:album:2"}
    assert payload(server, "getAlbumAudioQuality", args, auth=False)["error"]["code"] == 40
    assert not calls
    result = payload(server, "getAlbumAudioQuality", args)["albumAudioQuality"]
    assert result == {"id": args["id"], "source": "apple-music-catalog", "available": ["lossless"]}
    assert calls == [("br", "2")]
    for bad in ("am:us:lossless:album:2", "am:br:atmos:album:2", "am:br:lossless:song:2"):
        assert payload(server, "getAlbumAudioQuality", {"id": bad})["error"]["code"] == 70
    assert not downloads
    assert b"albumAudioQuality" in request(server, "getAlbumAudioQuality", {**args, "f": "xml"})[2]
