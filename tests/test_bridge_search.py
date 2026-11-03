import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from test_bridge import ALBUM, ARTIST, TRACK, FakeCatalog, payload, request
from test_bridge import bridge as _bridge

from apple_music_player.bridge.core import Actor, BridgeError
from apple_music_player.bridge.http import ProtocolCore

bridge = _bridge


def test_supersonic_parallel_auxiliary_requests_and_covers_bypass_catalog(bridge, monkeypatch):
    server, _ = bridge
    server.actor.call("remember", [ALBUM, TRACK, ARTIST])
    entered, release = threading.Event(), threading.Event()
    original = server.actor.core.catalog.search

    def slow(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(server.actor.core.catalog, "search", slow)
    with ThreadPoolExecutor(8) as pool:
        search = pool.submit(
            payload,
            server,
            "search3",
            {"query": "Bicep", "artistCount": "16", "albumCount": "16", "songCount": "16"},
        )
        assert entered.wait(1)
        try:
            # SearchAll sends these in parallel; none require Apple/catalog I/O.
            aux = [
                pool.submit(payload, server, method)
                for method in (
                    "getPlaylists",
                    "getGenres",
                    "getInternetRadioStations",
                    "ping",
                    "getMusicFolders",
                )
            ]
            assert all(f.result(timeout=1)["status"] == "ok" for f in aux)
            cover = pool.submit(request, server, "getCoverArt", {"id": "am:br:lossless:song:3"})
            assert cover.result(timeout=1)[1]["X-Artwork-Status"] == "placeholder"
            assert payload(server, "getGenres", auth=False)["error"]["code"] == 40
        finally:
            release.set()
        assert search.result()["status"] == "ok"


def test_busy_requests_return_explicit_responses(bridge):
    server, _ = bridge
    for _ in range(4):
        assert server.metadata_slots.acquire(False)
    try:
        assert (
            payload(server, "search3", {"query": "Bicep"})["error"]["message"]
            == "Catalog busy; retry later"
        )
        assert payload(server, "ping")["status"] == "ok"
    finally:
        for _ in range(4):
            server.metadata_slots.release()
    for _ in range(16):
        assert server.slots.acquire(timeout=1)
    try:
        status, headers, body = request(server, "ping")
        assert status == 503 and headers["Retry-After"] == "1" and body == b""
    finally:
        for _ in range(16):
            server.slots.release()


def test_metadata_deadline_cancels_queued_and_remaining_entity_work(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class SlowCatalog(FakeCatalog):
        def search(self, query, country, entity="song"):
            calls.append((query, entity))
            entered.set()
            assert release.wait(3)
            return super().search(query, country, entity)

    actor = Actor(
        lambda cancel: ProtocolCore(tmp_path / "cache", 1024, catalog=SlowCatalog(), cancel=cancel)
    )
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(actor.metadata, "search3", {"query": "old"}, 0.1)
            assert entered.wait(1)
            for _ in range(3):
                with pytest.raises(BridgeError):
                    actor.metadata("search3", {"query": "queued"}, timeout=0.01)
            with pytest.raises(BridgeError, match="Catalog busy"):
                actor.metadata("search3", {"query": "overflow"}, timeout=0.01)
            with pytest.raises(BridgeError):
                first.result(timeout=1)
            release.set()
        actor.call("dispatch", "ping", {})  # Barrier after the expired running work.
        assert calls == [("old", "musicArtist")]
        assert actor.core.operation_deadline is None
    finally:
        release.set()
        actor.close()


def test_metadata_counts_are_batched_and_invalidated(tmp_path, monkeypatch):
    core = ProtocolCore(tmp_path / "cache", 1024, catalog=FakeCatalog())
    core.remember([ARTIST, ALBUM, TRACK])
    calls = []
    original = core.known

    def known(kind):
        calls.append(kind)
        return original(kind)

    monkeypatch.setattr(core, "known", known)
    try:
        for _ in range(200):
            assert core.describe(ARTIST)["albumCount"] == 1
            assert core.describe(ALBUM)["songCount"] == 1
        assert calls == ["album", "song"]
        core.remember([replace(TRACK, id="4")])
        assert core.describe(ALBUM)["songCount"] == 2
        assert calls == ["album", "song", "album", "song"]
    finally:
        core.close()


def test_three_entity_cold_latency_cache_paging_and_validation(tmp_path, monkeypatch):
    now = [100.0]
    starts = []
    monkeypatch.setattr("apple_music_player.catalog.time.monotonic", lambda: now[0])

    class ClockEvent:
        def is_set(self):
            return False

        def wait(self, seconds):
            now[0] += seconds

    rows = {
        "musicArtist": [
            dict(wrapperType="artist", artistType="Artist", artistId=1, artistName="Bicep")
        ],
        "album": [
            dict(
                wrapperType="collection",
                collectionType="Album",
                collectionId=2,
                artistId=1,
                artistName="Bicep",
                collectionName="Fixture",
            )
        ],
        "song": [
            dict(
                wrapperType="track",
                kind="song",
                trackId=3,
                collectionId=2,
                artistId=1,
                artistName="Bicep",
                trackName="Fixture",
            )
        ],
    }

    def opened(url, timeout):
        entity = parse_qs(urlsplit(url).query)["entity"][0]
        starts.append((entity, now[0]))
        return io.BytesIO(json.dumps({"results": rows[entity]}).encode())

    monkeypatch.setattr("apple_music_player.catalog.urlopen", opened)
    core = ProtocolCore(tmp_path / "cache", 1024, cancel=ClockEvent())
    try:
        params = {"query": " Bicep ", "artistCount": "16", "albumCount": "16", "songCount": "16"}
        result = core.dispatch("search3", params)["searchResult3"]
        assert starts == [("musicArtist", 100), ("album", 103), ("song", 106)]
        assert all(len(result[k]) == 1 for k in ("artist", "album", "song"))
        assert core.dispatch("search3", params)["searchResult3"] == result
        assert len(starts) == 3
        assert (
            core.dispatch("search3", dict(params, artistOffset="1"))["searchResult3"]["artist"]
            == []
        )
        with pytest.raises(BridgeError):
            core.dispatch("search3", dict(params, query="new", songCount="invalid"))
        assert len(starts) == 3  # Validate all numeric fields before any network call.
        core.operation_deadline = now[0] + 2
        with pytest.raises(BridgeError):
            core.dispatch("search3", dict(params, query="new"))
        assert len(starts) == 3  # Deadline never bypasses catalog spacing.
    finally:
        core.close()


def test_slow_download_does_not_hold_catalog_worker(bridge, monkeypatch):
    server, _ = bridge
    server.actor.call("remember", [TRACK, ALBUM, ARTIST])
    entered, release = threading.Event(), threading.Event()
    downloader = server.actor.core.downloader
    original = downloader.run

    def slow(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(downloader, "run", slow)
    with ThreadPoolExecutor(2) as pool:
        stream = pool.submit(request, server, "stream", {"id": "am:br:lossless:song:3"})
        assert entered.wait(1)
        try:
            result = pool.submit(payload, server, "search3", {"query": "Bicep"})
            assert result.result(timeout=1)["status"] == "ok"
            assert not stream.done()
        finally:
            release.set()
        assert stream.result()[0] == 200
    assert any(m["stage"] == "download" and m["outcome"] == "ok" for m in server.metrics.snapshot())
