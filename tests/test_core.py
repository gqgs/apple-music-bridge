import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import wait_until

from apple_music_player.cache import Cache, safe_path
from apple_music_player.controller import Controller
from apple_music_player.downloader import Downloader, Summary, probe
from apple_music_player.model import Media, PlayerError, Track, parse_url
from apple_music_player.playback import Mpv

TRACK = Track("us", "12345", "Synthetic test track")


def add_file(cache, track, content=b"abcd", quality="lossless"):
    job = cache.staging / "job-test"
    job.mkdir(exist_ok=True)
    source = job / "audio"
    source.write_bytes(content)
    cache.put(track.key(quality), source, Media("alac", 44100, 16, 2))
    return track.key(quality)


def test_cache_lru_protection_variants_restart(tmp_path):
    cache = Cache(tmp_path / "cache", 8)
    first = add_file(cache, TRACK)
    second = add_file(cache, Track("us", "2", "second"))
    cache.touch(first)
    third = add_file(cache, Track("us", "3", "third"))
    assert cache.get(first) and cache.get(third) and cache.get(second) is None
    cache.protected = {first, third}
    with pytest.raises(PlayerError, match="queued"):
        add_file(cache, TRACK, quality="atmos")
    cache.protected = {first}
    variant = add_file(cache, TRACK, quality="atmos")
    assert variant != first and cache.get(variant) and cache.get(first)
    cache.close()
    cache = Cache(tmp_path / "cache", 8)
    assert cache.get(first) and cache.get(variant)
    assert not list(cache.staging.iterdir())
    cache.close()


def test_cache_recovery_and_oversize(tmp_path):
    cache = Cache(tmp_path / "cache", 8)
    key = add_file(cache, TRACK)
    cache.path(key).unlink()
    orphan = cache.path("a" * 64)
    orphan.write_bytes(b"orphan")
    cache.reconcile()
    assert cache.get(key) is None and not orphan.exists()
    with pytest.raises(PlayerError, match="exceeds"):
        add_file(cache, TRACK, b"a" * 9)
    assert cache.usage == 0
    cache.close()


def test_cache_lock_and_paths(tmp_path):
    cache = Cache(tmp_path / "cache", 8)
    with pytest.raises(PlayerError, match="Another"):
        Cache(cache.root, 8)
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    link = cache.media / ("b" * 64)
    link.symlink_to(outside)
    with pytest.raises(PlayerError, match="symlink"):
        cache.path(link.name)
    with pytest.raises(PlayerError):
        safe_path(cache.root, cache.root / "media" / ".." / "escape")
    with pytest.raises(PlayerError):
        safe_path(cache.root, outside)
    with pytest.raises(PlayerError):
        cache.path("../../outside")
    cache.close()
    assert outside.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "codec,label",
    [("alac", "Lossless"), ("aac", "Lossy"), ("ac3", "Lossy"), ("eac3", "Atmos unverified")],
)
def test_actual_synthetic_media(audio, codec, label):
    media = probe(audio[codec])
    assert media.codec == codec and label in media.label
    assert not media.atmos
    if codec == "ac3":
        assert "fallback" in media.warning("atmos")
    if codec == "aac":
        assert "fallback" in media.warning("lossless")


def test_joc_profile_not_tags():
    stream = {
        "codec_type": "audio",
        "codec_name": "eac3",
        "profile": "Dolby Digital Plus + Dolby Atmos",
    }
    assert "Dolby Atmos (JOC detected)" in Media.from_probe({"streams": [stream]}).label
    stream.pop("profile")
    stream["tags"] = {"title": "Dolby Atmos"}
    assert not Media.from_probe({"streams": [stream]}).atmos
    assert "24-bit" in Media("alac", 96000, 24, 2).label


def test_summary_discard_and_bound():
    summary = Summary()
    summary.feed(b"token=TEST_SECRET\n" + b"x" * 100000)
    assert len(summary.buffer) <= 65536
    summary.feed(b'\n[{"path":"/safe/file", "song":"TEST_SECRET"}]\n')
    assert summary.path == "/safe/file"
    assert "TEST_SECRET" not in repr(vars(summary))
    summary.feed(b'[{"path":"one"}, {"path":"two"}]\n')
    assert summary.path == "/safe/file"


@pytest.mark.parametrize("mode", ["zero", "malformed", "escape", "symlink"])
def test_downloader_rejects_invalid_results(tmp_path, fake_downloader, monkeypatch, mode):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setenv("TEST_MODE", mode)
    with pytest.raises(PlayerError) as error:
        Downloader(fake_downloader, staging).run(TRACK, "lossless", threading.Event())
    assert "TEST_SECRET" not in str(error.value)
    assert not list(staging.iterdir())


