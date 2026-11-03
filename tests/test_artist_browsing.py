import json
import threading

import pytest
from conftest import wait_until
from PySide6.QtWidgets import QApplication

from apple_music_player.app import Window
from apple_music_player.catalog import Catalog
from apple_music_player.model import Album, Artist, Track

ARTIST = dict(
    wrapperType="artist",
    artistType="Artist",
    artistId=301,
    artistName="Faye Webster",
    primaryGenreName="Alternative",
)
ALBUM = dict(
    wrapperType="collection",
    collectionType="Album",
    collectionId=101,
    collectionName="Atlanta Millionaires Club",
    artistId=301,
    artistName="Faye Webster",
)
SONG = dict(
    wrapperType="track",
    kind="song",
    trackId=11,
    collectionId=101,
    artistId=301,
    artistName="Faye Webster",
    trackName="Fixture",
    trackNumber=1,
)


def test_artist_contract_identity_and_public_diagnostics(monkeypatch):
    catalog = Catalog()
    calls = []

    def request(endpoint, **params):
        calls.append((endpoint, params))
        return [ARTIST, ALBUM, SONG, None, dict(ARTIST, artistId="invalid")]

    monkeypatch.setattr(catalog, "request", request)
    artists = catalog.search("Faye Webster", "br", "musicArtist")
    assert artists == [Artist("br", "301", "Faye Webster", "Alternative")]
    assert calls[-1] == (
        "search",
        dict(term="Faye Webster", country="br", media="music", entity="musicArtist", limit=50),
    )
    assert artists.diagnostics["raw_public_results"][0]["id"] == "301"
    albums = catalog.artist_albums(artists[0].id, artists[0].country)
    assert calls[-1] == ("lookup", dict(id="301", country="br", entity="album", limit=200))
    assert len(albums) == 1 and isinstance(albums[0], Album) and albums[0].id == "101"
    assert albums.diagnostics["artist_id"] == "301"
    assert albums.diagnostics["accepted_results"][0]["id"] == "101"
    monkeypatch.setattr(catalog, "request", lambda *a, **k: [ARTIST])
    assert catalog.artist_albums("301", "br") == []
    assert catalog.artist_albums("not-an-id", "br") == []


def test_artist_album_play_and_diagnostics(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    calls, played = [], []

    def request(endpoint, **params):
        calls.append((endpoint, params))
        if endpoint == "search":
            return [ARTIST]
        return (
            [ARTIST, ALBUM]
            if params["entity"] == "album"
            else [ALBUM, dict(SONG, trackId=12, trackNumber=2), SONG]
        )

    monkeypatch.setattr(window.catalog, "request", request)
    monkeypatch.setattr(
        window.controller, "play_album", lambda tracks, quality: played.append(list(tracks)) or True
    )

    def settle():
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])

    try:
        window.country.setText("br")
        window.search_kind.setCurrentIndex(window.search_kind.findData("musicArtist"))
        window.search.setText("Faye Webster")
        window.search_catalog()
        settle()
        assert window.selected_button.text() == "Open artist"
        assert window.result_list.item(0).icon().isNull()
        assert not window.all_button.isEnabled()
        window.selected_button.click()
        settle()
        assert calls[-1][1]["id"] == "301" and calls[-1][1]["country"] == "br"
        assert "may omit albums" in window.catalog_status.text()
        window.copy_search_button.click()
        report = json.loads(QApplication.clipboard().text())
        assert report["last_completed_search"]["entity"] == "musicArtist"
        assert report["last_artist_album_lookup"]["artist_id"] == "301"
        assert report["currently_displaying_artist_albums"]
        window.selected_button.click()
        settle()
        assert calls[-1][1]["id"] == "101" and calls[-1][1]["entity"] == "song"
        assert all(isinstance(track, Track) for track in window.results)
        window.play_album_button.click()
        assert [track.id for track in played[0]] == ["11", "12"]
        assert all(track.country == "br" for track in played[0])
    finally:
        window.close()
        QApplication.clipboard().clear()


