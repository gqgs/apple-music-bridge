"""Binary stream errors must never masquerade as cacheable successful audio."""

import http.client
import io
import json
import threading
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from test_bridge import KEY, TRACK, FakeCatalog, request
from test_format_fallback import NO_CODEC
from test_format_fallback import format_downloader as format_downloader

from apple_music_player.bridge.core import Actor, BridgeError
from apple_music_player.bridge.http import Handler, ProtocolCore, Server
from apple_music_player.downloader import DIAGNOSTICS, Downloader, DownloadError
from apple_music_player.model import Media, PlayerError

PRIVATE = "PRIVATE_TOKEN /private/session.json https://example.invalid/?token=SECRET"
IDENTIFIER = "am:br:atmos:song:3"


@pytest.fixture
def stream_bridge(tmp_path):
    class FailingDownloader:
        failure = DownloadError("wrapper-m3u8-api-404")
        calls = 0

        def run(self, *args):
            self.calls += 1
            raise self.failure

    actor = Actor(
        lambda cancel: ProtocolCore(
            tmp_path / "cache",
            1024**2,
            quality="atmos",
            catalog=FakeCatalog(),
            downloader_factory=lambda stage: FailingDownloader(),
            cancel=cancel,
        )
    )
    actor.call("remember", [TRACK])
    server = Server(actor, {"username": "bridge", "key": KEY}, port=0, quality="atmos")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        yield server
    finally:
        actor.cancel.set()
        server.shutdown()
        server.server_close()
        thread.join()
        actor.close()


def assert_stream_error(response, status, code, reason):
    actual, headers, body = response
    assert actual == status
    assert headers["Content-Type"] == "text/xml"
    assert headers["X-Stream-Error"] == reason
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Length"] == str(len(body))
    assert not any(name.startswith("X-Media-") for name in headers)
    if status == 503:
        assert headers["Retry-After"] == "3"
    root = ET.fromstring(body)
    assert root.attrib["status"] == "failed"
    error = root.find("{http://subsonic.org/restapi}error")
    assert error.attrib["code"] == str(code)
    assert f"[{reason}]" in error.attrib["message"]
    assert PRIVATE not in str(response) and KEY not in str(response)
    return error.attrib["message"]


