import io
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email.message import Message
from hashlib import sha256
from types import SimpleNamespace

import pytest
from test_bridge import ALBUM, ARTIST, TRACK, payload, request
from test_bridge import bridge as _bridge

from apple_music_player.artwork import MAX_BYTES, ArtworkRedirect, decode
from apple_music_player.bridge.artwork import Covers, CoverUnavailable

bridge = _bridge

URL = "https://is1-ssl.mzstatic.com/image/thumb/fixture/100x100bb.jpg"


def image_bytes(fmt="PNG", width=100):
    from PIL import Image

    data = io.BytesIO()
    Image.new("RGB", (width, 10), "#336699").save(data, format=fmt)
    return data.getvalue()


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
        assert first.startswith(b"\x89PNG") and decode(first) is not None
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
        with pytest.raises(CoverUnavailable):
            covers.get(url)
        assert not calls
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
        with pytest.raises(CoverUnavailable):
            covers.get(URL, 100)
        with pytest.raises(CoverUnavailable):
            covers.get(URL, 100)
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
        assert decode(response[2]) is not None
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
        return decode(image_bytes(), 1024)

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
            with pytest.raises(CoverUnavailable):
                covers.get(URL, 100)
            wait_until(lambda: not covers.pending)
            with pytest.raises(CoverUnavailable):
                covers.get(URL, 100)
            assert len(calls) == attempt + 1
            now[0] += 31
        with pytest.raises(CoverUnavailable):
            covers.get(URL, 100)
        assert len(calls) == 3
        with pytest.raises(CoverUnavailable):
            covers.get(URL + "denied")
        wait_until(lambda: not covers.pending)
        now[0] += 200
        with pytest.raises(CoverUnavailable):
            covers.get(URL + "denied")
        assert len(calls) == 4
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
        return decode(image_bytes(), 1024)

    monkeypatch.setattr(covers, "load", slow)
    with ThreadPoolExecutor(8) as pool:
        futures = [pool.submit(covers.get, URL + str(n)) for n in range(8)]
        try:
            wait_until(lambda: len(covers.pending) == 8)
            assert len(calls) == 2
            with pytest.raises(CoverUnavailable):
                covers.get(URL + "excess")
            covers.close()  # Queued work is cancelled; running fetches finish independently.
            with pytest.raises(CoverUnavailable):
                covers.get(URL, 100)
        finally:
            release.set()
        successes = 0
        for future in futures:
            try:
                successes += bool(future.result())
            except CoverUnavailable:
                pass
        assert successes == 2


def test_higher_resolution_and_client_size(tmp_path, monkeypatch):
    calls = transport(
        monkeypatch, image_bytes(width=1400), final=URL.replace("100x100", "1400x1400")
    )
    covers = Covers(tmp_path / "art")
    try:
        image = decode(covers.get(URL, 600), 2048)
        assert image.width == 600  # No inherited 256px decoder ceiling.
        assert calls == [(URL.replace("100x100", "600x600"), 8)]
        assert decode(covers.get(URL, 5000), 2048).width == 1400
        assert len(calls) == 2
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
        assert decode(covers.get(URL)).width == 100  # Never upscale fallback.
        assert decode(covers.get(URL)).width == 100
        assert calls == [URL.replace("100x100", "1400x1400"), URL]
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
    assert decode(response[2], 2048).width == 400
    assert calls == [(URL.replace("100x100", "400x400"), 8)]
    assert (
        payload(server, "getCoverArt", {"id": "am:br:lossless:album:2", "size": "-1"})["status"]
        == "failed"
    )


@pytest.mark.parametrize("size", [None, "0", "1400", "4096"])
def test_full_cover_is_bounded_1400_instead_of_thumbnail(bridge, monkeypatch, size):
    server, _ = bridge
    server.actor.call("remember", [replace(ALBUM, artwork_url=URL)])
    calls = transport(monkeypatch, image_bytes(width=1400))
    params = {"id": "am:br:lossless:album:2"}
    if size is not None:
        params["size"] = size
    response = request(server, "getCoverArt", params)
    assert response[0] == 200
    assert decode(response[2], 2048).width == 1400
    assert calls == [(URL.replace("100x100", "1400x1400"), 8)]


