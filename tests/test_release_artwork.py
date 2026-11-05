import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_bridge import ALBUM, ARTIST, payload
from test_bridge import bridge as _bridge
from test_bridge_artwork import URL, raw_cover_response, transport

from apple_music_player.artwork import decode
from apple_music_player.bridge.release_artwork import ReleaseUnavailable
from apple_music_player.catalog import CatalogDeferred

bridge = _bridge
ID = "am:br:lossless:artist:1"
COVER = "artist-release:" + ID


def admissions(service):
    return sum(
        m["count"]
        for m in service.metrics.snapshot()
        if m["stage"] == "artist_release" and m["outcome"] == "miss"
    )


def ready_album(**kwargs):
    return replace(
        ALBUM, **(dict(artwork_url=URL, store_url="https://music.apple.com/br/album/2") | kwargs)
    )


def wait_jobs(service):
    with service.lock:
        jobs = list(service.pending.values())
    for job in jobs:
        job.result(timeout=4)


def test_default_search_gallery_cover_and_source_metadata(bridge, monkeypatch):
    from test_bridge_artwork import image_bytes

    server, _ = bridge
    albums = [
        ready_album(release_date="2020-01-01"),
        ready_album(id="4", release_date="2024-01-01"),
        ready_album(id="5", artist_id="99", release_date="2025-01-01"),
        ready_album(id="6", country="us", release_date="2025-01-01"),
        ready_album(id="7", release_date="2999-01-01"),
        ready_album(id="8", release_date="bad"),
    ]
    server.actor.call("remember", [a for a in albums if a.country == "br"])
    requests = transport(monkeypatch, image_bytes("JPEG"))
    node = payload(
        server,
        "search3",
        {"query": "fixture", "artistCount": "1", "albumCount": "0", "songCount": "0"},
    )["searchResult3"]["artist"][0]
    assert node["coverArt"] == COVER and not server.release_art.pending
    assert payload(server, "getArtists")["artists"]["index"][0]["artist"][0]["coverArt"] == COVER
    status, headers, body = raw_cover_response(server, COVER)
    assert status == 200 and decode(body) is not None
    assert headers["X-Artwork-Kind"] == "release-artwork"
    assert len(requests) == 1
    info = payload(server, "getArtistArtworkInfo", {"id": ID})["artistArtworkInfo"]
    assert info["albumId"].endswith(":4") and info["imageKind"] == "release-artwork"
    assert "credits" not in info and info["sourceUrl"].startswith("https://music.apple.com/")
    assert payload(server, "getArtistInfo2", {"id": ID})["artistInfo2"] == {"similarArtist": []}
    # The underlying album uses the same Covers cache, not a second image store.
    raw_cover_response(server, "am:br:lossless:album:4")
    assert len(requests) == 1


@pytest.mark.parametrize("size", [None, "0"])
def test_release_full_artwork_uses_same_1400_default(bridge, monkeypatch, size):
    from test_bridge import request
    from test_bridge_artwork import image_bytes

    server, _ = bridge
    server.actor.call("remember", [ARTIST, ready_album()])
    calls = transport(monkeypatch, image_bytes(width=1400))
    params = {"id": COVER}
    if size is not None:
        params["size"] = size
    response = request(server, "getCoverArt", params)
    assert response[0] == 200
    assert decode(response[2], 2048).width == 1400
    assert calls == [(URL.replace("100x100", "1400x1400"), 8)]


def test_pending_enrichment_does_not_hold_http_or_search_and_deduplicates(bridge):
    server, _ = bridge
    server.actor.call("remember", [ARTIST])
    entered, release = threading.Event(), threading.Event()
    calls = []

    class SlowCatalog:
        def artist_albums(self, identifier, country):
            calls.append((identifier, country))
            entered.set()
            assert release.wait(3)
            return [ready_album()]

    server.release_art.catalog = SlowCatalog()
    start = time.monotonic()
    try:
        for _ in range(3):
            status, headers, _ = raw_cover_response(server, COVER)
            assert status == 503 and headers["X-Artwork-Reason"] == "release-pending"
        assert time.monotonic() - start < 1
        assert entered.wait(1)
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(
                payload,
                server,
                "search3",
                {"query": "fixture", "artistCount": "1", "albumCount": "0", "songCount": "0"},
            ).result(timeout=1)
            assert result["status"] == "ok"
    finally:
        release.set()
    wait_jobs(server.release_art)
    assert calls == [("1", "br")]
    assert server.release_art.get(ID).id == ALBUM.id


