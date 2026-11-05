import threading
import time
from types import SimpleNamespace

import pytest
from test_bridge import ARTIST, payload
from test_bridge import bridge as _bridge
from test_release_artwork import ID, ready_album, wait_jobs

from apple_music_player.bridge import artwork_seed
from apple_music_player.bridge.release_artwork import ReleaseUnavailable
from apple_music_player.catalog import Catalog, CatalogDeferred

bridge = _bridge
PARAMS = {"query": "fixture", "artistCount": "32", "albumCount": "0", "songCount": "0"}


class Clock:
    def __init__(self):
        self.now = time.monotonic() + 10

    def monotonic(self):
        return self.now

    def wait(self, *_args):
        self.now += _args[-1]
        return False


def prepare(server, monkeypatch, albums=()):
    service = server.release_art
    server.actor.call("remember", [ARTIST])
    clock = Clock()
    monkeypatch.setattr(artwork_seed, "time", clock)
    monkeypatch.setattr(service.seed, "wait", clock.wait)
    calls = []

    def search(*args, **kwargs):
        calls.append((args, kwargs))
        return list(albums)

    service.catalog = SimpleNamespace(search=search)
    query = service.seed.observe(PARAMS)
    query.ready = True
    return service, query, calls, clock


def test_exact_primary_identity_ranking_and_no_lookup_provenance(bridge, monkeypatch):
    server, _ = bridge
    albums = [
        ready_album(id="10", artist_id="99"),  # Same artist name, different ID.
        ready_album(id="11", country="us"),
        ready_album(id="12", artwork_url="https://evil.invalid/image.jpg"),
        ready_album(id="13", release_date="2099-01-01"),
        ready_album(id="14", release_date="2020-01-01"),
        ready_album(id="15", release_date="2021-01-01"),
        ready_album(id="15", release_date="2021-01-01"),
    ]
    service, _, calls, _ = prepare(server, monkeypatch, albums)
    assert service.seed.run()
    assert service.get(ID).id == "15"
    with service.connection() as db:
        assert db.execute("SELECT count(*) FROM bridge_artist_releases").fetchone() == (0,)
        assert db.execute(
            "SELECT lookup FROM bridge_artist_artwork WHERE id=?", (ID,)
        ).fetchone() == (0,)
        # Wrong profile rows cannot be selected.
        db.execute("DELETE FROM bridge_metadata")
        db.execute("UPDATE bridge_artist_artwork SET id=replace(id,'lossless','aac')")
    assert service.selection(ARTIST) is None
    assert len(calls) == 1


def test_dedup_pagination_history_and_debounce(bridge, monkeypatch):
    server, _ = bridge
    service, old, calls, clock = prepare(server, monkeypatch, [ready_album()])
    for query in ("f", "fi", "fix", "fixture"):
        current = service.seed.observe(PARAMS | {"query": query})
        current.ready = True
    assert old.cancelled.is_set()
    assert service.seed.run()
    assert calls[0][0] == ("fixture", "br", "album")
    assert clock.now >= current.created + service.seed.debounce
    assert service.seed.observe(PARAMS | {"artistOffset": "32"}) is current
    current.ready = True
    assert not service.seed.run()
    for i in range(140):
        current = service.seed.observe(PARAMS | {"query": str(i)})
        current.ready = True
        service.seed.run()
    assert len(service.seed.attempted) == 128


@pytest.mark.parametrize("failure", [None, RuntimeError("offline"), CatalogDeferred(300, True)])
def test_empty_error_throttle_leave_artist_fallback(bridge, monkeypatch, failure):
    server, _ = bridge
    service, _, _, _ = prepare(server, monkeypatch)

    def search(*args, **kwargs):
        if failure:
            raise failure
        return []

    service.catalog.search = search
    service.catalog.artist_albums = lambda *_: [ready_album()]
    service.seed.run()
    assert service.check(ID) == (0, "release-failed")
    with pytest.raises(ReleaseUnavailable):
        service.get(ID)
    wait_jobs(service)
    assert service.get(ID).id == "2"


def test_recheck_selection_after_limiter_wait(bridge, monkeypatch):
    server, _ = bridge
    service, _, _, _ = prepare(server, monkeypatch)
    service.seed.current = None
    calls = []

    def lookup(*_):
        calls.append(1)
        raise CatalogDeferred(0.01)

    def wait(seconds):
        service.store_albums([ready_album()])
        return False

    service.catalog.artist_albums = lookup
    monkeypatch.setattr(service.stop, "wait", wait)
    service.enrich(ID, ARTIST)
    assert calls == [1]
    assert service.get(ID).id == "2"
    assert service.check(ID) == (0, "release-failed")  # resolved is not a negative


