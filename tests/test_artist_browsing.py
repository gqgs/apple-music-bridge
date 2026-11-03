import json
import threading

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