def test_burst_queue_is_bounded_but_new_jobs_are_not_minute_capped(bridge):
    server, _ = bridge
    server.actor.call("remember", [replace(ARTIST, id=str(i)) for i in range(1, 101)])
    entered, release = threading.Event(), threading.Event()

    class SlowCatalog:
        def artist_albums(self, *_):
            entered.set()
            assert release.wait(3)
            return []

    service = server.release_art
    service.catalog = SlowCatalog()
    try:
        for i in range(1, 101):
            with pytest.raises(ReleaseUnavailable) as result:
                service.get(f"am:br:lossless:artist:{i}")
            assert result.value.reason == ("release-pending" if i <= 4 else "release-busy")
        assert entered.wait(1) and len(service.pending) == 4
    finally:
        release.set()
    wait_jobs(service)
    with pytest.raises(ReleaseUnavailable) as result:
        service.get("am:br:lossless:artist:99")
    assert result.value.reason == "release-pending"
    wait_jobs(service)


def test_miss_and_positive_selection_survive_service_restart(bridge):
    from apple_music_player.bridge.release_artwork import ReleaseArtwork

    server, _ = bridge
    server.actor.call("remember", [ARTIST])
    calls = []

    class Empty:
        def artist_albums(self, *_):
            calls.append(1)
            return []

    service = server.release_art
    service.catalog = Empty()
    with pytest.raises(ReleaseUnavailable):
        service.get(ID)
    wait_jobs(service)
    service.close()
    service = server.release_art = ReleaseArtwork(server.actor, catalog=Empty())
    with pytest.raises(ReleaseUnavailable) as result:
        service.get(ID)
    assert result.value.reason == "release-unavailable" and calls == [1]
    server.actor.call("remember", [ready_album()])
    assert service.get(ID).id == ALBUM.id  # Newly browsed metadata overrides a miss.
    service.close()
    server.release_art = ReleaseArtwork(server.actor, catalog=Empty())
    assert server.release_art.get(ID).id == ALBUM.id and calls == [1]


def test_enrichment_shares_catalog_spacing_but_not_network_worker(bridge, monkeypatch):
    server, _ = bridge
    core_catalog = server.actor.core.catalog
    service = server.release_art
    assert service.catalog.limiter is core_catalog.limiter
    core_catalog.last_request = time.monotonic()
    with pytest.raises(CatalogDeferred):
        service.catalog.artist_albums("1", "br")
    assert service.catalog.responses is not core_catalog.responses
    core_catalog.not_before = time.monotonic() + 30
    with pytest.raises(CatalogDeferred):
        service.catalog.artist_albums("1", "br")


def test_shutdown_rejects_new_enrichment(bridge):
    server, _ = bridge
    server.actor.call("remember", [ARTIST])
    server.release_art.close()
    with pytest.raises(ReleaseUnavailable) as result:
        server.release_art.get(ID)
    assert result.value.reason == "shutdown"


def test_large_index_does_not_schedule_and_selection_updates(bridge):
    import json
    from dataclasses import asdict

    server, _ = bridge
    server.actor.call("remember", [replace(ARTIST, id=str(i)) for i in range(1, 513)])
    result = payload(server, "getArtists")["artists"]
    assert sum(len(index["artist"]) for index in result["index"]) == 512
    assert not server.release_art.pending and not admissions(server.release_art)
    server.actor.call("remember", [ready_album(release_date="2020-01-01")])
    # Even an otherwise valid stored row from another storefront cannot win.
    wrong = ready_album(id="99", country="us", release_date="2025-01-01")
    with server.release_art.connection() as db:
        db.execute(
            "INSERT INTO bridge_metadata VALUES (?,?)",
            ("am:us:lossless:album:99", json.dumps(asdict(wrong))),
        )
    assert server.release_art.get(ID).id == ALBUM.id
    server.actor.call("remember", [ready_album(id="10", release_date="2024-01-01")])
    assert server.release_art.get(ID).id == "10"