def test_seed_retires_already_queued_artist_jobs(bridge, monkeypatch):
    server, _ = bridge
    service, _, calls, _ = prepare(server, monkeypatch, [ready_album()])
    service.catalog.artist_albums = lambda *_: pytest.fail("redundant lookup")
    service.enrich(ID, ARTIST)
    assert len(calls) == 1 and service.get(ID).id == "2"


def test_slow_optional_search_does_not_hold_foreground_and_stale_result_is_discarded(bridge):
    server, _ = bridge
    service = server.release_art
    entered, release = threading.Event(), threading.Event()

    def search(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return [ready_album()]

    service.catalog.search = search
    started = time.monotonic()
    assert payload(server, "search3", PARAMS)["status"] == "ok"
    assert time.monotonic() - started < 0.5
    assert entered.wait(2)
    try:
        started = time.monotonic()
        assert payload(server, "search3", PARAMS | {"query": "new"})["status"] == "ok"
        assert time.monotonic() - started < 0.5
        service.seed.observe(PARAMS | {"query": ""})
    finally:
        release.set()
    service.pool.submit(lambda: None).result(timeout=3)
    assert service.selection(ARTIST) is None


def test_shutdown_cancels_pending_seed_without_network(bridge):
    server, _ = bridge
    service = server.release_art
    service.catalog.search = lambda *_a, **_k: pytest.fail("cancelled seed")
    query = service.seed.observe(PARAMS)
    service.seed.ready(query, {"searchResult3": {"artist": [1]}})
    service.close()
    service.pool.shutdown(wait=True)
    assert query.cancelled.is_set()


def test_shared_catalog_cooldown_blocks_seed_and_fallback(bridge, monkeypatch):
    server, _ = bridge
    service, _, _, clock = prepare(server, monkeypatch)
    service.catalog = Catalog(limiter=service.core.catalog.limiter)
    service.catalog.not_before = time.monotonic() + 300
    service.seed.run()
    assert service.selection(ARTIST) is None
    assert not service.catalog.responses
    service.enrich(ID, ARTIST)
    after, reason = service.check(ID)
    assert reason == "release-deferred" and after > time.time() + 250
    assert clock.now < service.seed.current.created + 2


@pytest.mark.parametrize(
    "album",
    [
        ready_album(artist_id="99"),
        ready_album(country="us"),
        ready_album(artwork_url=""),
        ready_album(release_date="2099-01-01"),
    ],
)
def test_unusable_seed_is_never_negative_evidence(bridge, monkeypatch, album):
    server, _ = bridge
    service, _, _, _ = prepare(server, monkeypatch, [album])
    service.seed.run()
    assert service.selection(ARTIST) is None
    assert service.check(ID) == (0, "release-failed")


def test_query_storm_has_one_replacement_and_one_network_worker(bridge):
    server, _ = bridge
    service = server.release_art
    entered, release = threading.Event(), threading.Event()
    calls = []
    service.seed.debounce = 0

    def search(query, *_args, **_kwargs):
        calls.append(query)
        if len(calls) == 1:
            entered.set()
            assert release.wait(4)
        return []

    service.catalog.search = search
    query = service.seed.observe(PARAMS)
    service.seed.ready(query, {"searchResult3": {"artist": [1]}})
    assert entered.wait(1)
    try:
        for i in range(200):
            query = service.seed.observe(PARAMS | {"query": str(i)})
            service.seed.ready(query, {"searchResult3": {"artist": [1]}})
        assert service.pool._work_queue.qsize() == 0
    finally:
        release.set()
    service.pool.submit(lambda: None).result(timeout=3)
    service.pool.submit(lambda: None).result(timeout=3)
    assert calls == ["fixture", "199"]


def test_actual_429_seed_sets_shared_cooldown_and_fallback_retry(bridge, monkeypatch):
    import io
    from urllib.error import HTTPError

    server, _ = bridge
    service, _, _, _ = prepare(server, monkeypatch)
    service.catalog = Catalog(limiter=service.core.catalog.limiter)
    calls = []

    def opened(url, timeout):
        calls.append(url)
        raise HTTPError(url, 429, "limited", {"Retry-After": "60"}, io.BytesIO())

    monkeypatch.setattr("apple_music_player.catalog.urlopen", opened)
    service.seed.run()
    assert service.catalog.not_before > time.monotonic() + 55
    service.enrich(ID, ARTIST)
    after, reason = service.check(ID)
    assert reason == "release-deferred" and after > time.time() + 55
    assert len(calls) == 1


def test_sqlite_writer_contention_does_not_hold_query_generation(bridge, monkeypatch):
    server, _ = bridge
    service, query, _, _ = prepare(server, monkeypatch, [ready_album()])
    entered = threading.Event()
    original = service.store_albums

    def store(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "store_albums", store)
    with service.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        job = service.pool.submit(service.seed.run)
        assert entered.wait(1)
        start = time.monotonic()
        service.seed.observe(PARAMS | {"query": "new"})
        assert time.monotonic() - start < 0.1
        assert query.cancelled.is_set()
        job.result(timeout=1)
    assert service.selection(ARTIST) is None
    assert service.check(ID) == (0, "release-failed")


@pytest.mark.parametrize("coverage", [0, 2])
def test_http_busy_retry_budget_survives_slow_seed(bridge, monkeypatch, coverage):
    """Actual HTTP + deterministic model of Go's [1,2,4]s provider / 3 UI retries."""
    from dataclasses import replace

    from test_bridge import request
    from test_bridge_artwork import image_bytes, transport

    from apple_music_player.bridge import release_artwork

    server, _ = bridge
    service, _, _, clock = prepare(server, monkeypatch)
    server.actor.call("remember", [replace(ARTIST, id=str(i)) for i in range(1, 9)])
    base = clock.now
    monkeypatch.setattr(
        release_artwork,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, time=lambda: clock.now),
    )
    waiting, admit, fetching, finish = (threading.Event() for _ in range(4))

    def wait(*_):
        waiting.set()
        assert admit.wait(3)

    def search(*_args, **_kwargs):
        fetching.set()
        assert finish.wait(3)
        return [ready_album(id=str(100 + i), artist_id=str(i)) for i in range(1, coverage + 1)]

    service.seed.wait = wait
    service.catalog.search = search
    service.catalog.artist_albums = lambda i, *_: [ready_album(id=str(100 + int(i)), artist_id=i)]
    transport(monkeypatch, image_bytes())
    seed_job = service.pool.submit(service.seed.run)
    assert waiting.wait(1)
    for i in range(1, 5):
        status, headers, _ = request(
            server, "getCoverArt", {"id": f"artist-release:am:br:lossless:artist:{i}"}
        )
        assert status == 503 and headers["X-Artwork-Reason"] == "release-pending"
    assert len(service.pending) == 4
    observed = []
    completed = False

    def provider():
        budget = 7
        for attempt in range(4):
            status, headers, _ = request(
                server, "getCoverArt", {"id": "artist-release:am:br:lossless:artist:8"}
            )
            observed.append((clock.now - base, status, headers.get("X-Artwork-Reason")))
            if status == 200:
                return None
            assert headers["X-Artwork-Reason"] in {"release-busy", "release-pending"}
            retry = int(headers["Retry-After"])
            if attempt == 3:
                return clock.now + retry
            delay = max([1, 2, 4][attempt], retry)
            if delay > budget:
                return clock.now + delay
            budget -= delay
            clock.now += delay
            if completed:
                service.pool.submit(lambda: None).result(timeout=1)
        raise AssertionError("unreachable")

    try:
        retry_at = provider()
        assert observed == [
            (0, 503, "release-busy"),
            (3, 503, "release-busy"),
            (6, 503, "release-busy"),
        ]
        # Remaining admission budget + the catalog's 15s operation timeout.
        assert retry_at == base + 45
        clock.now = base + 29
        admit.set()
        assert fetching.wait(1)
        clock.now = base + 44
        finish.set()
        seed_job.result(timeout=1)
        service.pool.submit(lambda: None).result(timeout=1)
        completed = True
        clock.now = retry_at
        for _ in range(3):  # The existing UI's bounded deferred retry count.
            retry_at = provider()
            if retry_at is None:
                break
            clock.now = retry_at
        assert retry_at is None
        assert observed[-1][1] == 200
        assert service.get("am:br:lossless:artist:8").artist_id == "8"
    finally:
        admit.set()
        finish.set()
