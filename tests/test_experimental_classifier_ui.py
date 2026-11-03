import hashlib
from concurrent.futures import Future
from pathlib import Path

from conftest import wait_until
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest

from apple_music_player import experimental_classifier
from apple_music_player.app import Window
from apple_music_player.model import Album, Artist


def releases():
    return [
        Album("br", "101", "Live", artist="Fixture", album_title="Cornucopia Live"),
        Album("br", "102", "Ordinary", artist="Fixture", album_title="Ordinary"),
        Album("br", "103", "Single", artist="Fixture", album_title="Example - Single"),
    ]


def settle(app, window):
    wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])


def prepare(window, monkeypatch):
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    monkeypatch.setattr(window.artwork.cache, "load", lambda _: QImage())
    monkeypatch.setattr(window.catalog, "artist_albums", lambda *args: releases())


def headings(window):
    return [
        window.result_list.item(row).text()
        for row, index in window.result_indices.items()
        if index == -1
    ]


def test_packaged_network_matches_frozen_candidate():
    path = Path(experimental_classifier.__file__)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "f8d15e7917e74dc76fd05732331f0becacfc92a297ca1aeb68468f697ce57f14"
    )


def test_ui_toggle_regroups_without_fetch_or_queue_changes(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    prepare(window, monkeypatch)
    try:
        window.show()
        window.open_artist(Artist("br", "301", "Fixture"))
        settle(app, window)
        assert window.release_classifier.currentData() == "heuristic"
        assert headings(window) == ["Albums", "Singles & EPs"]
        selected_row = next(row for row, index in window.result_indices.items() if index == 0)
        window.result_list.setCurrentRow(selected_row)
        monkeypatch.setattr(window.catalog, "artist_albums", lambda *args: pytest_fail_fetch())
        window.release_classifier.setFocus()
        QTest.keyClick(window.release_classifier, Qt.Key_End)
        assert window.release_classifier.currentData() == "network"
        assert headings(window) == ["Albums", "Singles & EPs", "Live Albums"]
        assert window.result_index(window.result_list.currentRow()) == 0
        assert "Heuristic: Albums" in window.result_list.currentItem().toolTip()
        assert "Experimental network: Live Albums" in window.result_list.currentItem().toolTip()
        assert "1 classifier disagreements" in window.catalog_status.text()
        assert not window.controller.queue and not window.catalog_future
        QTest.keyClick(window.release_classifier, Qt.Key_Home)
        assert headings(window) == ["Albums", "Singles & EPs"]
        assert window.result_index(window.result_list.currentRow()) == 0
        window.render_catalog([])
        QTest.keyClick(window.release_classifier, Qt.Key_End)
        assert window.result_list.count() == 0
    finally:
        window.close()


def pytest_fail_fetch():
    raise AssertionError("Changing classifier must not fetch catalog data")


def test_pending_results_and_back_restore_classifier_context(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    prepare(window, monkeypatch)
    try:
        window.show()
        window.open_artist(Artist("br", "301", "Fixture"))
        settle(app, window)
        future = Future()
        window.catalog_future = (window.generation, future)
        window.release_classifier.setCurrentIndex(1)
        assert headings(window) == ["Albums", "Singles & EPs"]  # Wait for new data.
        future.set_result(releases())
        window.tick()
        assert headings(window) == ["Albums", "Singles & EPs", "Live Albums"]
        row = next(row for row, index in window.result_indices.items() if index == 0)
        window.result_list.setCurrentRow(row)
        songs = window.catalog.tracks(
            [
                dict(
                    kind="song",
                    trackId=201,
                    collectionId=101,
                    trackName="Start",
                    artistName="Fixture",
                )
            ],
            "br",
        )
        calls = []
        monkeypatch.setattr(
            window.catalog, "album", lambda id, country: calls.append((id, country)) or songs
        )
        QTest.mouseClick(window.selected_button, Qt.LeftButton)
        settle(app, window)
        assert calls == [("101", "br")]
        window.release_classifier.setCurrentIndex(0)
        QTest.mouseClick(window.back_button, Qt.LeftButton)
        app.processEvents()
        assert window.release_classifier.currentData() == "network"
        assert headings(window) == ["Albums", "Singles & EPs", "Live Albums"]
        assert window.result_index(window.result_list.currentRow()) == 0
    finally:
        window.close()