def test_failed_lookup_is_bounded_and_invalid_identity_cannot_schedule(bridge):
    from apple_music_player.bridge.core import BridgeError

    server, _ = bridge
    server.actor.call("remember", [ARTIST])

    class Failed:
        def artist_albums(self, *_):
            raise RuntimeError("sensitive provider payload must not escape")

    service = server.release_art
    service.catalog = Failed()
    with pytest.raises(BridgeError):
        service.get("am:us:lossless:artist:1")
    assert not admissions(service)
    with pytest.raises(ReleaseUnavailable):
        service.get(ID)
    wait_jobs(service)
    assert 0 < service.retry_after(ID) - time.time() <= 300
    with pytest.raises(ReleaseUnavailable) as error:
        service.get(ID)
    assert error.value.reason == "release-failed"
    assert admissions(service) == 1


@pytest.mark.parametrize("isolated", [False, True])
def test_artist_grid_burst_leaves_capacity_for_album_page(bridge, monkeypatch, isolated):
    """Five client image slots, distinct cold covers, then a navigated album page."""
    from test_bridge_artwork import image_bytes

    server, _ = bridge
    entered, release = threading.Event(), threading.Event()
    server.actor.call(
        "remember",
        [replace(ARTIST, id=str(i)) for i in range(1, 6)]
        + [
            ready_album(
                id=str(100 + i), artist_id=str(i), artwork_url=URL.replace("fixture", f"artist{i}")
            )
            for i in range(1, 6)
        ]
        + [
            ready_album(id="200", artist_id="999", artwork_url=URL.replace("fixture", "album-page"))
        ],
    )
    if not isolated:
        server.covers.background_pool.shutdown()
        server.covers.background_pool = server.covers.pool
    all_arrived = threading.Event()
    arrived_lock = threading.Lock()
    arrived = []
    original_get = server.release_art.get

    def tracked_get(identifier):
        result = original_get(identifier)
        with arrived_lock:
            arrived.append(identifier)
            if len(arrived) == 5:
                all_arrived.set()
        return result

    monkeypatch.setattr(server.release_art, "get", tracked_get)
    loads = []
    image = decode(image_bytes(), 1024)

    def slow_artist(url):
        loads.append(url)
        if "/artist" in url:
            entered.set()
            assert release.wait(10)
        return image

    monkeypatch.setattr(server.covers, "load", slow_artist)
    try:
        with ThreadPoolExecutor(6) as pool:
            jobs = [
                pool.submit(
                    raw_cover_response,
                    server,
                    f"artist-release:am:br:lossless:artist:{i}",
                    timeout=15,
                )
                for i in range(1, 6)
            ]
            assert entered.wait(3)
            assert all_arrived.wait(3)
            deadline = time.monotonic() + 1
            while len(server.covers.pending) < 2 and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            assert len(server.covers.pending) >= 2
            # Independent client request: no queued artist download may occupy
            # both direct-album workers. Client-side retry sleeps are separate.
            album_request = pool.submit(raw_cover_response, server, "am:br:lossless:album:200")
            if isolated:
                result = album_request.result(timeout=5)
                assert result[0] == 200 and decode(result[2]) is not None
            else:
                from concurrent.futures import TimeoutError

                with pytest.raises(TimeoutError):
                    album_request.result(timeout=0.2)
            assert len(server.covers.background_pending) <= 2
            release.set()
            responses = [job.result(timeout=2) for job in jobs]
            assert all(response[0] in (200, 503) for response in responses)
            assert all(
                response[1]["Retry-After"] == "1" for response in responses if response[0] == 503
            )
    finally:
        release.set()
    assert any("album-page" in url for url in loads)


def test_release_miss_is_not_a_transient_retry_response(bridge):
    server, _ = bridge
    server.actor.call("remember", [ARTIST])
    with server.release_art.connection() as db:
        db.execute(
            "INSERT INTO bridge_release_checks VALUES (?,?,?)",
            (ID, time.time() + 300, "release-unavailable"),
        )
    status, headers, body = raw_cover_response(server, COVER)
    assert status == 404
    assert headers["X-Artwork-Reason"] == "release-unavailable"
    assert headers.get_all("Cache-Control") == ["no-store"]
    assert 1 <= int(headers["Retry-After"]) <= 300
    assert b"<error " in body and not server.release_art.pending