def test_300_thumbnail_uses_matching_cdn_bucket_and_encodes_once(tmp_path, monkeypatch):
    from apple_music_player.bridge import artwork

    calls = transport(monkeypatch, image_bytes(width=300))
    encoded_sizes = []
    original_png = artwork.png

    def encode(image):
        encoded_sizes.append(image.size)
        return original_png(image)

    monkeypatch.setattr(artwork, "png", encode)
    covers = Covers(tmp_path)
    try:
        for _ in range(2):  # Cold network load, then warm disk load.
            assert decode(covers.get(URL, 300), 2048).width == 300
        assert calls == [(URL.replace("100x100", "300x300"), 8)]
        assert len(encoded_sizes) == 2
        assert all(size[0] == 300 for size in encoded_sizes)
    finally:
        covers.close()


def test_shared_decoded_source_is_not_resized_in_place(tmp_path, monkeypatch):
    covers = Covers(tmp_path)
    shared = decode(image_bytes(width=400), 1024)
    monkeypatch.setattr(covers, "_get", lambda *args, **kwargs: shared)
    try:
        with ThreadPoolExecutor(4) as pool:
            sizes = [100, 300, 80, 400] * 5
            results = list(pool.map(lambda size: decode(covers.get(URL, size), 2048).width, sizes))
        assert results == sizes
        assert shared.width == 400
        assert not hasattr(shared, "encoderinfo")
    finally:
        covers.close()


def test_supersonic_five_worker_burst_returns_every_cover(bridge, monkeypatch):
    import time

    server, _ = bridge
    items = [
        replace(ALBUM, id=str(100 + i), artwork_url=URL.replace("fixture", f"fixture-{i}"))
        for i in range(40)
    ]
    server.actor.call("remember", items)
    calls = []

    def delayed(url):
        calls.append(url)
        time.sleep(0.015)
        return decode(image_bytes(width=400), 1024)

    monkeypatch.setattr(server.covers, "load", delayed)

    def fetch(item):
        return request(
            server, "getCoverArt", {"id": f"am:br:lossless:album:{item.id}", "size": "300"}
        )

    with ThreadPoolExecutor(5) as pool:
        responses = list(pool.map(fetch, items))
    assert all(
        status == 200 and headers["X-Artwork-Status"] == "available"
        for status, headers, _ in responses
    )
    assert len(calls) == 40


def test_failures_are_not_successful_images_and_diagnostics_are_sanitized(bridge, monkeypatch):
    server, _ = bridge
    calls = transport(monkeypatch, b"not an image")
    server.actor.call("remember", [replace(ALBUM, artwork_url=URL)])
    response = request(server, "getCoverArt", {"id": "am:br:lossless:album:2"})
    assert response[0] == 503 and response[1]["Cache-Control"] == "no-store"
    assert response[1]["Content-Type"].startswith("application/xml")
    assert b"<error " in response[2]  # go-subsonic parses XML instead of decoding an image.
    diagnostics = payload(server, "getBridgeDiagnostics")
    assert diagnostics["status"] == "ok" and diagnostics["bridgeDiagnostics"]["metric"]
    text = str(diagnostics)
    assert URL not in text and "fixture" not in text and "apiKey" not in text
    assert payload(server, "getBridgeDiagnostics", auth=False)["error"]["code"] == 40
    assert len(calls) == 2  # Failed variant then failed original.


def test_failure_map_does_not_suppress_unrelated_or_warm_covers(tmp_path, monkeypatch):
    import time
    from concurrent.futures import Future

    covers = Covers(tmp_path / "art")
    calls = transport(monkeypatch, image_bytes())
    try:
        assert covers.get(URL, 100)
        for i in range(300):
            future = Future()
            future.set_result(b"")
            covers.finished(URL + f"?failed={i}", future)
        assert len(covers.failures) == 256
        # Even a full in-flight queue and negative map cannot block a disk hit.
        with covers.lock:
            covers.pending.update({f"pending-{i}": Future() for i in range(8)})
        assert covers.get(URL, 100) and len(calls) == 1
        with covers.lock:
            covers.pending.clear()
        assert covers.get(URL + "?new=1", 100) and len(calls) == 2
        assert all(after > time.monotonic() for _, after in covers.failures.values())
    finally:
        covers.close()


