"""Per-track preference fallback using pinned amdl transcripts and synthetic audio."""

import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_bridge import KEY, TRACK, FakeCatalog, payload, request

from apple_music_player.bridge.core import Actor
from apple_music_player.bridge.http import ProtocolCore, Server
from apple_music_player.downloader import Downloader, FormatUnavailable, Summary
from apple_music_player.model import PlayerError

NO_CODEC = "⚠ Failed to extract info from manifest: no codec found"


@pytest.fixture
def format_downloader(tmp_path, audio, monkeypatch):
    log = tmp_path / "attempts.jsonl"
    monkeypatch.setenv("FORMAT_ATTEMPTS", str(log))
    monkeypatch.setenv("FORMAT_AUDIO", json.dumps({key: str(path) for key, path in audio.items()}))
    binary = tmp_path / "amdl-formats"
    binary.write_text("""#!/usr/bin/env python3
import json, os, pathlib, shutil, sys, time
quality = "atmos" if "--atmos" in sys.argv else "aac" if "--aac" in sys.argv else "lossless"
track = sys.argv[-1].rsplit("/", 1)[-1]
job = pathlib.Path.cwd()
# A failed or discarded attempt must not keep staging files alive.
assert len(list(job.parent.iterdir())) == 1
with open(os.environ["FORMAT_ATTEMPTS"], "a") as log:
    log.write(json.dumps([track, quality]) + "\\n")
config = json.loads((job / "config.yaml").read_text())
assert config["media"]["aac-type"] == "aac-lc"
assert not config["convert"]["after-download"]
action = json.loads(os.environ["FORMAT_PLAN"])[track][quality]
time.sleep(action.get("delay", 0))
print("PRIVATE_PAYLOAD", flush=True)
if "stdout" in action or "stderr" in action:
    (job / "output" / "partial").write_bytes(b"partial media")
    print(action.get("stdout", ""), flush=True)
    print(action.get("stderr", ""), file=sys.stderr, flush=True)
    print("null")
    sys.exit(action.get("exit", 0))
source = json.loads(os.environ["FORMAT_AUDIO"])[action["codec"]]
output = job / "output" / "track"
shutil.copyfile(source, output)
print(json.dumps([{"path": str(output)}]))
""")
    binary.chmod(0o700)

    def configure(plan):
        monkeypatch.setenv("FORMAT_PLAN", json.dumps(plan))

    def attempts():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return str(binary), configure, attempts


@pytest.mark.parametrize(
    ("quality", "plan", "expected", "codec"),
    [
        ("atmos", {"atmos": {"codec": "eac3"}}, ["atmos"], "eac3"),
        (
            "atmos",
            {"atmos": {"stdout": NO_CODEC}, "lossless": {"codec": "alac"}},
            ["atmos", "lossless"],
            "alac",
        ),
        (
            "atmos",
            {
                "atmos": {"stdout": "Unavailable"},
                "lossless": {"stdout": NO_CODEC},
                "aac": {"codec": "aac"},
            },
            ["atmos", "lossless", "aac"],
            "aac",
        ),
        (
            "atmos",
            {"atmos": {"codec": "ac3"}, "lossless": {"codec": "alac"}},
            ["atmos", "lossless"],
            "alac",
        ),
        ("lossless", {"lossless": {"codec": "alac"}}, ["lossless"], "alac"),
        (
            "lossless",
            {"lossless": {"stdout": NO_CODEC}, "aac": {"codec": "aac"}},
            ["lossless", "aac"],
            "aac",
        ),
        # amdl itself falls back directly when there is no enhanced HLS URL.
        ("lossless", {"lossless": {"codec": "aac"}}, ["lossless"], "aac"),
    ],
)
def test_download_preference_order(tmp_path, format_downloader, quality, plan, expected, codec):
    binary, configure, attempts = format_downloader
    configure({TRACK.id: plan})
    stage = tmp_path / "stage"
    stage.mkdir()
    job, source, media = Downloader(binary, stage).run(TRACK, quality, threading.Event())
    try:
        assert attempts() == [[TRACK.id, value] for value in expected]
        assert source.is_file() and media.codec == codec
        assert not media.atmos  # A request or E-AC-3 codec alone never proves JOC.
        assert list(stage.iterdir()) == [job]
        assert not (job / "config.yaml").exists()
    finally:
        shutil.rmtree(job)


