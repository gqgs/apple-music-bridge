"""Stage labels follow exact wrappers in pinned amdl 452ba0b, not error payloads."""

import threading

import pytest
from test_bridge import TRACK, request
from test_format_fallback import NO_CODEC
from test_format_fallback import format_downloader as format_downloader
from test_stream_errors import IDENTIFIER, assert_stream_error
from test_stream_errors import stream_bridge as stream_bridge

from apple_music_player.downloader import Downloader, DownloadError, FormatUnavailable, Summary

SECRET = "PRIVATE_PAYLOAD"
FAIRPLAY = "Failed to run v4: "
AAC = "Failed to dl aac-lc via lite-server: "

# Each prefix or exact message comes from the pinned route named in the outer
# app/rip.go line. Suffixes represent untrusted URLs, tokens, bodies or key data.
STAGES = [
    (FAIRPLAY + SECRET, "download-fairplay"),
    (FAIRPLAY + "m3u8 not of media type", "fairplay-playlist"),
    (FAIRPLAY + "no segments extracted from playlist", "fairplay-playlist"),
    (FAIRPLAY + "non-byterange playlists are currently unsupported", "fairplay-playlist"),
    (FAIRPLAY + "no init segment found", "fairplay-container"),
    (FAIRPLAY + "segment number out of sync", "fairplay-container"),
    (FAIRPLAY + "unexpected box type html, should be ftyp or moov", "fairplay-container"),
    (FAIRPLAY + "read fragment: " + SECRET, "fairplay-read"),
    (FAIRPLAY + "decryptFragment seq 12: " + SECRET, "fairplay-decrypt"),
    (FAIRPLAY + "encode fragment seq 12 failed: " + SECRET, "fairplay-write"),
    (FAIRPLAY + "lite-server /key returned code=404 msg=" + SECRET, "wrapper-key-api-404"),
    (
        FAIRPLAY + "lite-server /key returned 503 Service Unavailable: " + SECRET,
        "wrapper-key-http-503",
    ),
    (AAC + SECRET, "download-aac"),
    (
        AAC + "lite-server /webplayback returned code=404 msg=" + SECRET,
        "wrapper-webplayback-api-404",
    ),
    (
        AAC + "lite-server /webplayback returned 401 Unauthorized: " + SECRET,
        "wrapper-webplayback-http-401",
    ),
    (AAC + "fetch media playlist: " + SECRET, "aac-playlist-fetch"),
    (AAC + "parse media playlist: " + SECRET, "aac-playlist-parse"),
    (AAC + "not a media playlist", "aac-playlist-parse"),
    (AAC + "no matching key information found", "aac-playlist-parse"),
    (AAC + "no initialization segment found", "aac-playlist-parse"),
    (AAC + "invalid DRM key URI", "aac-playlist-parse"),
    (AAC + "playlist is not encrypted", "aac-playlist-parse"),
    (AAC + "acquire content key: " + SECRET, "aac-license"),
    (AAC + "acquire content key: decrypt license keys: " + SECRET, "aac-license"),
    (
        AAC + "acquire content key: empty license in lite-server response",
        "wrapper-license-invalid-response",
    ),
    (
        AAC + "acquire content key: failed to decode license: " + SECRET,
        "wrapper-license-invalid-response",
    ),
    (
        AAC + "acquire content key: lite-server /license returned code=500 msg=" + SECRET,
        "wrapper-license-api-500",
    ),
    (
        AAC + "acquire content key: lite-server /license returned 403 Forbidden: " + SECRET,
        "wrapper-license-http-403",
    ),
    (AAC + "create media request: " + SECRET, "aac-media"),
    (AAC + "download media: " + SECRET, "aac-media"),
    (AAC + "failed to decode file: " + SECRET, "aac-container"),
    (AAC + "file is not fragmented", "aac-container"),
    (AAC + "no init part of file", "aac-container"),
    (AAC + "styp inside an incomplete fragment", "aac-container"),
    (AAC + "mdat without preceding moof", "aac-container"),
    (AAC + "truncated fragment at end of file", "aac-container"),
    (AAC + "failed to decrypt init: " + SECRET, "aac-decrypt"),
    (AAC + "failed to decrypt segment: " + SECRET, "aac-decrypt"),
    (AAC + "failed to write init: " + SECRET, "aac-write"),
    (AAC + "failed to encode segment: " + SECRET, "aac-write"),
    (AAC + "failed to write output: " + SECRET, "aac-write"),
    (AAC + "create temporary output: " + SECRET, "aac-write"),
    (AAC + "close temporary output: " + SECRET, "aac-write"),
    (AAC + "replace output: " + SECRET, "aac-write"),
]


