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


def test_artwork_completion_cannot_repaint_old_navigation(app, tmp_path, monkeypatch):
    from concurrent.futures import Future

    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    # Completion is keyed by URL, never by an old row index/widget callback.
    future = Future()
    window.artwork.pending[URL] = future
    window.artwork.images[BADGE_URL] = decode(png())
    window.results = [TRACK.album]
    window.result_list.addItem(TRACK.album.title)
    window.result_list.setCurrentRow(0)
    window.country.setText("gb")
    future.set_result(decode(png()))
    window.tick()
    assert window.result_list.count() == 0
    assert not window.results
    window.close()


def test_release_artist_bound_and_navigation_cancels_deferred_work(app, tmp_path, monkeypatch):
    from apple_music_player.catalog import CatalogDeferred

    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    calls = []

    def deferred(artist, country):
        calls.append((artist, country))
        raise CatalogDeferred(3)

    monkeypatch.setattr(window.catalog, "artist_albums", deferred)
    for n in range(8):
        window.history.record(Track("br", str(n), "Fixture", str(n), artist_id=str(n)))
    window.update_home()
    window.refresh_releases()
    wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_timer.isActive())[2])
    assert len(window.history.artists("br")) == 5 and len(calls) == 1
    window.pages.setCurrentIndex(1)
    assert not window.catalog_timer.isActive() and window.release_context is None
    window.pages.setCurrentIndex(0)
    window.close()
    assert not window.catalog_timer.isActive() and window.artwork.closed


def test_artwork_failure_categories_manual_recovery_and_no_403_retry(tmp_path, monkeypatch):
    from urllib.error import HTTPError, URLError

    from apple_music_player.artwork import Artwork

    now = [100.0]
    monkeypatch.setattr("apple_music_player.artwork.time.monotonic", lambda: now[0])
    calls = []
    failure = [URLError(TimeoutError())]

    def opened(request, **kwargs):
        calls.append(request.full_url)
        if failure[0]:
            raise failure[0]
        result = io.BytesIO(png())
        result.url = request.full_url
        result.headers = Message()
        result.headers["Content-Type"] = "image/png"
        return result

    monkeypatch.setattr(
        "apple_music_player.artwork.build_opener", lambda *_: SimpleNamespace(open=opened)
    )
    art = Artwork(tmp_path / "art")
    try:
        art.get(BADGE_URL)
        art.pending[BADGE_URL].result(timeout=2)
        art.poll()
        assert "network" in art.status()
        assert not art.retryable()
        art.get(BADGE_URL)
        assert len(calls) == 1
        now[0] += 31
        assert art.retryable() == [BADGE_URL]
        failure[0] = None
        art.retry_failed()
        art.get(BADGE_URL)
        art.pending[BADGE_URL].result(timeout=2)
        art.poll()
        assert not art.get(BADGE_URL).isNull() and not art.status()
        assert len(calls) == 2
    finally:
        art.close()
    failure[0] = HTTPError(BADGE_URL, 403, "PRIVATE_RESPONSE", {}, None)
    art = Artwork(tmp_path / "denied")
    try:
        art.get(BADGE_URL)
        art.pending[BADGE_URL].result(timeout=2)
        art.poll()
        assert "401/403" in art.status() and "PRIVATE" not in art.status()
        for _ in range(5):
            now[0] += 1000
            art.retry_failed()
            art.get(BADGE_URL)
            art.poll()
        assert len(calls) == 3 and not art.retryable()
    finally:
        art.close()


def test_artwork_retry_limit_and_proxy_denial(tmp_path, monkeypatch):
    from urllib.error import URLError

    from apple_music_player.artwork import Artwork

    now = [100.0]
    monkeypatch.setattr("apple_music_player.artwork.time.monotonic", lambda: now[0])
    calls = []

    def opened(*args, **kwargs):
        calls.append(1)
        raise URLError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(
        "apple_music_player.artwork.build_opener", lambda *_: SimpleNamespace(open=opened)
    )
    cache = ArtworkCache(tmp_path / "proxy")
    assert cache.load(BADGE_URL).isNull()
    assert cache.failures[BADGE_URL] == "denied"
    art = Artwork(tmp_path / "retry")

    def transient(url):
        calls.append(1)
        art.cache.failures[url] = "server"
        return QImage()

    monkeypatch.setattr(art.cache, "load", transient)
    try:
        for attempt in range(3):
            art.get(BADGE_URL)
            art.pending[BADGE_URL].result(timeout=2)
            art.poll()
            now[0] += 31
            if attempt < 2:
                assert art.retryable()
                art.retry_failed()
        assert not art.retryable() and "limit reached" in art.status()
        art.retry_failed()
        art.get(BADGE_URL)
        assert len(calls) == 4
    finally:
        art.close()