@pytest.mark.parametrize("fmt", ["json", "xml"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_download_error_xml_even_when_json_requested(stream_bridge, fmt, method):
    error = DownloadError("wrapper-m3u8-api-404")
    error.args = (str(error) + PRIVATE,)
    stream_bridge.actor.core.downloader.failure = error
    result = request(stream_bridge, "stream", {"id": IDENTIFIER, "f": fmt}, method=method)
    message = assert_stream_error(result, 503, 0, "wrapper-m3u8-api-404")
    assert "does not prove expired login" in message
    assert stream_bridge.actor.call("describe", TRACK).get("size") is None
    assert not stream_bridge.actor.core.pins


@pytest.mark.parametrize("category", sorted(DIAGNOSTICS))
def test_all_typed_diagnostics_remain_safe(stream_bridge, category):
    error = DownloadError(category)
    error.args = (str(error) + PRIVATE,)
    stream_bridge.actor.core.downloader.failure = error
    message = assert_stream_error(
        request(stream_bridge, "stream", {"id": IDENTIFIER}), 503, 0, category
    )
    assert DIAGNOSTICS[category] in message


@pytest.mark.parametrize(
    "error,status,reason",
    [
        (PlayerError("Download failed [manifest]. " + PRIVATE), 503, "stream-unavailable"),
        (OSError(PRIVATE), 503, "stream-unavailable"),
        (ValueError(PRIVATE), 503, "stream-unavailable"),
        (RuntimeError(PRIVATE), 500, "stream-failed"),
        (BridgeError(0, PRIVATE), 503, "stream-unavailable"),
        (BridgeError(70, PRIVATE), 404, "media-unavailable"),
    ],
)
def test_untyped_exception_text_is_not_forwarded(stream_bridge, error, status, reason):
    stream_bridge.actor.core.downloader.failure = error
    code = error.code if isinstance(error, BridgeError) else 0
    assert_stream_error(request(stream_bridge, "stream", {"id": IDENTIFIER}), status, code, reason)


@pytest.mark.parametrize("category", ["wrapper-m3u8-api-404 " + PRIVATE, PRIVATE, [PRIVATE], None])
def test_invalid_category_is_not_forwarded(stream_bridge, category):
    error = DownloadError(category)
    assert error.category == "download" and PRIVATE not in str(error)
    error.category = category  # Revalidate at the boundary, not just construction.
    error.exit_code = PRIVATE
    stream_bridge.actor.core.downloader.failure = error
    assert_stream_error(
        request(stream_bridge, "stream", {"id": IDENTIFIER}), 503, 0, "stream-unavailable"
    )


@pytest.mark.parametrize("exit_code", [-9, 0, 1, 255, PRIVATE, None, True, 256])
def test_process_exit_is_validated(stream_bridge, exit_code):
    error = DownloadError("no-result", exit_code=exit_code)
    stream_bridge.actor.core.downloader.failure = error
    message = assert_stream_error(
        request(stream_bridge, "stream", {"id": IDENTIFIER}), 503, 0, "no-result"
    )
    valid = type(exit_code) is int and -255 <= exit_code <= 255
    assert ("(exit " in message) == valid
    assert ("; exit " in str(error)) == valid
    error.exit_code = PRIVATE
    message = assert_stream_error(
        request(stream_bridge, "stream", {"id": IDENTIFIER}), 503, 0, "no-result"
    )
    assert "(exit " not in message


@pytest.mark.parametrize(
    "params,auth,status,code,reason",
    [
        ({}, False, 401, 40, "authentication"),
        ({"apiKey": "wrong"}, True, 401, 40, "authentication"),
        ({"u": "bridge"}, True, 400, 43, "authentication-conflict"),
        ({"id": "../../private"}, True, 404, 70, "media-unavailable"),
        ({"id": "am:br:lossless:song:3"}, True, 404, 70, "media-unavailable"),
        ({"format": "mp3"}, True, 400, 10, "invalid-request"),
        ({"maxBitRate": "128"}, True, 400, 10, "invalid-request"),
        ({"timeOffset": "1"}, True, 400, 10, "invalid-request"),
        ({"f": "jsonp"}, True, 400, 10, "invalid-request"),
        ({"c": ""}, True, 400, 10, "invalid-request"),
        ({"v": "1.99.0"}, True, 400, 30, "api-version"),
    ],
)
def test_stream_validation_precedes_download(stream_bridge, params, auth, status, code, reason):
    result = request(stream_bridge, "stream", {"id": IDENTIFIER, **params}, auth=auth)
    assert_stream_error(result, status, code, reason)
    assert stream_bridge.actor.core.downloader.calls == 0


def test_cold_head_errors_have_length_but_no_body_and_never_download(stream_bridge):
    for auth, expected in [(True, 404), (False, 401)]:
        status, headers, body = request(
            stream_bridge, "stream", {"id": IDENTIFIER}, method="HEAD", auth=auth
        )
        assert status == expected and body == b""
        assert headers["Content-Type"] == "text/xml" and int(headers["Content-Length"]) > 0
    assert stream_bridge.actor.core.downloader.calls == 0


def test_full_download_queue_is_retryable_not_success(stream_bridge):
    slots = stream_bridge.actor.download_slots
    for _ in range(4):
        assert slots.acquire(blocking=False)
    try:
        assert_stream_error(
            request(stream_bridge, "stream", {"id": IDENTIFIER}), 503, 0, "stream-unavailable"
        )
        assert stream_bridge.actor.core.downloader.calls == 0
    finally:
        for _ in range(4):
            slots.release()


@pytest.mark.parametrize("fmt,mime", [("json", "application/json"), ("xml", "application/xml")])
def test_metadata_contract_unchanged(stream_bridge, fmt, mime, monkeypatch):
    for endpoint, params, auth in [("ping", {}, False), ("getSong", {"id": "bad"}, True)]:
        status, headers, body = request(stream_bridge, endpoint, {"f": fmt, **params}, auth=auth)
        assert status == 200 and headers["Content-Type"] == mime
        assert "X-Stream-Error" not in headers
        if fmt == "json":
            assert json.loads(body)["subsonic-response"]["status"] == "failed"
        else:
            assert ET.fromstring(body).attrib["status"] == "failed"

    def fail(*args):
        raise DownloadError("manifest")

    monkeypatch.setattr(stream_bridge.actor, "metadata", fail)
    status, headers, body = request(stream_bridge, "getSong", {"f": fmt, "id": IDENTIFIER})
    assert status == 200 and headers["Content-Type"] == mime
    assert b"Bridge operation unavailable" in body and b"[manifest]" not in body


@pytest.mark.parametrize(
    "body,headers,query,status",
    [
        (b"", {"Content-Length": "bad"}, "", 400),
        (b"", {"Content-Length": "-1"}, "", 413),
        (b"", {"Content-Length": "8193"}, "", 413),
        (b"", {"Content-Type": "application/json"}, "", 415),
        (b"\xff", {}, "", 400),
        (b"", {}, "&id=duplicate", 400),
        (b"", {}, "&" + urlencode({str(i): "x" for i in range(33)}), 400),
    ],
)
def test_malformed_stream_requests_are_xml(stream_bridge, body, headers, query, status):
    connection = http.client.HTTPConnection("127.0.0.1", stream_bridge.server_port, timeout=5)
    query = (
        urlencode({"v": "1.16.1", "c": "fixture", "f": "json", "apiKey": KEY, "id": IDENTIFIER})
        + query
    )
    try:
        connection.request(
            "POST",
            "/rest/stream.view?" + query,
            body,
            {"Content-Type": "application/x-www-form-urlencoded", **headers},
        )
        response = connection.getresponse()
        assert_stream_error(
            (response.status, dict(response.getheaders()), response.read()),
            status,
            10,
            "invalid-request",
        )
    finally:
        connection.close()


def test_exhausted_format_fallback_returns_xml(stream_bridge, format_downloader):
    binary, configure, attempts = format_downloader
    configure(
        {TRACK.id: {quality: {"stdout": NO_CODEC} for quality in ("atmos", "lossless", "aac")}}
    )
    stream_bridge.actor.core.downloader = Downloader(binary, stream_bridge.actor.core.cache.staging)
    assert_stream_error(
        request(stream_bridge, "stream", {"id": IDENTIFIER}), 503, 0, "format-unavailable"
    )
    assert attempts() == [[TRACK.id, quality] for quality in ("atmos", "lossless", "aac")]
    assert not list(stream_bridge.actor.core.cache.staging.iterdir())


@pytest.mark.parametrize("failure", ["seek", "read", "write", "headers", "close", "release"])
def test_after_response_starts_close_without_appending_error(tmp_path, failure):
    path = tmp_path / "audio"
    content = b"\x00\x00\x00\x18ftypM4A " + b"fixture audio" * 3
    path.write_bytes(content)
    source = path.open("rb")
    released, responses = [], []

    class File:
        def fileno(self):
            return source.fileno()

        def read(self, count):
            if failure == "read" and count != 12:
                raise OSError(PRIVATE)
            return source.read(count)

        def seek(self, offset):
            if failure == "seek":
                raise ValueError(PRIVATE)
            return source.seek(offset)

        def close(self):
            source.close()
            if failure == "close":
                raise OSError(PRIVATE)

    class Writer(io.BytesIO):
        writes = 0

        def write(self, data):
            self.writes += 1
            if (failure == "write" and self.writes == 2) or failure == "headers":
                raise OSError(PRIVATE)
            return super().write(data)

    def release(*args):
        released.append(args)
        if failure == "release":
            raise RuntimeError(PRIVATE)

    handler = Handler.__new__(Handler)
    handler.server = SimpleNamespace(
        actor=SimpleNamespace(
            acquire=lambda *args: ("key", File(), len(content), Media("alac")),
            call=release,
            cancel=threading.Event(),
        ),
        hosts={"localhost"},
        credentials={"username": "bridge", "key": KEY},
        quality="atmos",
    )
    handler.path = "/rest/stream?" + urlencode(
        {"id": IDENTIFIER, "apiKey": KEY, "f": "json", "v": "1.16.1", "c": "fixture"}
    )
    handler.headers = {"Host": "localhost"}
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.wfile = Writer()

    def send_response(code, message=None):
        responses.append(code)
        Handler.send_response(handler, code, message)

    handler.send_response = send_response
    handler.handle_api()
    output = handler.wfile.getvalue()
    assert responses == [200] and output.count(b"HTTP/1.1") <= 1
    assert b"subsonic-response" not in output and PRIVATE.encode() not in output
    assert released == [("release", "key")] and source.closed
    assert handler.close_connection
    assert handler.wfile.writes <= 2