@pytest.mark.parametrize("line,category", STAGES)
@pytest.mark.parametrize("accept_paths", [True, False])
def test_source_wrappers_are_fragmented_and_private(line, category, accept_paths):
    summary = Summary(accept_paths=accept_paths)
    data = ("Queue 1 of 1: Song->" + line + "\n").encode()
    for offset in range(0, len(data), 7):
        summary.feed(data[offset : offset + 7])
    assert summary.error == category
    assert summary.buffer == b"" and SECRET not in repr(vars(summary))


@pytest.mark.parametrize(
    "outer,endpoint,method",
    [
        (FAIRPLAY, "key", "Get"),
        (AAC, "webplayback", "Get"),
        (AAC + "acquire content key: ", "license", "Post"),
    ],
)
@pytest.mark.parametrize(
    "detail,kind",
    [
        ("dial tcp 127.0.0.1:12340: connect: connection refused", "refused"),
        ("context deadline exceeded (Client.Timeout exceeded while awaiting headers)", "timeout"),
        ("read tcp 127.0.0.1:12345->127.0.0.1:12340: i/o timeout", "timeout"),
        (SECRET + " context deadline exceeded", "unknown"),
        ("context deadline exceeded " + SECRET, "unknown"),
        (SECRET + " i/o timeout", "unknown"),
    ],
)
def test_only_fixed_loopback_transport_errors_are_classified(outer, endpoint, method, detail, kind):
    summary = Summary()
    summary.feed(
        (
            outer + f'{method} "http://127.0.0.1:12340/{endpoint}?token={SECRET}": {detail}\n'
        ).encode()
    )
    assert summary.error == f"wrapper-{endpoint}-{kind}"
    assert SECRET not in repr(vars(summary))


@pytest.mark.parametrize(
    "line,category",
    [
        (
            FAIRPLAY + "unexpected box type " + SECRET + ", should be ftyp or moov",
            "download-fairplay",
        ),
        (FAIRPLAY + "m3u8 not of media type " + SECRET, "download-fairplay"),
        (FAIRPLAY + "decryptFragment seq 1 " + SECRET, "download-fairplay"),
        (FAIRPLAY + "decryptFragment seq -1: " + SECRET, "download-fairplay"),
        (FAIRPLAY + "decryptFragment seq 12345678901234567890: " + SECRET, "download-fairplay"),
        (
            FAIRPLAY + 'Get "https://cdn.example/?token=' + SECRET + '": 404 Not Found',
            "download-fairplay",
        ),
        (FAIRPLAY + "read fragment: 404 Not Found " + SECRET, "fairplay-read"),
        (FAIRPLAY + "lite-server /license returned code=404 msg=" + SECRET, "download-fairplay"),
        (FAIRPLAY + "lite-server /key returned code=4040 msg=" + SECRET, "wrapper-key-unknown"),
        (
            FAIRPLAY + 'Get "http://127.0.0.1:12340/key-extra": context deadline exceeded',
            "download-fairplay",
        ),
        (
            FAIRPLAY + 'Get "http://127.0.0.1:9999/key": context deadline exceeded',
            "download-fairplay",
        ),
        (
            FAIRPLAY + 'Post "http://127.0.0.1:12340/key": context deadline exceeded',
            "download-fairplay",
        ),
        (AAC + "Unavailable " + SECRET, "download-aac"),
        (AAC + "not a media playlist " + SECRET, "download-aac"),
        (
            AAC + "fetch media playlist: " + SECRET + "lite-server /license returned code=401",
            "aac-playlist-fetch",
        ),
        (
            AAC + "acquire content key: " + SECRET + "lite-server /license returned code=401",
            "aac-license",
        ),
        (
            AAC
            + "acquire content key: acquire content key: lite-server /license returned code=401",
            "aac-license",
        ),
        (AAC + "lite-server /license returned code=401", "download-aac"),
        (
            AAC + "lite-server /webplayback returned code=500 "
            "msg=lite-server /license returned code=401 " + SECRET,
            "wrapper-webplayback-api-500",
        ),
        (
            AAC
            + "acquire content key: lite-server /license returned code=500 msg=Unavailable "
            + SECRET,
            "wrapper-license-api-500",
        ),
        (
            AAC + 'acquire content key: Get "http://127.0.0.1:12340/license": '
            "context deadline exceeded",
            "aac-license",
        ),
        ('{"error":"' + AAC + 'lite-server /webplayback returned code=404"}', None),
        (SECRET + FAIRPLAY + "read fragment: 404", None),
    ],
)
def test_unknown_suffixes_and_false_positives_do_not_invent_causes(line, category):
    summary = Summary()
    summary.feed((line + "\n").encode())
    assert summary.error == category
    assert SECRET not in repr(vars(summary))


