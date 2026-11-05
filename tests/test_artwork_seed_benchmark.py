"""Offline actual-HTTP comparison; virtual scheduling, not a live coverage claim.

The modeled client requests cards in display order, waits for the single artwork
worker after a pending response, and retries immediately. It does not model Go's
parallelism or backoff. Synthetic upstream and CDN responses use real parsers,
SQLite retention, HTTP handlers and image decoding. Production spacing is intact.
"""

import io
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from test_artwork_seed import PARAMS, Clock
from test_bridge import bridge as _bridge
from test_bridge import payload, request
from test_bridge_artwork import URL, image_bytes, transport

from apple_music_player import catalog
from apple_music_player.bridge import artwork_seed, release_artwork

bridge = _bridge


@pytest.mark.parametrize("coverage", [0, 1, 8, 16, 32])
@pytest.mark.parametrize("latency", [0, 2])
@pytest.mark.parametrize("seeded", [False, True])
def test_actual_http_virtual_coverage_matrix(bridge, monkeypatch, coverage, latency, seeded):
    compare(bridge, monkeypatch, coverage, latency, seeded)


def compare(bridge, monkeypatch, coverage, latency, seeded, count=32, virtual=True):
    server, _ = bridge
    service = server.release_art
    clock = Clock() if virtual else WallClock()
    start = clock.now
    if virtual:
        timers = SimpleNamespace(monotonic=clock.monotonic, time=lambda: clock.now)
        monkeypatch.setattr(catalog, "time", timers)
        monkeypatch.setattr(artwork_seed, "time", timers)
        monkeypatch.setattr(release_artwork, "time", timers)
        monkeypatch.setattr(service.seed, "wait", clock.wait)
        monkeypatch.setattr(service.stop, "wait", clock.wait)
    server.actor.core.catalog = catalog.Catalog()
    service.catalog = catalog.Catalog(limiter=server.actor.core.catalog.limiter)
    if not seeded:
        monkeypatch.setattr(service.seed, "ready", lambda *_: None)
    calls = []

    def album(i):
        return {
            "wrapperType": "collection",
            "collectionType": "Album",
            "artistId": i,
            "artistName": "Same name",
            "collectionId": i + 100,
            "collectionName": "Release",
            "artworkUrl100": URL,
            "releaseDate": "2021-01-01",
        }

    def opened(url, timeout):
        params = parse_qs(urlsplit(url).query)
        entity = params["entity"][0]
        calls.append((clock.now - start, entity, "id" in params))
        clock.wait(latency)
        if entity == "musicArtist":
            rows = [
                {"wrapperType": "artist", "artistId": i, "artistName": "Same name"}
                for i in range(1, count + 1)
            ]
        elif "id" in params:
            rows = [album(int(params["id"][0]))]
        else:
            rows = [album(i) for i in range(1, coverage + 1)]
        return io.BytesIO(json.dumps({"results": rows}).encode())

    monkeypatch.setattr(catalog, "urlopen", opened)
    transport(monkeypatch, image_bytes())
    real_start = time.monotonic()
    artists = payload(server, "search3", PARAMS)["searchResult3"]["artist"]
    text_ms = (time.monotonic() - real_start) * 1000
    assert len(artists) == count
    ready = []
    for artist in artists:
        status, _, body = request(server, "getCoverArt", {"id": artist["coverArt"]})
        if status == 503:
            service.pool.submit(lambda: None).result(timeout=15)
            status, _, body = request(server, "getCoverArt", {"id": artist["coverArt"]})
        assert status == 200 and body.startswith(b"\x89PNG")
        ready.append(clock.now - start)
    expected = count + 2 - coverage if seeded else count + 1
    assert len(calls) == expected
    assert all(b[0] - a[0] >= 3 - 1e-7 for a, b in zip(calls, calls[1:], strict=False))
    print(
        "SEED_BENCH "
        + json.dumps(
            dict(
                seeded=seeded,
                coverage=coverage,
                time_mode="virtual" if virtual else "wall",
                upstream_seconds=latency,
                requests=len(calls),
                text_real_ms=round(text_ms, 2),
                first_seconds=round(ready[0], 2),
                median_seconds=round((ready[count // 2 - 1] + ready[count // 2]) / 2, 2),
                last_seconds=round(ready[-1], 2),
            )
        )
    )


class WallClock:
    @property
    def now(self):
        return time.monotonic()

    def wait(self, seconds):
        time.sleep(seconds)


@pytest.mark.parametrize("seeded,coverage", [(False, 0), (True, 0), (True, 4)])
def test_actual_http_wall_clock_examples(bridge, monkeypatch, seeded, coverage):
    compare(bridge, monkeypatch, coverage, 0.05, seeded, count=4, virtual=False)
