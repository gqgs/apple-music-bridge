import io
from datetime import date
from email.message import Message
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import wait_until
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from apple_music_player.app import Window
from apple_music_player.artwork import (
    BADGE_URL,
    MAX_BYTES,
    ArtworkCache,
    ArtworkRedirect,
    allowed_url,
    decode,
)
from apple_music_player.cache import Cache
from apple_music_player.catalog import Catalog, recent_releases
from apple_music_player.controller import Controller, Entry
from apple_music_player.library import History
from apple_music_player.model import Album, PlayerError, Track

URL = "https://is1-ssl.mzstatic.com/image/thumb/fixture/100x100bb.jpg"
TRACK = Track(
    "br",
    "101",
    "Still — Fixture",
    "201",
    "Fixture",
    "301",
    "Quiet Places",
    URL,
    "2026-10-01T00:00:00Z",
)


def png(width=100, height=100):
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor("#537780"))
    data = QBuffer()
    data.open(QIODevice.WriteOnly)
    assert image.save(data, "PNG")
    return bytes(data.data())


@pytest.mark.parametrize(
    "url",
    [
        "http://is1-ssl.mzstatic.com/x",
        "https://is1-ssl.mzstatic.com.evil.test/x",
        "https://user:password@is1-ssl.mzstatic.com/x",
        "https://127.0.0.1/x",
        "file:///tmp/x",
        "https://is1-ssl.mzstatic.com:1234/x",
        "https://is1-ssl.mzstatic.com/x#fragment",
    ],
)
def test_unsafe_artwork_urls(url):
    assert not allowed_url(url)


def test_artwork_validation_redirect_and_dimensions():
    assert allowed_url(URL) and allowed_url(BADGE_URL)
    with pytest.raises(ValueError):
        ArtworkRedirect().redirect_request(None, None, 302, "", {}, "http://localhost/x")
    for payload in (b"not an image", b"x" * (MAX_BYTES + 1), png(2049, 1)):
        with pytest.raises(ValueError):
            decode(payload)
    assert decode(png()).size().width() == 100


def test_art_cache_corruption_limit_symlink_and_restart(tmp_path, monkeypatch):
    payload = png()
    calls = []

    def opened(request, **kwargs):
        calls.append(request.full_url)
        result = io.BytesIO(payload)
        result.url = request.full_url
        result.headers = Message()
        result.headers["Content-Type"] = "image/png"
        return result

    monkeypatch.setattr(
        "apple_music_player.artwork.build_opener", lambda *_: SimpleNamespace(open=opened)
    )
    cache = ArtworkCache(tmp_path / "art", limit=len(payload) * 2)
    path = cache.root / (sha256(URL.encode()).hexdigest() + ".img")
    path.write_bytes(b"broken")
    assert not cache.load(URL).isNull()
    assert not cache.load(URL).isNull() and len(calls) == 1
    for n in range(3):
        assert not cache.load(URL + "?v=" + str(n)).isNull()
    assert sum(p.stat().st_size for p in cache.root.glob("*.img")) <= cache.limit
    orphan = cache.root / ".part-fixture"
    orphan.write_bytes(b"orphan")
    ArtworkCache(cache.root, cache.limit)
    assert not orphan.exists()
    path.unlink(missing_ok=True)
    outside = tmp_path / "outside"
    outside.write_bytes(payload)
    path.symlink_to(outside)
    assert cache.load(URL).isNull()
    assert outside.read_bytes() == payload


def test_history_restart_dedupe_clear_and_media_eviction_independence(tmp_path):
    history = History(tmp_path / "library", limit=2)
    history.record(TRACK)
    history.record(TRACK)
    assert len(history.albums("br")) == 1 and history.artists("br") == ["301"]
    cache = Cache(tmp_path / "media-cache", 0)
    cache.evict()
    cache.close()
    history.close()
    history = History(tmp_path / "library", limit=2)
    assert history.albums("br")[0].id == "201" and not history.albums("us")
    for n in range(3):
        history.record(Track("br", str(n), "Fixture", str(400 + n)))
    assert len(history.albums("br")) == 2
    history.clear()
    history.close()
    history = History(tmp_path / "library")
    assert not history.albums("br")
    history.close()