@pytest.mark.parametrize(
    "failure",
    [
        {"stdout": "Failed to rip song: 401 Unauthorized"},
        {"stdout": "Failed to rip song: 403 Forbidden"},
        {"stdout": "Failed to rip song: 429 Too Many Requests"},
        {"stdout": "⚠ Failed to extract info from manifest: 503 Service Unavailable"},
        {"stdout": "⚠ Failed to extract info from manifest: m3u8 not of master type"},
        {"stdout": "Failed to run v4: PRIVATE_PAYLOAD"},
        {"stdout": "Failed to dl aac-lc via lite-server: PRIVATE_PAYLOAD"},
        {"stdout": "Defragment failed: PRIVATE_PAYLOAD"},
        {"stdout": "proxy config error: PRIVATE_PAYLOAD"},
        {"stdout": ""},
        {"stdout": NO_CODEC, "exit": 1},
        {"stdout": "Unavailable", "exit": 2},
        {"stdout": NO_CODEC, "stderr": "Failed to rip song: 401 Unauthorized"},
        {"stdout": NO_CODEC + "\nFailed to rip song: 401 Unauthorized"},
        {"stdout": "Failed to rip song: 401 Unauthorized\n" + NO_CODEC},
        {
            "stdout": NO_CODEC,
            "stderr": "Error connecting to lite-server: "
            "lite-server /m3u8 returned code=404 msg=PRIVATE_PAYLOAD",
        },
    ],
)
def test_non_format_failures_do_not_downgrade(tmp_path, format_downloader, failure):
    binary, configure, attempts = format_downloader
    configure({TRACK.id: {"atmos": failure}})
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(PlayerError) as error:
        Downloader(binary, stage).run(TRACK, "atmos", threading.Event())
    assert not isinstance(error.value, FormatUnavailable)
    assert "PRIVATE_PAYLOAD" not in str(error.value)
    assert attempts() == [[TRACK.id, "atmos"]]
    assert not list(stage.iterdir())


