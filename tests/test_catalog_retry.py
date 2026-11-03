import io
import json
import time
from urllib.error import HTTPError

import pytest
from conftest import wait_until
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from apple_music_player.app import Window
from apple_music_player.catalog import Catalog, CatalogDeferred
from apple_music_player.model import Album

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


def pump(app, window, predicate):
    return lambda: (app.processEvents(), window.tick(), predicate())[2]


def test_real_qt_spacing_search_to_album_keeps_results(app, tmp_path, monkeypatch):
    calls = []

    def open_fixture(url, **kwargs):
        calls.append((time.monotonic(), url))
        rows = [ALBUM, SONG] if "/lookup?" in url else [ALBUM]
        return io.BytesIO(json.dumps({"results": rows}).encode())

    monkeypatch.setattr("apple_music_player.catalog.urlopen", open_fixture)
    window = Window(tmp_path / "cache")
    window.show()
    try:
        assert window.search_kind.currentData() == "musicArtist"
        assert window.country.text() == "br"
        window.search_kind.setCurrentIndex(window.search_kind.findData("album"))
        QTest.keyClicks(window.search, "daughter if you leave")
        QTest.keyClick(window.search, Qt.Key_Return)
        wait_until(pump(app, window, lambda: len(window.results) == 1))
        window.result_list.setCurrentRow(0)
        QTest.mouseClick(window.selected_button, Qt.LeftButton)
        wait_until(pump(app, window, window.catalog_timer.isActive))
        assert window.result_list.item(0).text() == "If You Leave — Daughter"
        assert "Waiting" in window.catalog_status.text()
        QTest.qWait(3100)
        wait_until(pump(app, window, lambda: window.all_button.text() == "Queue album"))
        assert window.results[0].title == "Smother — Daughter"
        assert len(calls) == 2 and calls[1][0] - calls[0][0] >= 3
    finally:
        window.close()


def test_new_intent_and_close_cancel_deferred_work(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    calls = []

    def search(query, *args):
        calls.append(query)
        if query != "new":
            raise CatalogDeferred(0.1)
        return [Album("br", "12345", "Newest album")]

    monkeypatch.setattr(window.catalog, "search", search)
    try:
        window.search.setText("old")
        QTest.keyClick(window.search, Qt.Key_Return)
        wait_until(pump(app, window, window.catalog_timer.isActive))
        window.search.selectAll()
        QTest.keyClicks(window.search, "new")
        QTest.keyClick(window.search, Qt.Key_Return)
        wait_until(pump(app, window, lambda: bool(window.results)))
        QTest.qWait(150)
        assert calls == ["old", "new"]
        assert window.results[0].title == "Newest album"
        window.search.setText("closing")
        QTest.keyClick(window.search, Qt.Key_Return)
        wait_until(pump(app, window, window.catalog_timer.isActive))
        assert window.results[0].title == "Newest album"
        window.close()
        QTest.qWait(150)
        assert calls == ["old", "new", "closing"]
        assert not window.catalog_timer.isActive()
    finally:
        if not window.closing:
            window.close()


@pytest.mark.parametrize("delay,attempts", [(0.01, 3), (61, 1)])
def test_http_rate_retry_is_bounded_in_ui(app, tmp_path, monkeypatch, delay, attempts):
    window = Window(tmp_path / "cache")
    calls = []

    def limited(*args):
        calls.append(True)
        raise CatalogDeferred(delay, rate_limited=True)

    monkeypatch.setattr(window.catalog, "search", limited)
    try:
        window.search.setText("limited")
        window.search_catalog()
        wait_until(pump(app, window, lambda: "persists" in window.catalog_status.text()))
        assert len(calls) == attempts and not window.catalog_timer.isActive()
    finally:
        window.close()


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


@pytest.mark.parametrize("saved", ["us", "gb"])
def test_saved_storefront_is_not_migrated_to_br(app, tmp_path, saved):
    root = tmp_path / "cache"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps({"storefront": saved}))
    window = Window(root)
    try:
        assert window.country.text() == saved
        assert window.search_kind.currentData() == "musicArtist"
    finally:
        window.close()
