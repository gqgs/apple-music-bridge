import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import wait_until

from apple_music_player.cache import Cache, safe_path
from apple_music_player.downloader import Downloader, Summary, probe
from apple_music_player.model import Media, PlayerError, Track, parse_url

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
