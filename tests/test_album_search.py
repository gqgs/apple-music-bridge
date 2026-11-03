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
    assert albums == [
        Album(
            "gb", "12345", "If You Leave — Daughter", artist="Daughter", album_title="If You Leave"
        )
    ]
    assert calls[-1] == (
        "search",
        {
            "term": "daughter if you leave",
            "country": "gb",
            "media": "music",
            "entity": "album",
            "limit": 200,
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
    stale.set_running_or_notify_cancel()
    window.catalog_future = (window.generation, stale)
    window.search_kind.setCurrentIndex(0)
    stale.set_result([Album("gb", "99999", "Stale album")])
    window.tick()
    assert [track.id for track in window.results] == ["12346", "12347"]
    assert window.result_list.count() == 2

    monkeypatch.setattr(window.catalog, "request", lambda *args, **kwargs: [])
    window.search_kind.setCurrentIndex(1)
    window.search_catalog()
    wait_until(lambda: (window.tick(), window.catalog_future is None)[1])
    assert "0 albums" in window.catalog_status.text()
    assert not window.selected_button.isEnabled()
    window.close()


def test_br_direct_song_adopts_and_persists_storefront(app, tmp_path, monkeypatch):
    import json

    from conftest import wait_until

    window = Window(tmp_path / "cache")
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    window.country.setText("us")
    window.results = [Track("us", "99999", "Old US result", "88888")]
    window.result_list.addItem("Old US result")
    stale = Future()
    stale.set_running_or_notify_cancel()
    window.catalog_future = (window.generation, stale)
    window.url.setText("https://music.apple.com/br/song/still/593614986?")
    window.add_url()
    assert window.country.text() == "br"
    assert window.results == [] and window.result_list.count() == 0
    assert window.controller.queue[0].track.url == "https://music.apple.com/br/song/593614986"
    assert json.loads(window.settings_path.read_text())["storefront"] == "br"
    stale.set_result([Track("us", "99999", "Stale US result")])
    window.tick()
    assert window.results == []
    assert "BR" in window.queue_list.item(0).text()

    calls = []

    def request(endpoint, **params):
        calls.append((endpoint, params))
        # Synthetic collection ID; the working song ID comes from the supplied BR URL.
        return [
            {
                "kind": "song",
                "trackId": 593614986,
                "collectionId": 12345,
                "trackName": "Still",
                "artistName": "Daughter",
            }
        ]

    monkeypatch.setattr(window.catalog, "request", request)
    window.search_kind.setCurrentIndex(0)
    window.search.setText("daughter still")
    window.search_catalog()
    wait_until(lambda: (window.tick(), window.catalog_future is None)[1])
    assert calls[-1][1]["country"] == "br"
    window.result_list.setCurrentRow(0)
    window.browse_album()
    wait_until(lambda: (window.tick(), window.catalog_future is None)[1])
    assert calls[-1] == (
        "lookup",
        {"id": "12345", "country": "br", "entity": "song", "limit": 200},
    )
    assert window.results[0].url == window.controller.queue[0].track.url
    window.close()

    reopened = Window(tmp_path / "cache")
    try:
        assert reopened.country.text() == "br"
        reopened.country.setText("gb")
        reopened.country.editingFinished.emit()
        assert json.loads(reopened.settings_path.read_text())["storefront"] == "gb"
        reopened.url.setText("https://untrusted.example/br/song/123")
        reopened.add_url()
        assert reopened.country.text() == "gb"  # Invalid URLs cannot change settings.
    finally:
        reopened.close()


def test_qt_backend_storefront_choice_search_and_queue(app, tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from conftest import wait_until
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "settings.json").write_text(json.dumps({"storefront": "us"}))
    window = Window(cache)
    window.startup_attempted = True
    window.controller.backend = "lite"
    window.controller.runtime = SimpleNamespace(regions=("br",))
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    calls = []

    def request(endpoint, **params):
        calls.append((endpoint, params))
        return [ALBUM] if endpoint == "search" else [ALBUM, *SONGS]

    monkeypatch.setattr(window.catalog, "request", request)
    window.pages.setCurrentIndex(1)
    window.show()
    window.tick()
    app.processEvents()
    try:
        assert "Browsing US" in window.storefront_notice.text()
        assert "backend reports BR" in window.storefront_notice.text()
        assert window.use_backend_country.isVisible()
        # A stale US result cannot queue even if it somehow survives UI invalidation.
        window.controller.add(Track("us", "99999", "Smother — Daughter"), "lossless")
        assert not window.controller.queue
        assert "Track is US" in window.controller.error
        QTest.mouseClick(window.use_backend_country, Qt.LeftButton)
        assert window.country.text() == "br"
        assert json.loads(window.settings_path.read_text())["storefront"] == "br"
        QTest.mouseClick(window.search, Qt.LeftButton)
        QTest.keyClicks(window.search, "daughter if you leave")
        window.search_kind.setCurrentIndex(1)
        QTest.keyClick(window.search, Qt.Key_Return)
        wait_until(lambda: (app.processEvents(), window.tick(), len(window.results) == 1)[2])
        assert calls[-1][1]["country"] == "br"
        window.result_list.setCurrentRow(0)
        QTest.mouseClick(window.selected_button, Qt.LeftButton)
        wait_until(lambda: (app.processEvents(), window.tick(), len(window.results) == 2)[2])
        QTest.mouseClick(window.all_button, Qt.LeftButton)
        assert [entry.track.country for entry in window.controller.queue] == ["br", "br"]
        assert calls[-1][1]["country"] == "br"
        # Editing storefront persists on text change, without relying on focus loss.
        window.country.setFocus()
        window.country.selectAll()
        QTest.keyClicks(window.country, "us")
        assert not window.results
        assert json.loads(window.settings_path.read_text())["storefront"] == "us"
    finally:
        window.close()


def test_fresh_storefront_uses_only_unambiguous_backend_region(app, tmp_path, monkeypatch):
    from types import SimpleNamespace

    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    window.controller.backend = "lite"
    window.controller.runtime = SimpleNamespace(regions=())
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    try:
        window.tick()
        assert window.country.text() == "br"  # Explicit requested default.
        window.country.clear()
        window.controller.runtime.regions = ("br", "us")
        window.tick()
        assert window.country.text() == ""
        window.controller.runtime.regions = ("br",)
        window.tick()
        assert window.country.text() == "br"
    finally:
        window.close()


def test_faye_album_query_uses_full_public_limit_and_direct_lookup(monkeypatch):
    from apple_music_player.model import parse_url

    catalog = Catalog()
    calls = []

    def request(endpoint, **params):
        calls.append((endpoint, params))
        return [dict(ALBUM, collectionId=n, collectionName=f"Fixture {n}") for n in range(60)]

    monkeypatch.setattr(catalog, "request", request)
    assert len(catalog.search("faye webster Atlanta Millionaires Club", "br", "album")) == 60
    assert calls[-1][1]["limit"] == 200
    assert calls[-1][1]["term"] == "faye webster Atlanta Millionaires Club"
    assert calls[-1][1]["country"] == "br"
    assert parse_url("https://music.apple.com/br/album/atlanta-millionaires-club/1452886606") == (
        "br",
        "album",
        "1452886606",
    )


def test_album_order_filters_other_collections_and_preserves_unnumbered(monkeypatch):
    catalog = Catalog()
    rows = [
        dict(SONGS[0], trackId=3, discNumber=2, trackNumber=1),
        dict(SONGS[0], trackId=2, discNumber=1, trackNumber=2),
        dict(SONGS[0], trackId=1, discNumber=1, trackNumber=1),
        dict(SONGS[0], trackId=9, collectionId=999),
    ]
    monkeypatch.setattr(catalog, "request", lambda *a, **k: rows)
    assert [t.id for t in catalog.album("12345", "br")] == ["1", "2", "3"]
    rows[0].pop("trackNumber")
    assert [t.id for t in catalog.album("12345", "br")] == ["3", "2", "1"]


def test_play_album_ui_loading_empty_error_and_repeat(app, tmp_path, monkeypatch):
    from conftest import wait_until

    window = Window(tmp_path / "cache")
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    monkeypatch.setattr(window.catalog, "request", lambda *a, **k: [ALBUM, *SONGS])
    old = Track("br", "999", "Previous")
    window.controller.add(old, "lossless")
    try:
        assert not window.play_album_button.isEnabled()
        window.open_album(Album("br", "12345", "Fixture album"))
        assert not window.play_album_button.isEnabled()
        wait_until(lambda: (window.tick(), window.play_album_button.isEnabled())[1])
        window.play_album_button.click()
        window.play_album_button.click()
        assert [e.track.id for e in window.controller.queue] == ["12346", "12347"]
        window.all_button.click()
        assert len(window.controller.queue) == 2  # Append deduplicates; it does not restart.
        window.open_album(Album("br", "987", "Next album"))
        assert not window.play_album_button.isEnabled()
        window.play_album()
        assert len(window.controller.queue) == 2
        wait_until(lambda: (window.tick(), window.catalog_future is None)[1])
        assert not window.play_album_button.isEnabled()  # Empty album lookup.

        def fail(*a, **k):
            raise PlayerError("Fixture catalog failure")

        monkeypatch.setattr(window.catalog, "request", fail)
        window.open_album(Album("br", "12345", "Fixture album"))
        wait_until(lambda: (window.tick(), window.catalog_future is None)[1])
        assert not window.play_album_button.isEnabled()
        assert "failure" in window.catalog_status.text()
        assert [e.track.id for e in window.controller.queue] == ["12346", "12347"]
    finally:
        window.close()