def test_history_only_after_verified_advancing_playback(tmp_path):
    history = History(tmp_path / "library")
    player = Mock()
    player.poll.return_value = []
    player.properties = {"pause": False, "time-pos": 0}
    player.output_verified = False
    controller = Controller(Cache(tmp_path / "cache", 1000), player)
    controller.on_played = history.record
    controller.add(TRACK, "lossless")
    assert not history.albums("br")
    controller.current = controller.queue[0]
    controller.current.state = "Starting"
    controller.tick()
    assert not history.albums("br")
    player.output_verified = True
    controller.tick()
    assert not history.albums("br")
    player.properties["time-pos"] = 0.1
    player.properties["pause"] = True
    controller.tick()
    assert not history.albums("br")
    player.properties["pause"] = False
    controller.tick()
    assert len(history.albums("br")) == 1
    history.clear()
    controller.tick()
    assert not history.albums("br")  # Clear does not immediately reinsert the current play.
    controller.current = Entry(TRACK, "lossless", "Starting")
    player.poll.side_effect = PlayerError("Synthetic playback failure")
    controller.tick()
    assert not history.albums("br")
    controller.close()
    history.close()


def test_recent_release_dates_and_artist_contract(monkeypatch):
    albums = [
        Album("br", str(i), "Fixture", release_date=day)
        for i, day in enumerate(["2026-10-01", "2026-11-03", "2020-01-01", "", "2026-09-01"])
    ]
    assert [a.id for a in recent_releases(albums + albums, date(2026, 11, 2))] == ["0", "4"]
    catalog = Catalog()
    request = Mock(return_value=[])
    monkeypatch.setattr(catalog, "request", request)
    assert catalog.artist_albums("301", "br") == []
    request.assert_called_once_with("lookup", id="301", country="br", entity="album", limit=200)
    assert catalog.artist_albums("", "br") == []
    assert not recent_releases([])


def test_qt_search_album_history_home_clear_and_stale_navigation(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    # Neutral synthetic image loader; never contact Apple in UI tests.
    monkeypatch.setattr(window.artwork.cache, "load", lambda url: QImage())
    rows = [
        dict(
            wrapperType="collection",
            collectionType="Album",
            collectionId=201,
            collectionName="Quiet Places",
            artistName="Fixture",
            artistId=301,
        ),
        dict(
            kind="song",
            trackId=101,
            collectionId=201,
            collectionName="Quiet Places",
            artistName="Fixture",
            artistId=301,
            trackName="Still",
        ),
    ]
    monkeypatch.setattr(window.catalog, "request", lambda *args, **kwargs: rows)
    window.show()
    try:
        assert window.pages.currentIndex() == 0
        assert not window.recent_shelf.cards
        assert "Play" in window.recent_shelf.note.text()
        window.pages.setCurrentIndex(1)
        QTest.keyClicks(window.search, "quiet places")
        QTest.keyClick(window.search, Qt.Key_Return)
        wait_until(lambda: (app.processEvents(), window.tick(), bool(window.results))[2])
        assert window.detail_card.album.id == "201"
        window.result_list.setCurrentRow(0)
        QTest.mouseClick(window.selected_button, Qt.LeftButton)
        wait_until(
            lambda: (app.processEvents(), window.tick(), window.all_button.text() == "Queue album")[
                2
            ]
        )
        QTest.mouseClick(window.all_button, Qt.LeftButton)
        assert not window.history.albums("br")
        # The separate controller regression verifies when this callback is emitted.
        window.controller.on_played(window.controller.queue[0].track)
        window.pages.setCurrentIndex(0)
        window.tick()
        assert window.recent_shelf.cards[0].album.id == "201"
        window.country.setText("gb")
        window.tick()
        assert not window.recent_shelf.cards
        window.country.setText("br")
        window.tick()
        window.clear_history()
        assert not window.recent_shelf.cards
        assert not window.release_shelf.cards
    finally:
        window.close()