def test_download_private_config_and_probe(tmp_path, fake_downloader, capsys):
    staging = tmp_path / "staging"
    staging.mkdir()
    job, source, media = Downloader(fake_downloader, staging).run(
        TRACK, "lossless", threading.Event()
    )
    assert media.codec == "alac" and source.exists()
    assert not (job / "config.yaml").exists()
    assert "TEST_SECRET" not in str(capsys.readouterr())
    shutil.rmtree(job)


def test_cancel_process_group(tmp_path, fake_downloader, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("TEST_MODE", "hang")
    monkeypatch.setenv("TEST_PID", str(pidfile))
    cancel = threading.Event()
    with ThreadPoolExecutor() as pool:
        future = pool.submit(Downloader(fake_downloader, staging).run, TRACK, "lossless", cancel)
        wait_until(pidfile.exists)
        child = int(pidfile.read_text())
        cancel.set()
        with pytest.raises(PlayerError, match="cancelled"):
            future.result(timeout=5)
    stat = Path(f"/proc/{child}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"
    assert not list(staging.iterdir())


def test_disk_and_stage_limits(tmp_path, fake_downloader, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(PlayerError, match="staging"):
        Downloader(fake_downloader, staging, stage_limit=1).run(
            TRACK, "lossless", threading.Event()
        )
    monkeypatch.setattr(shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(100, 99, 1))
    with pytest.raises(PlayerError, match="disk"):
        Downloader(fake_downloader, staging).run(TRACK, "lossless", threading.Event())
    assert not list(staging.iterdir())


def test_url_canonicalization():
    assert parse_url("https://music.apple.com/us/album/title/100?i=123") == ("us", "song", "123")
    assert parse_url("https://music.apple.com/gb/album/title/100") == ("gb", "album", "100")
    for value in [
        "file:///tmp/a",
        "https://evil.com/us/song/123",
        "https://music.apple.com/us/artist/123",
    ]:
        with pytest.raises(PlayerError):
            parse_url(value)


def test_queue_download_play_dedup_prefetch(tmp_path, fake_downloader, fake_mpv):
    cache = Cache(tmp_path / "cache", 1_000_000)
    player = Mpv(fake_mpv)
    c = Controller(cache, player, fake_downloader)
    from types import SimpleNamespace

    c.runtime = SimpleNamespace(ready=True, store=SimpleNamespace(downloader=fake_downloader))
    c.add(TRACK, "lossless")
    c.add(TRACK, "lossless")
    c.add(Track("us", "2", "Next"), "lossless")
    c.add(Track("us", "3", "Later"), "lossless")
    assert len(c.queue) == 3

    def ready():
        c.tick()
        return c.queue[0].state == "Playing" and c.queue[1].state == "Ready"

    try:
        wait_until(ready)
        assert c.media.codec == "alac" and c.queue[2].state == "Queued"
        assert "PCM" in player.output_label
        player.command("cycle", "pause")
        wait_until(lambda: (c.tick(), c.queue[0].state == "Paused")[1])
        player.command("seek", 0.5, "absolute")
        wait_until(lambda: (c.tick(), player.properties.get("time-pos") == 0.5)[1])
        c.next()
        wait_until(lambda: (c.tick(), c.current is c.queue[0])[1])
    finally:
        c.close()


def test_passthrough_fallback_stops(tmp_path, audio, fake_mpv):
    player = Mpv(fake_mpv)
    with pytest.raises(PlayerError, match="HDMI"):
        player.start(audio["eac3"], Media("eac3"), "auto")
    player.start(audio["eac3"], Media("eac3"), "alsa/hdmi:CARD=TEST,DEV=0")
    try:
        with pytest.raises(PlayerError, match="fell back"):
            wait_until(lambda: (player.poll(), player.output_verified)[1])
    finally:
        player.close()


def test_passthrough_truthful_label(audio, fake_mpv, monkeypatch):
    monkeypatch.setenv("TEST_MPV_FORMAT", "spdif-eac3")
    player = Mpv(fake_mpv)
    player.start(audio["eac3"], Media("eac3"), "alsa/hdmi:CARD=TEST,DEV=0")
    try:
        wait_until(lambda: (player.poll(), player.output_verified)[1])
        assert "receiver Atmos indication not verified" in player.output_label
    finally:
        player.close()


def test_store_disk_full_keeps_existing_cache(tmp_path, monkeypatch):
    import errno
    import os

    cache = Cache(tmp_path / "cache", 100)
    key = add_file(cache, TRACK)

    def disk_full(*args):
        raise OSError(errno.ENOSPC, "No space left")

    monkeypatch.setattr(os, "replace", disk_full)
    with pytest.raises(OSError):
        add_file(cache, Track("us", "2", "Next"))
    assert cache.get(key) and cache.usage == 4
    cache.close()


def test_cancel_retry_and_removed_job_not_committed(
    tmp_path, fake_downloader, fake_mpv, monkeypatch
):
    cache = Cache(tmp_path / "cache", 1_000_000)
    controller = Controller(cache, Mpv(fake_mpv), fake_downloader)
    from types import SimpleNamespace

    controller.runtime = SimpleNamespace(
        ready=True, store=SimpleNamespace(downloader=fake_downloader)
    )
    monkeypatch.setenv("TEST_MODE", "hang")
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("TEST_PID", str(pidfile))
    controller.add(TRACK, "lossless")
    controller.tick()
    wait_until(pidfile.exists)
    controller.cancel_download()
    wait_until(lambda: (controller.tick(), controller.future is None)[1])
    assert controller.queue[0].state.startswith("Cancelled")
    assert cache.get(TRACK.key("lossless")) is None
    monkeypatch.setenv("TEST_MODE", "ok")
    controller.retry()
    wait_until(lambda: (controller.tick(), controller.current is not None)[1])
    controller.next()
    assert not controller.queue
    assert not controller.player.process
    controller.close()


@pytest.mark.parametrize("name", ["audio-params", "audio-out-params"])
@pytest.mark.parametrize("paused", [False, True])
def test_output_changes_revoke_and_restore_without_resume(name, paused):
    import json
    import time
    from unittest.mock import Mock

    player = Mpv()
    player.process = Mock()
    player.process.poll.return_value = None
    player.socket = Mock()
    player.socket.recv.side_effect = TimeoutError
    player.started = time.monotonic()
    player.passthrough = True
    player.command = Mock()

    def emit(prop, data):
        player.buffer += (
            json.dumps({"event": "property-change", "name": prop, "data": data}).encode() + b"\n"
        )
        player.poll()

    emit("audio-params", {"format": "spdif-eac3"})
    assert not player.output_verified
    emit("audio-out-params", {"format": "spdif-eac3"})
    assert player.output_verified
    player.command.assert_called_once_with("set_property", "pause", False)
    emit("pause", paused)
    player.command.reset_mock()

    # Valid updates after startup must not override the user's pause state.
    emit(name, {"format": "spdif-eac3", "samplerate": 192000})
    player.command.assert_not_called()
    assert player.properties["pause"] is paused

    emit(name, None)
    assert not player.output_verified
    assert "passthrough active" not in player.output_label
    assert "unavailable" in player.output_label
    player.command.assert_called_once_with("set_property", "pause", True)
    emit("pause", True)
    player.command.reset_mock()

    # Repeated missing updates remain paused; restored params re-enable controls,
    # but only an explicit user action may resume after this interruption.
    emit(name, None)
    emit(name, {"format": "spdif-eac3"})
    assert player.output_verified and player.properties["pause"] is True
    assert "passthrough active" in player.output_label
    player.command.assert_not_called()

    emit("pause", paused)
    with pytest.raises(PlayerError, match="fell back"):
        emit(name, {"format": "s16"})
    assert not player.output_verified
    assert "passthrough active" not in player.output_label
    player.command.assert_called_once_with("set_property", "pause", True)


def test_play_album_replaces_playing_and_cancels_prefetch_latest_wins(
    tmp_path, audio, fake_downloader, fake_mpv, monkeypatch
):
    import shutil
    from types import SimpleNamespace

    cache = Cache(tmp_path / "cache", 1_000_000)
    source = cache.staging / "old.m4a"
    shutil.copyfile(audio["alac"], source)
    cache.put(TRACK.key("lossless"), source, Media("alac", 44100, 16, 1))
    c = Controller(cache, Mpv(fake_mpv))
    c.runtime = SimpleNamespace(ready=True, store=SimpleNamespace(downloader=fake_downloader))
    pidfile = tmp_path / "prefetch-pid"
    monkeypatch.setenv("TEST_PID", str(pidfile))
    monkeypatch.setenv("TEST_MODE", "hang")
    c.add(TRACK, "lossless")
    c.add(Track("us", "20", "Old prefetch"), "lossless")
    try:
        wait_until(lambda: (c.tick(), pidfile.exists() and c.current is not None)[1])
        old_future = c.future
        assert not c.play_album([], "lossless") and c.current is not None
        c.play_album([Track("us", "30", "Superseded")], "lossless")
        latest = [Track("us", "40", "First"), Track("us", "41", "Second")]
        c.play_album(latest + [latest[0]], "lossless")
        assert c.future is old_future and c.cancel.is_set()
        assert c.current is None and [e.track.id for e in c.queue] == ["40", "41"]
        monkeypatch.setenv("TEST_MODE", "ok")
        wait_until(lambda: (c.tick(), c.current is not None and c.current.state == "Playing")[1])
        assert c.current.track.id == "40" and not c.error
        assert cache.get(TRACK.key("lossless"))  # Replacement does not delete existing cache.
        assert cache.get(Track("us", "20", "").key("lossless")) is None
        assert cache.get(Track("us", "30", "").key("lossless")) is None
    finally:
        c.close()
