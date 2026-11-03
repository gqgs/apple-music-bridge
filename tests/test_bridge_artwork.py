import io
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email.message import Message
from hashlib import sha256
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage
from test_bridge import ALBUM, ARTIST, TRACK, payload, request
from test_bridge import bridge as _bridge

from apple_music_player.artwork import MAX_BYTES, ArtworkRedirect
from apple_music_player.bridge.artwork import Covers

bridge = _bridge

URL = "https://is1-ssl.mzstatic.com/image/thumb/fixture/100x100bb.jpg"


def image_bytes(fmt="PNG", width=100):
    image = QImage(width, 10, QImage.Format_RGB32)
    image.fill(0xFF336699)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, fmt)
    return bytes(buffer.data())


def transport(monkeypatch, data, mime="image/jpeg", final=URL):
    calls = []

    def opened(req, timeout):
        calls.append((req.full_url, timeout))
        response = io.BytesIO(data)
        response.url = final
        response.headers = Message()
        response.headers["Content-Type"] = mime
        return response

    monkeypatch.setattr(
        "apple_music_player.artwork.build_opener", lambda *_: SimpleNamespace(open=opened)
    )
    return calls


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_decodes_actual_bytes_normalizes_png_and_caches(tmp_path, monkeypatch, fmt):
    calls = transport(monkeypatch, image_bytes(fmt))  # Deliberately JPEG MIME for every format.
    covers = Covers(tmp_path / "art")
    try:
        first = covers.get(URL, 100)
        assert first.startswith(b"\x89PNG") and not QImage.fromData(first).isNull()
        assert covers.get(URL, 100) == first
        assert calls == [(URL, 8)]  # No speculative resizing or format suffix rewrite.
    finally:
        covers.close()
    restarted = Covers(tmp_path / "art")
    try:
        assert restarted.get(URL, 100) == first and len(calls) == 1
        path = restarted.cache.root / (sha256(URL.encode()).hexdigest() + ".img")
        path.write_bytes(b"corrupt")
        assert restarted.get(URL, 100) == first and len(calls) == 2
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/no",
        "http://is1-ssl.mzstatic.com/x",
        "https://localhost/x",
        "https://is1-ssl.mzstatic.com.evil/x",
        "https://user:pass@is1-ssl.mzstatic.com/x",
    ],
)
def test_unsafe_urls_and_redirects_are_not_fetched(tmp_path, monkeypatch, url):
    calls = transport(monkeypatch, image_bytes())
    covers = Covers(tmp_path / "art")
    try:
        assert covers.get(url) == b"" and not calls
        with pytest.raises(ValueError):
            ArtworkRedirect().redirect_request(None, None, 302, "", {}, url)
    finally:
        covers.close()


@pytest.mark.parametrize(
    "data,mime,final",
    [
        (b"<html>error</html>", "image/jpeg", URL),
        (b"x" * (MAX_BYTES + 1), "image/png", URL),
        (image_bytes(width=2049), "image/png", URL),
        (image_bytes(), "text/html", URL),
        (image_bytes(), "image/png", "http://localhost/x"),
    ],
)
def test_invalid_responses_fall_back_and_do_not_hot_retry(tmp_path, monkeypatch, data, mime, final):
    calls = transport(monkeypatch, data, mime, final)
    covers = Covers(tmp_path / "art")
    try:
        assert covers.get(URL, 100) == b""
        assert covers.get(URL, 100) == b""
        assert len(calls) == 1
        assert not list(covers.cache.root.glob("*.img"))
    finally:
        covers.close()


def test_http_song_album_artist_auth_and_mime(bridge, monkeypatch):
    server, _ = bridge
    calls = transport(monkeypatch, image_bytes("WEBP"), "image/webp")
    server.actor.call("remember", [replace(ALBUM, artwork_url=URL), ARTIST, TRACK])
    for kind, identifier in (("album", "2"), ("song", "3")):
        response = request(
            server, "getCoverArt", {"id": f"am:br:lossless:{kind}:{identifier}", "size": "100"}
        )
        assert response[1]["Content-Type"] == "image/png"
        assert response[1]["X-Artwork-Status"] == "available"
        assert not QImage.fromData(response[2]).isNull()
    assert len(calls) == 1
    artist = request(server, "getCoverArt", {"id": "am:br:lossless:artist:1"})
    assert artist[1]["X-Artwork-Status"] == "placeholder"
    assert (
        payload(server, "getCoverArt", {"id": "am:br:lossless:album:2", "size": "100"}, auth=False)[
            "error"
        ]["code"]
        == 40
    )
    assert len(calls) == 1