def test_stale_artist_lookup_cannot_overwrite_new_region_search(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    started, release = threading.Event(), threading.Event()

    def request(endpoint, **params):
        if endpoint == "lookup":
            started.set()
            assert release.wait(3)
            return [ALBUM]
        assert params["country"] == "gb" and params["term"] == "New artist"
        return [dict(ARTIST, artistId=302, artistName="New artist")]

    monkeypatch.setattr(window.catalog, "request", request)
    try:
        window.open_artist(Artist("br", "301", "Faye Webster"))
        assert started.wait(2)
        window.country.setText("gb")
        window.search_kind.setCurrentIndex(window.search_kind.findData("musicArtist"))
        window.search.setText("New artist")
        window.search_catalog()
        release.set()
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])
        assert window.results == [Artist("gb", "302", "New artist", "Alternative")]
        assert window.last_artist_diagnostics is None
    finally:
        release.set()
        window.close()


def test_album_artist_button_navigates_by_collection_identity(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    calls = []

    def request(endpoint, **params):
        calls.append(params)
        if params["entity"] == "album":
            return [ARTIST, ALBUM]
        return [ALBUM, dict(SONG, artistId=999, artistName="Guest performer")]

    monkeypatch.setattr(window.catalog, "request", request)

    def settle():
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])

    try:
        window.show()
        window.open_album(Album("br", "101", "Atlanta Millionaires Club"))
        settle()
        button = window.album_header.artist
        assert button.isEnabled()
        assert button.text() == "Faye Webster"
        assert "Open artist albums" in button.accessibleName()
        QTest.mouseClick(button, Qt.LeftButton)
        assert "Loading" in window.catalog_status.text()
        settle()
        assert calls[-1] == dict(id="301", country="br", entity="album", limit=200)
        assert window.results[0].id == "101"
        window.selected_button.click()
        settle()
        assert window.album_header.artist.isEnabled()
        assert window.album_tracks[0].artist_id == "999"
        assert window.album_header.album.artist_id == "301"
        window.album_header.artist.setFocus()
        QTest.keyClick(window.album_header.artist, Qt.Key_Space)
        settle()
        assert calls[-1]["id"] == "301"
        assert not window.controller.queue
    finally:
        window.close()


@pytest.mark.parametrize(
    "collection",
    [
        dict(ALBUM, artistId=""),
        dict(ALBUM, artistId="invalid"),
        dict(ALBUM, artistName="Various Artists", artistId=""),
        None,
    ],
)
def test_missing_album_artist_id_never_infers_track_performer(
    app, tmp_path, monkeypatch, collection
):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    monkeypatch.setattr(
        window.catalog, "request", lambda *a, **k: ([collection] if collection else []) + [SONG]
    )
    try:
        window.open_album(Album("br", "101", "Fixture"))
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])
        assert not window.album_header.artist.isEnabled()
        assert "did not provide" in window.album_header.artist.toolTip()
        window.album_header.artist.click()
        assert window.catalog_future is None
    finally:
        window.close()


def test_artist_link_error_and_superseded_navigation(app, tmp_path, monkeypatch):
    from apple_music_player.model import PlayerError

    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    started, release = threading.Event(), threading.Event()
    fail = [True]

    def request(endpoint, **params):
        if params["entity"] == "album":
            if fail[0]:
                raise PlayerError("Catalog unavailable")
            started.set()
            assert release.wait(3)
            return [ALBUM]
        return [ALBUM, SONG]

    monkeypatch.setattr(window.catalog, "request", request)

    def settle():
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])

    try:
        window.open_album(Album("br", "101", "Fixture"))
        settle()
        window.album_header.artist.click()
        settle()
        assert window.catalog_status.text() == "Catalog unavailable"
        assert window.album_header.artist.isEnabled()  # Retained page permits retry.
        fail[0] = False
        window.album_header.artist.click()
        assert started.wait(2)
        window.open_album(Album("gb", "101", "New navigation"))
        release.set()
        settle()
        assert window.album_header.album.country == "gb"
        assert window.album_tracks and window.album_tracks[0].country == "gb"
        assert not window.controller.queue
    finally:
        release.set()
        window.close()
