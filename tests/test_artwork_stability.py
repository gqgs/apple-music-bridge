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
