import threading

from conftest import wait_until
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from apple_music_player.app import Window
from apple_music_player.model import Album, Artist, Track


def test_back_restores_search_artist_album_context_without_network(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    artists = [Artist("br", str(n), f"Fixture {n}") for n in range(1, 101)]
    albums = [
        Album("br", str(n + 1000), f"Record {n}", "Fixture", "21", f"Record {n}")
        for n in range(1, 101)
    ]
    tracks = [
        Track(
            "br",
            "5001",
            "Start",
            "1021",
            album_title="Record 21",
            album_artist="Fixture",
            album_artist_id="21",
        )
    ]
    calls = []

    def search(*args):
        calls.append("search")
        return artists

    def releases(*args):
        calls.append("artist")
        return albums

    def album(*args):
        calls.append("album")
        return tracks

    monkeypatch.setattr(window.catalog, "search", search)
    monkeypatch.setattr(window.catalog, "artist_albums", releases)
    monkeypatch.setattr(window.catalog, "album", album)

    def settle():
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])

    try:
        window.show()
        window.resize(1000, 480)
        window.search.setText("fixture query")
        window.search_catalog()
        settle()
        window.result_list.setCurrentRow(20)
        window.result_list.scrollToItem(window.result_list.item(20))
        app.processEvents()
        search_scroll = window.result_list.verticalScrollBar().value()
        window.selected_button.click()
        settle()
        window.result_list.setCurrentRow(21)
        window.result_list.scrollToItem(window.result_list.item(21))
        app.processEvents()
        release_scroll = window.result_list.verticalScrollBar().value()
        window.selected_button.click()
        settle()
        assert window.album_header.album
        assert window.back_button.isEnabled()
        QTest.mouseClick(window.back_button, Qt.LeftButton)
        app.processEvents()
        assert window.results == albums
        assert window.result_list.currentRow() == 21
        assert window.result_list.verticalScrollBar().value() == release_scroll
        window.back_button.setFocus()
        QTest.keyClick(window.back_button, Qt.Key_Left, Qt.AltModifier)
        wait_until(lambda: (app.processEvents(), window.results == artists)[1])
        app.processEvents()
        assert window.results == artists
        assert window.result_list.currentRow() == 20
        assert window.result_list.verticalScrollBar().value() == search_scroll
        assert window.search.text() == "fixture query"
        assert window.search_kind.currentData() == "musicArtist"
        assert window.country.text() == "br"
        assert calls == ["search", "artist", "album"]
        assert not window.back_button.isEnabled()
        assert not window.controller.queue
    finally:
        window.close()


def test_back_during_loading_discards_late_result_and_repeated_clicks(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    entered, release = threading.Event(), threading.Event()

    def albums(*args):
        entered.set()
        assert release.wait(3)
        return [Album("gb", "99", "Stale")]

    monkeypatch.setattr(window.catalog, "artist_albums", albums)
    try:
        window.show()
        window.pages.setCurrentIndex(1)
        window.search.setText("original")
        window.open_artist(Artist("gb", "1", "Pending"))
        assert entered.wait(2)
        window.open_artist(Artist("gb", "2", "Newer pending"))
        assert len(window.view_history) == 1
        window.back_button.click()
        release.set()
        app.processEvents()
        window.tick()
        assert window.country.text() == "br"
        assert window.search.text() == "original"
        assert window.results == []
        assert window.catalog_future is None
        assert not window.catalog_timer.isActive()
        assert not window.view_history
    finally:
        release.set()
        window.close()


def test_back_to_home_and_new_search_starts_new_navigation(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    monkeypatch.setattr(window.catalog, "album", lambda *a: [])
    monkeypatch.setattr(window.catalog, "search", lambda *a: [])
    try:
        window.show()
        assert window.pages.currentIndex() == 0
        window.open_album(Album("br", "1", "Empty"))
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])
        window.back_button.click()
        assert window.pages.currentIndex() == 0
        window.open_album(Album("br", "1", "Empty"))
        window.search.setText("new search")
        window.search_catalog()
        assert not window.view_history and not window.back_button.isEnabled()
    finally:
        window.close()
