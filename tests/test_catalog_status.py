import threading

import pytest
from conftest import wait_until

from apple_music_player.app import SEARCH_IDLE, Window
from apple_music_player.catalog import CatalogDeferred
from apple_music_player.model import Artist, PlayerError, Track


@pytest.mark.parametrize("history", [False, True])
def test_fresh_search_idle_does_not_inherit_home_loading(app, tmp_path, monkeypatch, history):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    release = threading.Event()
    calls = []

    def lookup(*args):
        calls.append(args)
        assert release.wait(3)
        return []

    monkeypatch.setattr(window.catalog, "artist_albums", lookup)
    monkeypatch.setattr(window.catalog, "search", lambda *a: pytest.fail("Unexpected search"))
    try:
        if history:
            window.history.record(Track("br", "11", "Fixture", "101", artist_id="301"))
        window.show()
        window.tick()
        if history:
            wait_until(lambda: (app.processEvents(), bool(calls))[1])
            assert "Loading" in window.release_status.text()
        window.pages.setCurrentIndex(1)
        assert window.catalog_status.text() == SEARCH_IDLE
        assert window.catalog_future is None and not window.catalog_timer.isActive()
        release.set()
        app.processEvents()
        window.tick()
        assert window.catalog_status.text() == SEARCH_IDLE
        window.search.setText("faye")
        assert window.catalog_status.text() == SEARCH_IDLE
    finally:
        release.set()
        window.close()


@pytest.mark.parametrize("outcome", ["results", "empty", "error", "cooldown"])
def test_search_status_tracks_actual_request_state(app, tmp_path, monkeypatch, outcome):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    release = threading.Event()

    def search(*args):
        assert release.wait(3)
        if outcome == "error":
            raise PlayerError("Catalog unavailable")
        if outcome == "cooldown":
            raise CatalogDeferred(30)
        return [] if outcome == "empty" else [Artist("br", "301", "Faye fixture")]

    monkeypatch.setattr(window.catalog, "search", search)
    try:
        window.pages.setCurrentIndex(1)
        assert window.catalog_status.text() == SEARCH_IDLE
        window.search.setText("faye")
        window.search_catalog()
        assert window.catalog_status.text() == "Loading catalog…"
        assert window.catalog_future is not None
        release.set()
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])
        status = window.catalog_status.text()
        if outcome == "results":
            assert "1 artist" in status
        elif outcome == "empty":
            assert "0 artists" in status and "try another" in status
        elif outcome == "error":
            assert status == "Catalog unavailable"
        else:
            assert status == "Waiting for catalog request spacing…"
            assert window.catalog_timer.isActive()
            window.search.setText("new intent")
            assert not window.catalog_timer.isActive()
            assert window.catalog_status.text() == SEARCH_IDLE
    finally:
        release.set()
        window.close()