@pytest.mark.parametrize("deferred", [False, True])
def test_failed_and_deferred_lookups_are_not_cached_as_absent(bridge, deferred):
    server, _ = bridge
    server.actor.call("remember", [ARTIST])

    class Failed:
        def artist_albums(self, *_):
            if deferred:
                raise CatalogDeferred(50, rate_limited=True)
            raise RuntimeError("private response must never be returned")

    server.release_art.catalog = Failed()
    with pytest.raises(ReleaseUnavailable):
        server.release_art.get(ID)
    wait_jobs(server.release_art)
    status, headers, body = raw_cover_response(server, COVER)
    assert status == 503
    assert headers["X-Artwork-Reason"] == ("release-deferred" if deferred else "release-failed")
    assert 1 <= int(headers["Retry-After"]) <= (50 if deferred else 300)
    assert b"private response" not in body
    assert admissions(server.release_art) == 1


def test_legacy_checks_migrate_without_inventing_a_confirmed_miss(tmp_path):
    from apple_music_player.bridge.http import ProtocolCore

    root = tmp_path / "cache"
    core = ProtocolCore(root, 1024)
    core.remember([ARTIST])
    core.cache.db.execute("DROP TABLE bridge_release_checks")
    core.cache.db.execute(
        "CREATE TABLE bridge_release_checks(id TEXT PRIMARY KEY,retry_after REAL)"
    )
    core.cache.db.execute("INSERT INTO bridge_release_checks VALUES (?,?)", (ID, time.time() + 100))
    core.cache.db.commit()
    core.close()
    core = ProtocolCore(root, 1024)
    try:
        assert core.cached_artist(ID).id == ARTIST.id
        assert core.cache.db.execute(
            "SELECT retry_after FROM bridge_release_checks"
        ).fetchone() == (0,)
        assert core.cache.db.execute("SELECT reason FROM bridge_release_checks").fetchone() == (
            "release-failed",
        )
    finally:
        core.close()


@pytest.mark.parametrize("title", ["Only Release - EP", "Only Release - Single"])
def test_ep_or_single_only_artist_resolves_through_catalog_parser(bridge, monkeypatch, title):
    from test_bridge_artwork import image_bytes

    from apple_music_player.catalog import Catalog

    server, _ = bridge
    server.actor.call("remember", [ARTIST])
    calls = []

    class PublicFixture(Catalog):
        def request(self, endpoint, **params):
            calls.append((endpoint, params))
            return [
                {"wrapperType": "artist", "artistId": 1, "artistName": "Fixture"},
                {
                    "wrapperType": "collection",
                    "collectionType": "Album",
                    "artistId": 1,
                    "artistName": "Fixture",
                    "collectionId": 22,
                    "collectionName": title,
                    "artworkUrl100": URL,
                    "releaseDate": "2020-01-01T00:00:00Z",
                },
            ]

    server.release_art.catalog = PublicFixture()
    transport(monkeypatch, image_bytes())
    with pytest.raises(ReleaseUnavailable):
        server.release_art.get(ID)
    wait_jobs(server.release_art)
    status, _, body = raw_cover_response(server, COVER)
    assert status == 200 and decode(body) is not None
    assert server.release_art.get(ID).album_title == title
    assert calls == [("lookup", {"id": "1", "country": "br", "entity": "album", "limit": 200})]


@pytest.mark.parametrize("via_detail", [False, True])
def test_exact_lookup_association_allows_featured_artist_release(bridge, via_detail):
    from apple_music_player.bridge.release_artwork import ReleaseArtwork

    server, _ = bridge
    server.actor.call("remember", [ARTIST])
    featured = ready_album(id="88", artist_id="99", album_title="Collaboration - EP")
    # Merely finding a different primary artist's album in cache is insufficient.
    server.actor.call("remember", [featured])
    assert server.release_art.selection(ARTIST) is None

    class Associated:
        def artist_albums(self, identifier, country):
            assert (identifier, country) == ("1", "br")
            return [featured]

    if via_detail:
        server.actor.core.catalog = Associated()
        result = payload(server, "getArtist", {"id": ID})
        assert result["artist"]["album"][0]["id"].endswith(":88")
    else:
        server.release_art.catalog = Associated()
        with pytest.raises(ReleaseUnavailable):
            server.release_art.get(ID)
        wait_jobs(server.release_art)
    assert server.release_art.get(ID).id == "88"
    server.release_art.close()
    server.release_art = ReleaseArtwork(server.actor, catalog=Associated())
    assert server.release_art.get(ID).id == "88"  # Association survives restart.
    # Same spelling/metadata does not grant association to another artist ID.
    other = replace(ARTIST, id="2")
    server.actor.call("remember", [other])
    assert server.release_art.selection(other) is None


