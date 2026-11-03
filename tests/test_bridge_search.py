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

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

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
            payload(server, "getArtist", {"id": "am:br:lossless:artist:1"})["error"]["message"]
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
        def search(self, query, country, entity="song", *, limit=None):
            calls.append((query, entity))
            entered.set()
            assert release.wait(3)
            return super().search(query, country, entity, limit=limit)

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
            assert "albumCount" not in core.describe(ARTIST)
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
        assert len(starts) == 4  # Validate all numeric fields before any network call.
        core.operation_deadline = now[0] + 2
        with pytest.raises(BridgeError):
            core.dispatch("search3", dict(params, query="new"))
        assert len(starts) == 4  # Deadline never bypasses catalog spacing.
    finally:
        core.close()


def test_slow_download_does_not_hold_catalog_worker(bridge, monkeypatch):
    server, _ = bridge
    server.actor.call("remember", [TRACK, ALBUM, ARTIST])
    entered, release = threading.Event(), threading.Event()
    downloader = server.actor.core.downloader
    original = downloader.run

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

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


def test_catalog_cooldown_releases_worker_for_warm_artist(tmp_path, monkeypatch):
    """Real Catalog spacing/cache, synthetic HTTP: cold search must not block warm browse."""
    import time

    starts = []
    entered = threading.Event()

    def opened(url, timeout):
        params = parse_qs(urlsplit(url).query)
        starts.append((params.get("entity", [""])[0], time.monotonic()))
        return io.BytesIO(b'{"results": []}')

    monkeypatch.setattr("apple_music_player.catalog.urlopen", opened)
    actor = Actor(lambda cancel: ProtocolCore(tmp_path / "cache", 1024, cancel=cancel))
    try:
        actor.call("remember", [ARTIST])
        actor.metadata("getArtist", {"id": "am:br:lossless:artist:1"})
        original = actor.core.catalog.search

        def search(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)

        monkeypatch.setattr(actor.core.catalog, "search", search)
        with ThreadPoolExecutor(2) as pool:
            cold = pool.submit(actor.metadata, "search3", {"query": "bjork"})
            assert entered.wait(1)
            warm = pool.submit(actor.metadata, "getArtist", {"id": "am:br:lossless:artist:1"})
            assert warm.result(timeout=1)["artist"]["id"] == "am:br:lossless:artist:1"
            assert not cold.done()
            assert cold.result(timeout=12)["searchResult3"] == {
                "artist": [],
                "album": [],
                "song": [],
            }
        assert [entity for entity, _ in starts] == ["album", "musicArtist", "album", "song"]
        assert all(b[1] - a[1] >= 2.99 for a, b in zip(starts, starts[1:], strict=False))
        metrics = actor.metrics.snapshot()
        assert any(m["stage"] == "catalog_wait" and m["count"] == 3 for m in metrics)
        assert max(m["maxMs"] for m in metrics if m["stage"] == "metadata_queue") < 1000
    finally:
        actor.close()


