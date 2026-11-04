import io
from urllib.error import HTTPError

import pytest

from apple_music_player.catalog import Catalog, CatalogDeferred

ALBUM = {
    "wrapperType": "collection",
    "collectionType": "Album",
    "collectionId": 12345,
    "collectionName": "If You Leave",
    "artistName": "Daughter",
}
SONG = {
    "kind": "song",
    "trackId": 12346,
    "collectionId": 12345,
    "trackName": "Smother",
    "artistName": "Daughter",
}


@pytest.mark.parametrize(
    "header,seconds", [("5", 5), ("bad", 3), ("600", 600), ("Thu, 01 Jan 1970 00:16:50 GMT", 10)]
)
def test_429_retry_after_and_local_cooldown_are_distinct(monkeypatch, header, seconds):
    monkeypatch.setattr("apple_music_player.catalog.time.monotonic", lambda: 100)
    monkeypatch.setattr("apple_music_player.catalog.time.time", lambda: 1000)
    calls = []

    def limited(*args, **kwargs):
        calls.append(True)
        raise HTTPError(
            "https://itunes.apple.com/search",
            429,
            "withheld",
            {"Retry-After": header},
            io.BytesIO(b"PRIVATE_BODY"),
        )

    monkeypatch.setattr("apple_music_player.catalog.urlopen", limited)
    catalog = Catalog()
    with pytest.raises(CatalogDeferred) as error:
        catalog.search("daughter", "br", "album")
    assert error.value.rate_limited and error.value.seconds == seconds
    with pytest.raises(CatalogDeferred) as cooldown:
        catalog.search("daughter", "br", "album")
    assert not cooldown.value.rate_limited and cooldown.value.seconds == seconds
    assert len(calls) == 1