@pytest.mark.parametrize("known_first", [True, False])
def test_specific_diagnostic_survives_generic_route_and_format_miss(known_first):
    specific = AAC + "lite-server /webplayback returned code=404 msg=" + SECRET
    generic = AAC + SECRET
    lines = [specific, generic] if known_first else [generic, specific]
    summary = Summary()
    summary.feed((NO_CODEC + "\n" + "\n".join(lines) + "\nUnavailable\n").encode())
    assert summary.error == "wrapper-webplayback-api-404"


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize(
    "line,category",
    [STAGES[i] for i in (0, 3, 7, 8, 9, 10, 12, 13, 15, 22, 26, 29, 30, 36, 38)],
)
def test_known_stage_failure_never_downgrades(
    tmp_path, format_downloader, pipe, exit_code, line, category
):
    binary, configure, attempts = format_downloader
    other = "stderr" if pipe == "stdout" else "stdout"
    # A format miss on either pipe must not hide a later hard stage failure.
    configure({TRACK.id: {"lossless": {pipe: line, other: NO_CODEC, "exit": exit_code}}})
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(DownloadError) as caught:
        Downloader(binary, stage).run(TRACK, "lossless", threading.Event())
    error = caught.value
    assert not isinstance(error, FormatUnavailable)
    assert error.category == category and error.exit_code is None
    assert SECRET not in str(error)
    assert attempts() == [[TRACK.id, "lossless"]]
    assert not list(stage.iterdir())


@pytest.mark.parametrize("specific_pipe", ["stdout", "stderr"])
def test_specific_endpoint_beats_generic_route_on_other_pipe(
    tmp_path, format_downloader, specific_pipe
):
    binary, configure, attempts = format_downloader
    other = "stderr" if specific_pipe == "stdout" else "stdout"
    configure(
        {
            TRACK.id: {
                "lossless": {
                    specific_pipe: AAC
                    + "acquire content key: lite-server /license returned code=403 msg="
                    + SECRET,
                    other: AAC + SECRET,
                }
            }
        }
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(DownloadError) as caught:
        Downloader(binary, stage).run(TRACK, "lossless", threading.Event())
    assert caught.value.category == "wrapper-license-api-403"
    assert attempts() == [[TRACK.id, "lossless"]]


@pytest.mark.parametrize(
    "line,category",
    [
        (FAIRPLAY + "read fragment: " + SECRET, "fairplay-read"),
        (
            AAC + "lite-server /webplayback returned code=404 msg=" + SECRET,
            "wrapper-webplayback-api-404",
        ),
        (
            AAC + "acquire content key: lite-server /license returned code=401 msg=" + SECRET,
            "wrapper-license-api-401",
        ),
    ],
)
def test_nested_stage_reaches_http_safely(stream_bridge, format_downloader, line, category):
    binary, configure, attempts = format_downloader
    configure({TRACK.id: {"atmos": {"stderr": line, "exit": 1}}})
    stream_bridge.actor.core.downloader = Downloader(binary, stream_bridge.actor.core.cache.staging)
    message = assert_stream_error(
        request(stream_bridge, "stream", {"id": IDENTIFIER}), 503, 0, category
    )
    assert "(exit " not in message and SECRET not in message
    if category == "wrapper-webplayback-api-404":
        assert "does not distinguish" in message
    assert attempts() == [[TRACK.id, "atmos"]]


def test_oversized_diagnostic_line_is_discarded():
    summary = Summary()
    summary.feed((AAC + "acquire content key: " + SECRET * 10000 + "\n").encode())
    assert summary.error is None and summary.buffer == b"" and not summary.dropping
    assert SECRET not in repr(vars(summary))


@pytest.mark.parametrize("transcript,category", [("", "no-result"), (NO_CODEC, "process-exit")])
def test_existing_process_exit_metadata_is_preserved(
    stream_bridge, format_downloader, transcript, category
):
    binary, configure, attempts = format_downloader
    configure({TRACK.id: {"atmos": {"stdout": transcript, "exit": 7}}})
    stream_bridge.actor.core.downloader = Downloader(binary, stream_bridge.actor.core.cache.staging)
    message = assert_stream_error(
        request(stream_bridge, "stream", {"id": IDENTIFIER}), 503, 0, category
    )
    assert "(exit 7)" in message
    assert attempts() == [[TRACK.id, "atmos"]]
