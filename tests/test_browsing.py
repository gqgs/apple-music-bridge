import io
from datetime import date
from email.message import Message
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apple_music_player.artwork import (
    BADGE_URL,
    MAX_BYTES,
    ArtworkCache,
    ArtworkRedirect,
    allowed_url,
    decode,
)
from apple_music_player.catalog import Catalog, recent_releases
from apple_music_player.model import Album, Track

URL = "https://is1-ssl.mzstatic.com/image/thumb/fixture/100x100bb.jpg"
TRACK = Track(
    "br",
    "101",
    "Still — Fixture",
    "201",
    "Fixture",
    "301",
    "Quiet Places",
    URL,
    "2026-10-01T00:00:00Z",
)


def png(width=100, height=100):
    from PIL import Image

    data = io.BytesIO()
    Image.new("RGB", (width, height), "#537780").save(data, format="PNG")
    return data.getvalue()


@pytest.mark.parametrize(
    "url",
    [
        "http://is1-ssl.mzstatic.com/x",
        "https://is1-ssl.mzstatic.com.evil.test/x",
        "https://user:password@is1-ssl.mzstatic.com/x",
        "https://127.0.0.1/x",
        "file:///tmp/x",
        "https://is1-ssl.mzstatic.com:1234/x",
        "https://is1-ssl.mzstatic.com/x#fragment",
    ],
)
def test_unsafe_artwork_urls(url):
    assert not allowed_url(url)


def test_artwork_validation_redirect_and_dimensions():
    assert allowed_url(URL) and allowed_url(BADGE_URL)
    with pytest.raises(ValueError):
        ArtworkRedirect().redirect_request(None, None, 302, "", {}, "http://localhost/x")
    for payload in (b"not an image", b"x" * (MAX_BYTES + 1), png(2049, 1)):
        with pytest.raises(ValueError):
            decode(payload)
    assert decode(png()).width == 100


def test_art_cache_corruption_limit_symlink_and_restart(tmp_path, monkeypatch):
    payload = png()
    calls = []

    def opened(request, **kwargs):
        calls.append(request.full_url)
        result = io.BytesIO(payload)
        result.url = request.full_url
        result.headers = Message()
        result.headers["Content-Type"] = "image/png"
        return result

    monkeypatch.setattr(
        "apple_music_player.artwork.build_opener", lambda *_: SimpleNamespace(open=opened)
    )
    cache = ArtworkCache(tmp_path / "art", limit=len(payload) * 2)
    path = cache.root / (sha256(URL.encode()).hexdigest() + ".img")
    path.write_bytes(b"broken")
    assert cache.load(URL) is not None
    assert cache.load(URL) is not None and len(calls) == 1
    for n in range(3):
        assert cache.load(URL + "?v=" + str(n)) is not None
    assert sum(p.stat().st_size for p in cache.root.glob("*.img")) <= cache.limit
    orphan = cache.root / ".part-fixture"
    orphan.write_bytes(b"orphan")
    ArtworkCache(cache.root, cache.limit)
    assert not orphan.exists()
    path.unlink(missing_ok=True)
    outside = tmp_path / "outside"
    outside.write_bytes(payload)
    path.symlink_to(outside)
    assert cache.load(URL) is None
    assert outside.read_bytes() == payload


def test_recent_release_dates_and_artist_contract(monkeypatch):
    albums = [
        Album("br", str(i), "Fixture", release_date=day)
        for i, day in enumerate(["2026-10-01", "2026-11-03", "2020-01-01", "", "2026-09-01"])
    ]
    assert [a.id for a in recent_releases(albums + albums, date(2026, 11, 2))] == ["0", "4"]
    catalog = Catalog()
    request = Mock(return_value=[])
    monkeypatch.setattr(catalog, "request", request)
    assert catalog.artist_albums("301", "br") == []
    request.assert_called_once_with("lookup", id="301", country="br", entity="album", limit=200)
    assert catalog.artist_albums("", "br") == []
    assert not recent_releases([])


@pytest.mark.parametrize("kind,reason", [("403", "denied"), ("timeout", "network")])
def test_cache_failure_reason_sanitized(tmp_path, monkeypatch, kind, reason):
    from urllib.error import URLError

    def opened(*args, **kwargs):
        raise URLError("403 SECRET" if kind == "403" else TimeoutError("SECRET"))

    monkeypatch.setattr(
        "apple_music_player.artwork.build_opener", lambda *_: SimpleNamespace(open=opened)
    )
    cache = ArtworkCache(tmp_path / "art")
    assert cache.load(URL) is None
    assert cache.failures == {URL: reason}