def test_populated_home_fits_short_viewport_with_scroll_and_visible_transport(
    app, tmp_path, monkeypatch
):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    window.timer.stop()
    monkeypatch.setattr(window, "refresh_releases", lambda: None)
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    window.show()
    app.processEvents()
    albums = [Album("br", str(n), "Synthetic album", artist="Fixture") for n in range(3)]
    window.recent_shelf.set_albums(albums, "")
    window.release_shelf.set_albums(albums, "")
    # 480 logical pixels leaves desktop decoration space on a 768px screen at 150%.
    window.resize(1024, 480)
    app.processEvents()
    assert window.height() == 480
    assert window.home_scroll.verticalScrollBar().maximum() > 0
    assert window.pause_button.isVisible()
    assert window.pause_button.mapTo(window, window.pause_button.rect().bottomLeft()).y() < 480
    bar = window.home_scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    assert bar.value() > 0
    window.close()


def test_one_shared_badge_for_home_search_and_now_playing(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QPushButton

    from apple_music_player.browsing import AlbumCard, store_link

    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    window.timer.stop()
    monkeypatch.setattr(window, "refresh_releases", lambda: None)
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    present = [True]
    image = decode(png())  # Synthetic image only; no official badge retrieval.
    monkeypatch.setattr(window.artwork, "get", lambda url: image if present[0] else QImage())
    opened = []
    monkeypatch.setattr(
        "apple_music_player.app.QDesktopServices.openUrl", lambda url: opened.append(url.toString())
    )
    window.show()
    app.processEvents()
    album = TRACK.album
    window.recent_shelf.set_albums([album], "")
    window.release_shelf.set_albums([album], "")
    window.now_card = AlbumCard(album, window.artwork, window.open_album, compact=True)
    window.now_host.addWidget(window.now_card)
    window.resize(1024, 480)

    def verify():
        window.refresh_artwork()
        app.processEvents()
        badges = window.findChildren(QPushButton, "itunesStoreBadge")
        assert len(badges) == 1 and badges[0].isVisible()
        assert window.height() == 480
        strip_top = window.badge_strip.mapTo(window, QPoint()).y()
        strip_bottom = window.badge_strip.mapTo(window, window.badge_strip.rect().bottomLeft()).y()
        pages_bottom = window.pages.mapTo(window, window.pages.rect().bottomLeft()).y()
        assert 0 <= strip_top - pages_bottom <= 16
        assert strip_bottom < window.now_card.mapTo(window, QPoint()).y()
        assert window.pause_button.mapTo(window, window.pause_button.rect().bottomLeft()).y() < 480
        assert window.now_card.store.icon().isNull()
        assert window.now_card.store.text() == "View album in iTunes"
        window.store_badge.click()
        assert opened[-1] == store_link(window.badge_album)

    try:
        verify()
        assert window.home_scroll.verticalScrollBar().maximum() > 0
        for card in window.recent_shelf.cards + window.release_shelf.cards:
            assert card.store.icon().isNull() and card.store.text() == "View album in iTunes"
        window.pages.setCurrentIndex(1)
        window.results = [album]
        window.result_list.addItem(album.title)
        window.result_list.setCurrentRow(0)
        verify()
        assert window.detail_card.store.icon().isNull()
        window.results = [TRACK]
        window.result_list.clear()
        window.result_list.addItem(TRACK.title)
        window.result_list.setCurrentRow(0)
        verify()  # Album track detail + now playing share the same badge.
        window.pages.setCurrentIndex(2)
        verify()  # Now-playing artwork alone still has one adjacent badge.
        present[0] = False
        window.refresh_artwork()
        assert window.badge_strip.isHidden()
        assert window.now_card.cover.icon().pixmap(48, 48).toImage().pixelColor(0, 0) == QColor(
            "#264653"
        )
    finally:
        window.close()


def test_album_header_compact_rows_badge_and_search_transition(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QPushButton

    from apple_music_player.browsing import AlbumHeader

    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    image = decode(png())
    monkeypatch.setattr(window.artwork, "get", lambda url: image)
    album = dict(
        wrapperType="collection",
        collectionType="Album",
        collectionId=201,
        collectionName="Quiet Places",
        artistName="Fixture artist",
        artworkUrl100=URL,
        releaseDate="2026-09-15T00:00:00Z",
    )
    songs = [
        dict(
            kind="song",
            trackId=n,
            collectionId=201,
            trackName=f"Track {n}",
            artistName="Fixture artist",
            trackNumber=n,
            discNumber=1,
        )
        for n in range(1, 11)
    ]
    monkeypatch.setattr(
        window.catalog,
        "request",
        lambda endpoint, **kwargs: [album, *songs] if endpoint == "lookup" else [album],
    )
    window.show()
    try:
        window.open_album(Album("br", "201", "Quiet Places — Fixture artist"))
        wait_until(
            lambda: (app.processEvents(), window.tick(), window.play_album_button.isEnabled())[2]
        )
        window.resize(1000, 480)
        window.tick()
        app.processEvents()
        assert window.height() == 480
        assert len(window.findChildren(AlbumHeader)) == 1 and window.album_header.isVisible()
        assert window.album_header.title.text() == "Quiet Places"
        assert window.album_header.artist.text() == "Fixture artist"
        assert window.album_header.details.text() == "2026-09-15 · 10 tracks"
        assert window.detail_card is None
        assert window.play_album_button.parent() is window.album_header
        assert window.all_button.parent() is window.album_header
        assert window.play_album_button.isEnabled() and window.all_button.isEnabled()
        for index in range(10):
            item = window.result_list.item(index)
            assert item.text() == f"{index + 1:02d}  Track {index + 1} — Fixture artist"
            assert item.icon().isNull()
            assert window.result_list.visualItemRect(item).height() <= 32
        window.result_list.setCurrentRow(5)
        window.refresh_artwork()
        assert window.detail_card is None and window.result_list.item(5).icon().isNull()
        assert (
            len([b for b in window.findChildren(QPushButton, "itunesStoreBadge") if b.isVisible()])
            == 1
        )
        scroll = window.pages.widget(1).verticalScrollBar()
        assert scroll.maximum() > 0
        scroll.setValue(scroll.maximum())
        assert window.pause_button.mapTo(window, window.pause_button.rect().bottomLeft()).y() < 480
        window.search.setText("another album")
        window.search_catalog()
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])
        assert window.album_header.isHidden() and window.play_album_button.isHidden()
        assert not window.result_list.item(0).icon().isNull()
        assert window.all_button.parent() is not window.album_header
    finally:
        window.close()


