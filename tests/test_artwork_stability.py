import threading

import pytest
from conftest import wait_until
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from apple_music_player.app import Window
from apple_music_player.artwork import BADGE_URL

URL = "https://is1-ssl.mzstatic.com/image/thumb/fixture/100x100bb.jpg"


@pytest.mark.parametrize("mode", ["album", "song", "musicArtist"])
@pytest.mark.parametrize("success", [True, False])
def test_search_idle_and_async_artwork_do_not_churn_rows(app, tmp_path, monkeypatch, mode, success):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    release = threading.Event()
    image = QImage(100, 100, QImage.Format_RGB32)
    image.fill(QColor("#536e75"))

    def load(url):
        assert release.wait(4)
        return image if success else QImage()

    monkeypatch.setattr(window.artwork.cache, "load", load)
    rows = [
        dict(
            wrapperType="artist"
            if mode == "musicArtist"
            else "collection"
            if mode == "album"
            else "track",
            collectionType="Album",
            kind="song",
            artistId=n,
            artistName=f"Faye fixture {n}",
            collectionId=n + 100,
            collectionName=f"Album {n}",
            trackId=n + 200,
            trackName=f"Song {n}",
            artworkUrl100=URL,
        )
        for n in range(1, 41)
    ]
    monkeypatch.setattr(window.catalog, "request", lambda *a, **k: rows)
    changes = []
    window.result_list.model().dataChanged.connect(lambda *a: changes.append(1))
    try:
        window.show()
        window.resize(1000, 480)
        window.search_kind.setCurrentIndex(window.search_kind.findData(mode))
        window.search.setText("faye")
        window.search_catalog()
        wait_until(lambda: (app.processEvents(), len(window.results) == 40)[1])
        window.result_list.setCurrentRow(20)
        window.result_list.scrollToItem(window.result_list.item(20))
        QTest.qWait(150)
        scroll = window.result_list.verticalScrollBar().value()
        card = window.detail_card
        changes.clear()
        QTest.qWait(450)  # Several real 100 ms timer cycles while art is pending.
        assert changes == []
        release.set()
        wait_until(lambda: (app.processEvents(), not window.artwork.pending)[1])
        # Badge completion may schedule the actual image on the next poll.
        QTest.qWait(350)
        wait_until(lambda: (app.processEvents(), not window.artwork.pending)[1])
        if mode != "musicArtist":
            assert BADGE_URL in window.artwork.images if success else window.artwork.failures
        changes.clear()
        icon_keys = [window.result_list.item(n).icon().cacheKey() for n in range(40)]
        QTest.qWait(550)
        assert changes == []
        assert icon_keys == [window.result_list.item(n).icon().cacheKey() for n in range(40)]
        assert window.result_list.currentRow() == 20
        assert window.result_list.verticalScrollBar().value() == scroll
        assert window.detail_card is card
        if card:
            before = card.cover.icon().cacheKey()
            QTest.qWait(250)
            assert card.cover.icon().cacheKey() == before
    finally:
        release.set()
        window.close()


@pytest.mark.parametrize("mode", ["album", "musicArtist"])
@pytest.mark.parametrize("success", [True, False])
def test_large_search_has_stable_geometry_and_bounded_artwork(
    app, tmp_path, monkeypatch, mode, success
):
    from collections import Counter

    from PySide6.QtCore import QEvent, QObject

    class Events(QObject):
        def __init__(self, parent):
            super().__init__(parent)
            self.counts = Counter()

        def eventFilter(self, watched, event):
            if event.type() in (QEvent.Resize, QEvent.Show, QEvent.Hide, QEvent.LayoutRequest):
                self.counts[event.type()] += 1
            return False

    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    loaded = []

    def load(url):
        loaded.append(url)
        image = QImage(100, 100, QImage.Format_RGB32) if success else QImage()
        if success:
            image.fill(QColor("#536e75"))
        return image

    monkeypatch.setattr(window.artwork.cache, "load", load)
    monkeypatch.setattr(
        window.catalog,
        "request",
        lambda *a, **k: [
            dict(
                wrapperType="artist" if mode == "musicArtist" else "collection",
                collectionType="Album",
                collectionId=n,
                artistId=n,
                collectionName=f"Album {n}",
                artistName=f"Faye fixture {n}",
                artworkUrl100=URL.replace("fixture", f"fixture{n}"),
            )
            for n in range(1, 201)
        ],
    )
    from types import SimpleNamespace

    window.setup_service = SimpleNamespace(
        stop_requested=False,
        error="",
        runtime=SimpleNamespace(state="Ready · fixture"),
        close=lambda: None,
    )
    events = Events(window)
    for widget in (window.centralWidget(), window.badge_strip, window.result_list.viewport()):
        widget.installEventFilter(events)
    try:
        window.show()
        window.resize(1000, 768)
        window.search_kind.setCurrentIndex(window.search_kind.findData(mode))
        window.search.setText("faye")
        window.search_catalog()
        wait_until(lambda: (app.processEvents(), len(window.results) == 200)[1])
        QTest.qWait(800)
        events.counts.clear()
        before = len(loaded)
        QTest.qWait(1200)
        assert not events.counts
        assert len(loaded) == before < 20
        window.result_list.setCurrentRow(150)
        window.result_list.scrollToItem(window.result_list.item(150))
        window.resize(950, 480)
        QTest.qWait(800)
        events.counts.clear()
        before = len(loaded)
        QTest.qWait(1200)
        assert not events.counts
        assert len(loaded) == before < 30
        assert window.result_list.currentRow() == 150
        assert window.badge_strip.isVisible() == (success and mode == "album")
    finally:
        window.close()


def test_shared_badge_survives_bounded_image_eviction(tmp_path):
    from concurrent.futures import Future

    from apple_music_player.artwork import Artwork

    artwork = Artwork(tmp_path / "artwork")
    try:
        badge = QImage(1, 1, QImage.Format_RGB32)
        artwork.images[BADGE_URL] = badge
        for index in range(90):
            future = Future()
            future.set_result(QImage(1, 1, QImage.Format_RGB32))
            artwork.pending[URL.replace("fixture", f"fixture{index}")] = future
        artwork.poll()
        assert len(artwork.images) == 64
        assert artwork.get(BADGE_URL).cacheKey() == badge.cacheKey()
        assert not artwork.pending
    finally:
        artwork.close()