def test_twelve_cold_artists_continue_with_shared_spacing(bridge, monkeypatch):
    """Real request/parser/cache and virtual waits: no extra eight-per-minute stop."""
    import io
    import json
    from urllib.parse import parse_qs, urlsplit

    server, _ = bridge
    server.actor.call("remember", [replace(ARTIST, id=str(i)) for i in range(1, 13)])
    now = [100.0]
    calls = []
    monkeypatch.setattr("apple_music_player.catalog.time.monotonic", lambda: now[0])

    class ClockStop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            now[0] += seconds
            return self.stopped

        def set(self):
            self.stopped = True

    server.release_art.stop = ClockStop()

    def opened(url, timeout):
        params = parse_qs(urlsplit(url).query)
        calls.append((now[0], params))
        artist = int(params["id"][0])
        return io.BytesIO(
            json.dumps(
                {
                    "results": [
                        {
                            "wrapperType": "collection",
                            "collectionType": "Album",
                            "artistId": artist,
                            "collectionId": artist + 100,
                            "collectionName": "Release - EP",
                            "artworkUrl100": URL,
                            "releaseDate": "2021-01-01T00:00:00Z",
                        }
                    ]
                }
            ).encode()
        )

    monkeypatch.setattr("apple_music_player.catalog.urlopen", opened)
    for i in range(1, 13):
        identifier = f"am:br:lossless:artist:{i}"
        with pytest.raises(ReleaseUnavailable) as error:
            server.release_art.get(identifier)
        assert error.value.reason == "release-pending"
        wait_jobs(server.release_art)
        assert server.release_art.get(identifier).artist_id == str(i)
    assert len(calls) == 12 and admissions(server.release_art) == 12
    assert calls[-1][0] - calls[0][0] == pytest.approx(33)
    assert all(b[0] - a[0] >= 3 for a, b in zip(calls, calls[1:], strict=False))
    assert all(p["country"] == ["br"] and p["entity"] == ["album"] for _, p in calls)
    # Cache-only revisits generate no provider requests or admission waits.
    for i in range(1, 13):
        server.release_art.get(f"am:br:lossless:artist:{i}")
    assert len(calls) == 12


def test_active_interactive_request_has_priority_over_release_lookup(bridge):
    server, _ = bridge
    server.actor.call("remember", [ARTIST])
    entered, finish, background_called = threading.Event(), threading.Event(), threading.Event()

    class Foreground:
        def search(self, *args, **kwargs):
            entered.set()
            assert finish.wait(4)
            return [ARTIST]

    class Background:
        def artist_albums(self, *args):
            background_called.set()
            return [ready_album()]

    server.actor.core.catalog = Foreground()
    server.release_art.catalog = Background()
    try:
        with ThreadPoolExecutor(1) as pool:
            search = pool.submit(
                payload,
                server,
                "search3",
                {"query": "daughter", "artistCount": "1", "albumCount": "0", "songCount": "0"},
            )
            assert entered.wait(1) and not server.actor.metadata_idle.is_set()
            # Model an upstream call lasting past the old one-second quiet window.
            server.actor.last_metadata = time.monotonic() - 5
            with pytest.raises(ReleaseUnavailable):
                server.release_art.get(ID)
            assert not background_called.wait(0.2)
            finish.set()
            assert search.result(timeout=1)["status"] == "ok"
        assert background_called.wait(2)
        wait_jobs(server.release_art)
        assert server.actor.metadata_idle.is_set()
    finally:
        finish.set()


def test_shared_retry_protocol_fixture(bridge, monkeypatch):
    import json
    from pathlib import Path

    server, _ = bridge
    server.actor.call("remember", [ARTIST])
    monkeypatch.setattr("apple_music_player.bridge.release_artwork.time.time", lambda: 1000.0)
    cases = json.loads((Path(__file__).parent / "fixtures/artwork_retry_protocol.json").read_text())
    for case in cases:
        with server.release_art.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO bridge_release_checks VALUES (?,?,?)",
                (ID, 1000 + case["seconds"], case["reason"]),
            )
        status, headers, body = raw_cover_response(server, COVER)
        assert status == case["status"]
        assert headers["X-Artwork-Status"] == "unavailable"
        assert headers["X-Artwork-Reason"] == case["reason"]
        assert int(headers["Retry-After"]) == case["seconds"]
        assert b'<error code="0"' in body
        assert not server.release_art.pending