def test_all_formats_unavailable_is_bounded(tmp_path, format_downloader):
    binary, configure, attempts = format_downloader
    configure(
        {
            TRACK.id: {
                "atmos": {"stdout": "Unavailable"},
                "lossless": {"stdout": NO_CODEC},
                "aac": {"stdout": "Failed to dl aac-lc via lite-server: Unavailable"},
            }
        }
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(FormatUnavailable, match=r"\[unavailable\]"):
        Downloader(binary, stage).run(TRACK, "atmos", threading.Event())
    assert attempts() == [[TRACK.id, quality] for quality in ("atmos", "lossless", "aac")]
    assert not list(stage.iterdir())


def test_cancel_between_attempts_does_not_start_another_process(tmp_path, format_downloader):
    binary, configure, attempts = format_downloader
    configure({TRACK.id: {"atmos": {"stdout": NO_CODEC}}})
    stage = tmp_path / "stage"
    stage.mkdir()
    cancel = threading.Event()

    class CancelAfterMiss(Downloader):
        def _run(self, *args):
            try:
                return super()._run(*args)
            except FormatUnavailable:
                cancel.set()
                raise

    with pytest.raises(PlayerError, match="cancelled"):
        CancelAfterMiss(binary, stage).run(TRACK, "atmos", cancel)
    assert attempts() == [[TRACK.id, "atmos"]]
    assert not list(stage.iterdir())


def test_attempts_share_one_deadline(tmp_path, format_downloader):
    binary, configure, attempts = format_downloader
    configure({TRACK.id: {"atmos": {"stdout": NO_CODEC}, "lossless": {"codec": "alac"}}})
    stage = tmp_path / "stage"
    stage.mkdir()
    deadlines = []

    class Recorded(Downloader):
        def _run(self, track, quality, cancel, deadline):
            deadlines.append(deadline)
            # Deterministically exhaust the same request budget before attempt 2.
            return super()._run(track, quality, cancel, deadline if quality == "atmos" else 0)

    with pytest.raises(PlayerError, match="timed out"):
        Recorded(binary, stage).run(TRACK, "atmos", threading.Event())
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert attempts() == [[TRACK.id, "atmos"]]
    assert not list(stage.iterdir())


def test_exact_missing_codec_transcript_remains_private():
    summary = Summary()
    transcript = ("Queue 1 of 1: Song->" + NO_CODEC + "\n").encode()
    for offset in range(0, len(transcript), 7):
        summary.feed(transcript[offset : offset + 7])
    assert summary.error == "format-unavailable"
    assert summary.buffer == b""


def test_mixed_playlist_http_cache_and_restart(tmp_path, format_downloader, audio):
    binary, configure, attempts = format_downloader
    configure(
        {
            "3": {"atmos": {"stdout": NO_CODEC}, "lossless": {"codec": "alac"}},
            "4": {"atmos": {"codec": "eac3"}},
            "5": {
                "atmos": {"stdout": "Unavailable"},
                "lossless": {"stdout": NO_CODEC},
                "aac": {"codec": "aac"},
            },
        }
    )
    root = tmp_path / "cache"
    actor = Actor(
        lambda cancel: ProtocolCore(
            root,
            1024**2,
            quality="atmos",
            catalog=FakeCatalog(),
            downloader_factory=lambda stage: Downloader(binary, stage),
            cancel=cancel,
        )
    )
    tracks = [replace(TRACK, id=identifier) for identifier in ("3", "4", "5")]
    actor.call("remember", tracks)
    # All client identities keep the same preference.
    identifiers = [actor.call("identity", track) for track in tracks]
    server = Server(actor, {"username": "bridge", "key": KEY}, port=0, quality="atmos")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        with ThreadPoolExecutor(3) as pool:
            results = list(
                pool.map(lambda _: request(server, "stream", {"id": identifiers[0]}), range(3))
            )
        assert all(
            result[0] == 200 and result[2] == audio["alac"].read_bytes() for result in results
        )
        assert attempts() == [["3", "atmos"], ["3", "lossless"]]
        for identifier, codec, fallback in zip(
            identifiers, ("alac", "eac3", "aac"), ("true", "false", "true"), strict=True
        ):
            result = request(server, "stream", {"id": identifier})
            assert result[0] == 200 and result[2] == audio[codec].read_bytes()
            assert result[1]["X-Media-Codec"] == codec
            assert result[1]["X-Media-Atmos"] == "unverified"
            assert result[1]["X-Requested-Quality"] == "atmos"
            assert result[1]["X-Quality-Fallback"] == fallback
            assert result[1]["Content-Type"] == ("audio/eac3" if codec == "eac3" else "audio/mp4")
            ranged = request(server, "stream", {"id": identifier}, headers={"Range": "bytes=0-15"})
            assert ranged[0] == 206 and ranged[2] == audio[codec].read_bytes()[:16]
            song = payload(server, "getSong", {"id": identifier})["song"]
            assert song["samplingRate"] > 0 and song["channelCount"] > 0
        assert attempts() == [
            ["3", "atmos"],
            ["3", "lossless"],
            ["4", "atmos"],
            ["5", "atmos"],
            ["5", "lossless"],
            ["5", "aac"],
        ]
        assert actor.core.quality == "atmos"
    finally:
        actor.cancel.set()
        server.shutdown()
        server.server_close()
        thread.join()
        actor.close()
    # Cached fallback results remain valid without an authenticated downloader.
    core = ProtocolCore(root, 1024**2, quality="atmos", catalog=FakeCatalog())
    try:
        for identifier, codec in zip(identifiers, ("alac", "eac3", "aac"), strict=True):
            key, file, _, media = core.acquire(identifier)
            assert media.codec == codec
            file.close()
            core.release(key)
        assert not core.pins and not list(core.cache.staging.iterdir())
    finally:
        core.close()