def test_slow_art_deduplicates_without_blocking_ping_or_core(bridge, monkeypatch):
    server, _ = bridge
    server.actor.call("remember", [replace(ALBUM, artwork_url=URL)])
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(url):
        calls.append(url)
        entered.set()
        assert release.wait(3)
        return image_bytes()

    monkeypatch.setattr(server.covers, "load", slow)
    with ThreadPoolExecutor(5) as pool:
        first = pool.submit(
            request, server, "getCoverArt", {"id": "am:br:lossless:album:2", "size": "100"}
        )
        assert entered.wait(1)
        second = pool.submit(server.covers.get, URL, 100)
        try:
            assert payload(server, "ping")["status"] == "ok"
            assert server.actor.call("dispatch", "getMusicFolders", {})
        finally:
            release.set()
        assert first.result()[1]["X-Artwork-Status"] == "available"
        assert second.result().startswith(b"\x89PNG")
    assert calls == [URL]


def test_transient_retry_is_bounded_and_denials_are_not_retried(tmp_path, monkeypatch):
    from urllib.error import HTTPError, URLError

    from conftest import wait_until

    now = [0.0]
    monkeypatch.setattr("apple_music_player.bridge.artwork.time.monotonic", lambda: now[0])
    calls = []

    def opened(req, **kwargs):
        calls.append(req.full_url)
        if req.full_url.endswith("denied"):
            raise HTTPError(req.full_url, 403, "private error", {}, None)
        raise URLError("private error")

    monkeypatch.setattr(
        "apple_music_player.artwork.build_opener", lambda *_: SimpleNamespace(open=opened)
    )
    covers = Covers(tmp_path / "art")
    try:
        for attempt in range(3):
            assert covers.get(URL, 100) == b""
            wait_until(lambda: not covers.pending)
            assert covers.get(URL, 100) == b""
            assert len(calls) == attempt + 1
            now[0] += 31
        assert covers.get(URL, 100) == b"" and len(calls) == 3
        assert covers.get(URL + "denied") == b""
        wait_until(lambda: not covers.pending)
        now[0] += 99999
        assert covers.get(URL + "denied") == b"" and len(calls) == 4
    finally:
        covers.close()


def test_worker_and_waiter_bounds_and_close(tmp_path, monkeypatch):
    from conftest import wait_until

    covers = Covers(tmp_path / "art")
    release = threading.Event()
    calls = []

    def slow(url):
        calls.append(url)
        assert release.wait(3)
        return image_bytes()

    monkeypatch.setattr(covers, "load", slow)
    with ThreadPoolExecutor(4) as pool:
        futures = [pool.submit(covers.get, URL + str(n)) for n in range(4)]
        try:
            wait_until(lambda: len(covers.pending) == 4)
            assert len(calls) == 2
            assert covers.get(URL + "excess") == b""
            covers.close()  # Queued work is cancelled; running fetches finish independently.
            assert covers.get(URL, 100) == b""
        finally:
            release.set()
        assert sum(bool(f.result()) for f in futures) == 2


def test_higher_resolution_and_client_size(tmp_path, monkeypatch):
    calls = transport(
        monkeypatch, image_bytes(width=1024), final=URL.replace("100x100", "1024x1024")
    )
    covers = Covers(tmp_path / "art")
    try:
        image = QImage.fromData(covers.get(URL, 600))
        assert image.width() == 600  # No inherited 256px decoder ceiling.
        assert calls == [(URL.replace("100x100", "1024x1024"), 8)]
        assert QImage.fromData(covers.get(URL, 5000)).width() == 1024
        assert len(calls) == 1
    finally:
        covers.close()


def test_bad_variant_falls_back_to_original_and_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def opened(req, **kwargs):
        calls.append(req.full_url)
        response = io.BytesIO(image_bytes() if req.full_url == URL else b"not an image")
        response.url = req.full_url
        response.headers = Message()
        response.headers["Content-Type"] = "image/jpeg"
        return response

    monkeypatch.setattr(
        "apple_music_player.artwork.build_opener", lambda *_: SimpleNamespace(open=opened)
    )
    covers = Covers(tmp_path / "art")
    try:
        assert QImage.fromData(covers.get(URL)).width() == 100  # Never upscale fallback.
        assert QImage.fromData(covers.get(URL)).width() == 100
        assert calls == [URL.replace("100x100", "400x400"), URL]
    finally:
        covers.close()


@pytest.mark.parametrize(
    "url",
    [
        URL + "?signature=abc",
        URL.replace("100x100bb", "100x100"),
        URL.replace("/image/thumb/", "/other/"),
        URL.replace("100x100", "100x60"),
    ],
)
def test_unknown_or_signed_urls_are_not_rewritten(url):
    from apple_music_player.bridge.artwork import sized_url

    assert sized_url(url, 512) == url


def test_http_requested_cover_size(bridge, monkeypatch):
    server, _ = bridge
    server.actor.call("remember", [replace(ALBUM, artwork_url=URL)])
    calls = transport(monkeypatch, image_bytes(width=512))
    response = request(server, "getCoverArt", {"id": "am:br:lossless:album:2", "size": "400"})
    assert QImage.fromData(response[2]).width() == 400
    assert calls == [(URL.replace("100x100", "400x400"), 8)]
    assert (
        payload(server, "getCoverArt", {"id": "am:br:lossless:album:2", "size": "-1"})["status"]
        == "failed"
    )
