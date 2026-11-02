from concurrent.futures import Future

import pytest

from apple_music_player.app import Window
from apple_music_player.catalog import Catalog
from apple_music_player.model import Album, PlayerError, Track

# Synthetic IDs, public metadata shape; no live API/account required.
ALBUM = {
    "wrapperType": "collection",
    "collectionType": "Album",
    "collectionId": 12345,
    "collectionName": "If You Leave",
    "artistName": "Daughter",
}
SONGS = [
    {
        "kind": "song",
        "trackId": 12346,
        "collectionId": 12345,
        "trackName": "Winter",
        "artistName": "Daughter",
    },
    {
        "kind": "song",
        "trackId": 12347,
        "collectionId": 12345,
        "trackName": "Smother",
        "artistName": "Daughter",
    },
]


def test_album_search_contract_and_lookup(monkeypatch):
    catalog = Catalog()
    calls = []

    def request(endpoint, **params):
        calls.append((endpoint, params))
        return [ALBUM, *SONGS]

    monkeypatch.setattr(catalog, "request", request)
    albums = catalog.search("daughter if you leave", "gb", "album")
    assert albums == [Album("gb", "12345", "If You Leave — Daughter")]
    assert calls[-1] == (
        "search",
        {
            "term": "daughter if you leave",
            "country": "gb",
            "media": "music",
            "entity": "album",
            "limit": 50,
        },
    )
    tracks = catalog.album(albums[0].id, albums[0].country)
    assert [track.id for track in tracks] == ["12346", "12347"]
    assert all(isinstance(track, Track) and track.country == "gb" for track in tracks)
    assert calls[-1] == ("lookup", {"id": "12345", "country": "gb", "entity": "song", "limit": 200})
    assert catalog.albums([dict(ALBUM, collectionId="bad"), *SONGS], "gb") == []
    with pytest.raises(PlayerError):
        catalog.search("daughter", "invalid", "album")


def test_album_ui_open_tracks_queue_and_stale_results(app, tmp_path, monkeypatch):
    from conftest import wait_until

    window = Window(tmp_path / "cache")
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    calls = []

    def request(endpoint, **params):
        calls.append((endpoint, params))
        return [ALBUM] if endpoint == "search" else [ALBUM, *SONGS]

    monkeypatch.setattr(window.catalog, "request", request)
    window.search.setText("daughter if you leave")
    window.country.setText("gb")
    window.search_kind.setCurrentIndex(1)
    window.search_catalog()
    wait_until(lambda: (window.tick(), window.result_list.count() == 1)[1])
    assert window.result_list.item(0).text() == "If You Leave — Daughter"
    assert "1 album" in window.catalog_status.text()
    assert window.selected_button.text() == "Open album"
    assert not window.all_button.isEnabled()
    window.add_all()  # Defensive: collection IDs can never enter the song queue.
    assert not window.controller.queue
    window.result_list.setCurrentRow(0)
    window.selected_button.click()
    wait_until(lambda: (window.tick(), window.result_list.count() == 2)[1])
    assert "If You Leave — Daughter" in window.catalog_status.text()
    assert window.all_button.text() == "Queue album"
    window.all_button.click()
    assert [entry.track.id for entry in window.controller.queue] == ["12346", "12347"]
    assert calls[-1][1]["country"] == "gb"

    stale = Future()
    window.catalog_future = (window.generation, stale)
    window.search_kind.setCurrentIndex(0)
    stale.set_result([Album("gb", "99999", "Stale album")])
    window.tick()
    assert window.results == [] and window.result_list.count() == 0
    assert not window.all_button.isEnabled()

    monkeypatch.setattr(window.catalog, "request", lambda *args, **kwargs: [])
    window.search_kind.setCurrentIndex(1)
    window.search_catalog()
    wait_until(lambda: (window.tick(), window.catalog_future is None)[1])
    assert "0 albums" in window.catalog_status.text()
    assert not window.selected_button.isEnabled()
    window.close()