def test_busy_metadata_is_not_a_successful_placeholder(bridge):
    import sqlite3

    server, _ = bridge
    server.actor.call("remember", [ALBUM])
    connection = sqlite3.connect(server.actor.core.cache.root / "index.sqlite3")
    try:
        connection.execute("BEGIN EXCLUSIVE")
        response = request(server, "getCoverArt", {"id": "am:br:lossless:album:2"})
        assert response[1]["Content-Type"] != "image/png"
        assert b'status="failed"' in response[2]
    finally:
        connection.rollback()
        connection.close()


def raw_cover_response(server, identifier, *, timeout=3):
    import http.client
    from urllib.parse import urlencode

    from test_bridge import KEY

    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
    try:
        query = urlencode(
            {"apiKey": KEY, "v": "1.16.1", "c": "fixture", "f": "json", "id": identifier}
        )
        conn.request("GET", "/rest/getCoverArt?" + query)
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


def test_album_success_has_one_cache_policy_and_needs_no_client_retry(bridge, monkeypatch):
    server, _ = bridge
    calls = transport(monkeypatch, image_bytes("JPEG"))
    server.actor.call("remember", [replace(ALBUM, artwork_url=URL)])
    responses = []
    # Mirror the current client's first-header no-store retry decision, without sleeps.
    for _ in range(4):
        status, headers, body = raw_cover_response(server, "am:br:lossless:album:2")
        responses.append(status)
        assert status == 200 and decode(body) is not None
        if "no-store" not in headers.get("Cache-Control", ""):
            break
    assert responses == [200]
    assert headers.get_all("Cache-Control") == ["private, max-age=86400"]
    assert len(calls) == 1


def test_cover_failure_has_one_no_store_policy(bridge, monkeypatch):
    server, _ = bridge
    server.actor.call("remember", [replace(ALBUM, artwork_url=URL)])
    transport(monkeypatch, b"invalid image")
    status, headers, body = raw_cover_response(server, "am:br:lossless:album:2")
    assert status == 503
    assert headers.get_all("Cache-Control") == ["no-store"]
    assert headers.get_content_type() == "application/xml" and b"<error " in body


@pytest.mark.parametrize(
    "params,auth,status,reason",
    [
        ({"id": "artist-release:am:br:lossless:artist:999"}, True, 404, "item-unavailable"),
        ({"id": "artist-image:am:br:lossless:artist:1"}, True, 404, "item-unavailable"),
        ({"id": "am:br:lossless:album:2", "size": "invalid"}, True, 400, "invalid-request"),
        ({"id": "am:br:lossless:album:2"}, False, 401, "authentication"),
    ],
)
def test_cover_protocol_errors_never_look_like_successful_images(
    bridge, params, auth, status, reason
):
    server, _ = bridge
    actual, headers, body = request(server, "getCoverArt", dict(params, f="json"), auth=auth)
    assert actual == status and headers["X-Artwork-Reason"] == reason
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Type"].startswith("application/xml") and b"<error " in body


@pytest.mark.parametrize("width", [1024, 1400])
def test_normalized_png_can_exceed_source_byte_cap(tmp_path, monkeypatch, width):
    import random

    from PIL import Image

    # Valid small JPEG expands to >2 MiB PNG after decoding; source limit still applies.
    source = Image.frombytes("RGB", (width, width), random.Random(7).randbytes(width * width * 3))
    stream = io.BytesIO()
    source.save(stream, format="JPEG", quality=85)
    assert len(stream.getvalue()) < MAX_BYTES
    transport(monkeypatch, stream.getvalue())
    covers = Covers(tmp_path)
    try:
        output = covers.get(URL, width)
        assert len(output) > MAX_BYTES
        with Image.open(io.BytesIO(output)) as image:
            image.load()
            assert image.size == (width, width) and image.format == "PNG"
    finally:
        covers.close()
