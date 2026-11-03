import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from PySide6.QtGui import QImage
from test_bridge import ALBUM, ARTIST, payload
from test_bridge import bridge as _bridge
from test_bridge_artwork import URL, raw_cover_response, transport

from apple_music_player.bridge.release_artwork import ReleaseUnavailable
from apple_music_player.catalog import CatalogDeferred

bridge = _bridge
ID = "am:br:lossless:artist:1"
COVER = "artist-release:" + ID


def ready_album(**kwargs):
    return replace(ALBUM, artwork_url=URL, store_url="https://music.apple.com/br/album/2", **kwargs)


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
    assert status == 200 and not QImage.fromData(body).isNull()
    assert headers["X-Artwork-Kind"] == "release-artwork"
    assert len(requests) == 1
    info = payload(server, "getArtistArtworkInfo", {"id": ID})["artistArtworkInfo"]
    assert info["albumId"].endswith(":4") and info["imageKind"] == "release-artwork"
    assert "credits" not in info and info["sourceUrl"].startswith("https://music.apple.com/")
    assert payload(server, "getArtistInfo2", {"id": ID})["artistInfo2"] == {"similarArtist": []}
    # The underlying album uses the same Covers cache, not a second image store.
    raw_cover_response(server, "am:br:lossless:album:4")
    assert len(requests) == 1


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


def test_burst_queue_and_start_budget_are_bounded(bridge):
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
    service.starts.extend([time.monotonic()] * 4)
    with pytest.raises(ReleaseUnavailable) as result:
        service.get("am:br:lossless:artist:99")
    assert result.value.reason == "release-busy"


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
    assert not server.release_art.pending and not server.release_art.starts
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
    assert not service.starts
    with pytest.raises(ReleaseUnavailable):
        service.get(ID)
    wait_jobs(service)
    assert 0 < service.retry_after(ID) - time.time() <= 300
    with pytest.raises(ReleaseUnavailable) as error:
        service.get(ID)
    assert error.value.reason == "release-unavailable"
    assert len(service.starts) == 1