def test_album_header_without_artwork_does_not_request_badge(app):
    from PySide6.QtCore import QCoreApplication, QEvent

    from apple_music_player.browsing import AlbumHeader

    artwork = Mock()
    header = AlbumHeader(artwork)
    try:
        header.set_album(Album("br", "123", "No artwork"), 10)
        header.refresh()
        artwork.get.assert_not_called()
    finally:
        # Unlike production's parent-owned header, this standalone widget must be
        # destroyed while QApplication is alive, not during interpreter shutdown.
        header.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_text_rows_duration_column_and_short_play_label(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    from apple_music_player.browsing import TrackRowDelegate

    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    rows = [
        dict(
            wrapperType="collection",
            collectionId=201,
            artistName="Album artist",
            collectionName="Fixture Album",
        ),
        dict(
            kind="song",
            collectionId=201,
            trackId=1,
            artistName="Performer",
            trackName="First",
            discNumber=1,
            trackNumber=1,
            trackTimeMillis=125000,
        ),
        dict(
            kind="song",
            collectionId=201,
            trackId=2,
            artistName="Performer",
            trackName="Second",
            discNumber=2,
            trackNumber=1,
        ),
    ]
    monkeypatch.setattr(window.catalog, "request", lambda *a, **k: rows)
    window.show()
    try:
        window.open_album(Album("br", "201", "Fixture Album"))
        wait_until(
            lambda: (app.processEvents(), window.tick(), window.play_album_button.isEnabled())[2]
        )
        assert window.play_album_button.text() == "Play album"
        assert "Replace" in window.play_album_button.toolTip()
        assert window.result_list.item(0).text() == "1.01  First — Album artist"
        assert window.result_list.item(1).text() == "2.01  Second — Album artist"
        assert window.result_list.item(0).data(TrackRowDelegate.duration_role) == "2:05"
        assert window.result_list.item(1).data(TrackRowDelegate.duration_role) == "—"
        delegate = window.result_list.itemDelegate()
        assert delegate.duration_alignment & Qt.AlignRight
        for index in range(2):
            item = window.result_list.item(index)
            assert item.icon().isNull()
            rect = window.result_list.visualItemRect(item)
            duration_rect = delegate.duration_rect(rect, window.result_list.fontMetrics())
            assert duration_rect.left() > rect.center().x()
            assert 0 <= rect.right() - duration_rect.right() <= 12
        window.resize(1000, 480)
        app.processEvents()
        assert window.height() == 480
    finally:
        window.close()