@pytest.mark.parametrize("stop", [False, True])
def test_deferred_metadata_retry_or_shutdown(tmp_path, stop):
    from apple_music_player.catalog import CatalogDeferred

    entered = threading.Event()
    attempts = []

    class DeferredCatalog(FakeCatalog):
        def search(self, *args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                entered.set()
                raise CatalogDeferred(1 if stop else 0.02, rate_limited=True)
            return super().search(*args, **kwargs)

    actor = Actor(
        lambda cancel: ProtocolCore(
            tmp_path / "cache", 1024, catalog=DeferredCatalog(), cancel=cancel
        )
    )
    try:
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(actor.metadata, "search3", {"query": "bjork"})
            assert entered.wait(1)
            if stop:
                actor.close()
                with pytest.raises(BridgeError, match="stopping"):
                    result.result(timeout=1)
                assert len(attempts) == 1
            else:
                assert result.result(timeout=1)["searchResult3"]["artist"]
        # All admission slots are returned, including cancellation while waiting.
        for _ in range(4):
            assert actor.metadata_pending.acquire(False)
        for _ in range(4):
            actor.metadata_pending.release()
    finally:
        if not stop:
            actor.close()


def test_parallel_deferrals_keep_deadlines_and_admission_bounded(tmp_path):
    import time

    from apple_music_player.catalog import CatalogDeferred

    calls = []
    entered = threading.Event()

    class LimitedCatalog(FakeCatalog):
        def search(self, *args, **kwargs):
            calls.append(time.monotonic())
            entered.set()
            raise CatalogDeferred(0.08, rate_limited=True)

    actor = Actor(
        lambda cancel: ProtocolCore(
            tmp_path / "cache", 1024, catalog=LimitedCatalog(), cancel=cancel
        )
    )
    try:
        started = time.monotonic()
        with ThreadPoolExecutor(4) as pool:
            jobs = [
                pool.submit(actor.metadata, "search3", {"query": "bjork"}, 0.3) for _ in range(4)
            ]
            assert entered.wait(1)
            # Wait for all four logical RPCs to be admitted, without upstream sleeps
            # consuming the worker. Cache-only operations can still run between them.
            for job in jobs:
                with pytest.raises(BridgeError):
                    job.result(timeout=1)
        assert time.monotonic() - started < 1
        assert 4 <= len(calls) <= 16
        for _ in range(4):
            assert actor.metadata_pending.acquire(False)
        for _ in range(4):
            actor.metadata_pending.release()
        assert actor.core.operation_deadline is None
    finally:
        actor.close()


def test_typing_burst_returns_protocol_responses_before_client_timeout(bridge, monkeypatch):
    """Exact typing sequence, real limiter/HTTP, synthetic public responses only."""
    import time

    from apple_music_player.catalog import Catalog

    server, _ = bridge
    server.actor.core.catalog = Catalog()
    starts = []

    def opened(url, timeout):
        params = parse_qs(urlsplit(url).query)
        query, entity = params["term"][0], params["entity"][0]
        starts.append((query, entity, time.monotonic()))
        rows = []
        if entity == "musicArtist":
            rows = [dict(wrapperType="artist", artistType="Artist", artistId=1, artistName=query)]
        return io.BytesIO(json.dumps({"results": rows}).encode())

    monkeypatch.setattr("apple_music_player.catalog.urlopen", opened)
    queries = ("b", "bj", "bjo", "bjor")

    def search(query):
        started = time.monotonic()
        result = payload(server, "search3", {"query": query})
        return result, time.monotonic() - started

    with ThreadPoolExecutor(4) as pool:
        futures = []
        for query in queries:
            futures.append(pool.submit(search, query))
            threading.Event().wait(0.25)  # Just beyond the client's 200ms debounce.
        results = [future.result(timeout=15) for future in futures]
    for query, (result, seconds) in zip(queries, results, strict=True):
        assert seconds < 14.5  # Default Supersonic request deadline is 15s.
        if result["status"] == "ok":
            assert result["searchResult3"]["artist"][0]["name"] == query
        else:
            assert result["error"]["code"] == 0
            assert "Catalog" in result["error"]["message"]
    assert any(result["status"] == "failed" for result, _ in results)
    assert all(b[2] - a[2] >= 2.99 for a, b in zip(starts, starts[1:], strict=False))
    assert {query for query, _, _ in starts} <= set(queries)
    assert payload(server, "ping")["status"] == "ok"


def test_singleflight_waiter_timeout_does_not_cancel_other_waiter(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class SlowCatalog(FakeCatalog):
        def search(self, query, country, entity="song", *, limit=None):
            calls.append((query, entity))
            entered.set()
            assert release.wait(2)
            return super().search(query, country, entity, limit=limit)

    actor = Actor(
        lambda cancel: ProtocolCore(tmp_path / "cache", 1024, catalog=SlowCatalog(), cancel=cancel)
    )
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(
                actor.metadata,
                "search3",
                {"query": "bjork", "u": "a", "t": "one", "s": "salt1"},
                0.2,
            )
            assert entered.wait(1)
            second = pool.submit(
                actor.metadata, "search3", {"query": "bjork", "u": "a", "t": "two", "s": "salt2"}, 2
            )
            # Both calls share work despite distinct per-request token/salt values.
            with pytest.raises(BridgeError, match="timed out"):
                first.result(timeout=1)
            assert not second.done()
            release.set()
            assert second.result(timeout=1)["searchResult3"]["artist"]
        assert len(calls) == 3
        assert any(
            m["stage"] == "search_flight" and m["count"] == 1 for m in actor.metrics.snapshot()
        )
    finally:
        release.set()
        actor.close()


@pytest.mark.parametrize(
    "difference", [{"query": "bjork"}, {"u": "other"}, {"albumCount": "1"}, {"c": "other-client"}]
)
def test_singleflight_does_not_combine_distinct_requests(tmp_path, difference):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class SlowCatalog(FakeCatalog):
        def search(self, query, country, entity="song", *, limit=None):
            calls.append((query, entity))
            entered.set()
            assert release.wait(2)
            return super().search(query, country, entity, limit=limit)

    actor = Actor(
        lambda cancel: ProtocolCore(tmp_path / "cache", 1024, catalog=SlowCatalog(), cancel=cancel)
    )
    try:
        params = {"query": "bjor", "u": "first", "c": "client"}
        with ThreadPoolExecutor(2) as pool:
            one = pool.submit(actor.metadata, "search3", params)
            assert entered.wait(1)
            two = pool.submit(actor.metadata, "search3", dict(params, **difference))
            release.set()
            assert one.result(timeout=1)["searchResult3"]
            assert two.result(timeout=1)["searchResult3"]
        assert len(calls) == 6
        assert not any(m["stage"] == "search_flight" for m in actor.metrics.snapshot())
    finally:
        release.set()
        actor.close()


def test_identical_http_search_burst_shares_one_flight(bridge, monkeypatch):
    import time

    server, _ = bridge
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = server.actor.core.catalog.search

    def slow(*args, **kwargs):
        calls.append(args)
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(server.actor.core.catalog, "search", slow)
    try:
        with ThreadPoolExecutor(8) as pool:
            jobs = [pool.submit(payload, server, "search3", {"query": "bjork"}) for _ in range(8)]
            assert entered.wait(1)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if any(
                    m["stage"] == "search_flight" and m["count"] == 7
                    for m in server.metrics.snapshot()
                ):
                    break
                threading.Event().wait(0.01)
            release.set()
            results = [job.result(timeout=2) for job in jobs]
        assert all(result["status"] == "ok" for result in results)
        assert all(result == results[0] for result in results)
        assert len(calls) == 3
    finally:
        release.set()


@pytest.mark.parametrize(
    "kind,entity", [("artist", "musicArtist"), ("album", "album"), ("song", "song")]
)
def test_category_only_search_counts_paging_and_shared_limiter(tmp_path, monkeypatch, kind, entity):
    now = [100.0]
    requests = []
    monkeypatch.setattr("apple_music_player.catalog.time.monotonic", lambda: now[0])

    class ClockEvent:
        def is_set(self):
            return False

        def wait(self, seconds):
            now[0] += seconds

    def opened(url, timeout):
        params = parse_qs(urlsplit(url).query)
        requests.append((params, now[0]))
        assert params["country"] == ["br"]
        assert params["entity"] == [entity]
        assert "offset" not in params
        rows = []
        for i in range(1, int(params["limit"][0]) + 1):
            row = dict(artistId=i, artistName=f"Artist {i}")
            if kind == "artist":
                row.update(wrapperType="artist", artistType="Artist")
            elif kind == "album":
                row.update(
                    wrapperType="collection",
                    collectionType="Album",
                    collectionId=i,
                    collectionName=f"Album {i}",
                )
            else:
                row.update(wrapperType="track", kind="song", trackId=i, trackName=f"Song {i}")
            rows.append(row)
        return io.BytesIO(json.dumps({"results": rows}).encode())

    monkeypatch.setattr("apple_music_player.catalog.urlopen", opened)
    core = ProtocolCore(tmp_path / "cache", 1024, cancel=ClockEvent())
    params = {"query": "bjork", "artistCount": "0", "albumCount": "0", "songCount": "0"}
    try:
        assert all(not rows for rows in core.dispatch("search3", params)["searchResult3"].values())
        assert requests == []
        params[kind + "Count"] = "20"
        first = core.dispatch("search3", params)["searchResult3"]
        assert all(not rows for name, rows in first.items() if name != kind)
        assert len(first[kind]) == 20
        page = dict(params, **{kind + "Offset": "20"})
        second = core.dispatch("search3", page)["searchResult3"][kind]
        assert [row["id"].rsplit(":", 1)[1] for row in second] == [str(i) for i in range(21, 41)]
        assert core.dispatch("search3", page)["searchResult3"][kind] == second
        assert [(p["limit"][0], t) for p, t in requests] == [("20", 100), ("40", 103)]
        last = core.dispatch("search3", dict(params, **{kind + "Offset": "190"}))["searchResult3"][
            kind
        ]
        assert len(last) == 10 and last[-1]["id"].endswith(":200")
        assert requests[-1][0]["limit"] == ["200"]
        count = len(requests)
        assert (
            core.dispatch("search3", dict(params, **{kind + "Offset": "200"}))["searchResult3"][
                kind
            ]
            == []
        )
        assert len(requests) == count
        with pytest.raises(BridgeError):
            core.dispatch("search3", dict(params, songOffset="invalid"))
        assert len(requests) == count
    finally:
        core.close()


def test_catalog_limits_preserve_qt_defaults(monkeypatch):
    from apple_music_player.catalog import Catalog
    from apple_music_player.model import PlayerError

    catalog = Catalog()
    requests = []

    def request(endpoint, **params):
        requests.append(params)
        return []

    monkeypatch.setattr(catalog, "request", request)
    for entity in ("musicArtist", "album", "song"):
        catalog.search("bjork", "br", entity)
    assert [p["limit"] for p in requests] == [50, 200, 50]
    for bad in (0, -1, 201, True, "20"):
        with pytest.raises(PlayerError):
            catalog.search("bjork", "br", limit=bad)
    assert len(requests) == 3


@pytest.mark.parametrize("kind,entity", [("artist", "musicArtist"), ("album", "album")])
def test_http_category_only_search_uses_one_requested_upstream_limit(
    bridge, monkeypatch, kind, entity
):
    from apple_music_player.catalog import Catalog

    server, _ = bridge
    server.actor.core.catalog = Catalog()
    requests = []

    def opened(url, timeout):
        params = parse_qs(urlsplit(url).query)
        requests.append(params)
        return io.BytesIO(b'{"results": []}')

    monkeypatch.setattr("apple_music_player.catalog.urlopen", opened)
    params = {"query": "bjork", "artistCount": "0", "albumCount": "0", "songCount": "0"}
    params[kind + "Count"] = "50"
    result = payload(server, "search3", params)
    assert result["status"] == "ok"
    assert result["searchResult3"] == {"artist": [], "album": [], "song": []}
    assert len(requests) == 1
    assert requests[0]["entity"] == [entity]
    assert requests[0]["limit"] == ["50"]
    assert requests[0]["country"] == ["br"]


def test_artist_search_count_and_portrait_are_unknown_despite_cached_albums(bridge):
    import xml.etree.ElementTree as ET

    server, _ = bridge
    server.actor.call("remember", [ARTIST, ALBUM, replace(ALBUM, id="99")])
    params = {"query": "fixture", "artistCount": "50", "albumCount": "0", "songCount": "0"}
    artist = payload(server, "search3", params)["searchResult3"]["artist"][0]
    assert artist == {"id": "am:br:lossless:artist:1", "name": ARTIST.title}
    status, _, body = request(server, "search3", dict(params, f="xml"))
    assert status == 200
    node = ET.fromstring(body).find(".//{http://subsonic.org/restapi}artist")
    assert node is not None
    assert "albumCount" not in node.attrib and "coverArt" not in node.attrib
    # Detail count describes this returned collection, not remembered albums or a
    # claim that the limited public lookup is a complete subscription discography.
    detail = payload(server, "getArtist", {"id": artist["id"]})["artist"]
    assert detail["albumCount"] == len(detail["album"]) == 1
    assert "coverArt" not in detail and "artistImageUrl" not in detail
    assert payload(server, "search3", params)["searchResult3"]["artist"][0] == artist
