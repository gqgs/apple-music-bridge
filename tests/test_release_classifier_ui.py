import hashlib
import subprocess
import sys
from pathlib import Path

from conftest import wait_until
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox

from apple_music_player import release_classifier
from apple_music_player.app import Window
from apple_music_player.catalog import release_group
from apple_music_player.model import Album, Artist


def test_packaged_network_matches_frozen_candidate():
    assert hashlib.sha256(Path(release_classifier.__file__).read_bytes()).hexdigest() == (
        "f8d15e7917e74dc76fd05732331f0becacfc92a297ca1aeb68468f697ce57f14"
    )


def test_classifier_runs_without_site_packages_or_qt():
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import runpy,sys; m=runpy.run_path(sys.argv[1]); "
            "print(m['classify']('Cornucopia Live')); "
            "assert not any(k.startswith('PySide') for k in sys.modules)",
            str(Path(release_classifier.__file__)),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "Live Albums"


def test_default_network_artist_album_back_and_play(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    monkeypatch.setattr(window.artwork.cache, "load", lambda _: QImage())
    releases = [
        Album("br", "101", "Live", artist="Fixture", album_title="Cornucopia Live"),
        Album("br", "102", "Ordinary", artist="Fixture", album_title="Ordinary"),
        Album("br", "103", "Single", artist="Fixture", album_title="Example - Single"),
    ]
    monkeypatch.setattr(window.catalog, "artist_albums", lambda *args: releases)
    songs = window.catalog.tracks(
        [dict(kind="song", trackId=201, collectionId=101, trackName="Start", artistName="Fixture")],
        "br",
    )
    calls, played = [], []
    monkeypatch.setattr(
        window.catalog, "album", lambda id, country: calls.append((id, country)) or songs
    )
    monkeypatch.setattr(
        window.controller, "play_album", lambda tracks, quality: played.append(list(tracks)) or True
    )

    def settle():
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])

    try:
        window.show()
        assert window.windowTitle() == "Apple Music Player"
        assert not hasattr(window, "release_classifier")
        assert not any(
            "heuristic" in combo.itemText(i).lower()
            for combo in window.findChildren(QComboBox)
            for i in range(combo.count())
        )
        window.open_artist(Artist("br", "301", "Fixture"))
        settle()
        headings = [
            window.result_list.item(row).text()
            for row, index in window.result_indices.items()
            if index == -1
        ]
        assert headings == ["Albums", "Singles & EPs", "Live Albums"]
        row = next(row for row, index in window.result_indices.items() if index == 0)
        window.result_list.setCurrentRow(row)
        assert not window.result_list.currentItem().toolTip()
        assert "Approximate neural groups" in window.catalog_status.text()
        QTest.mouseClick(window.selected_button, Qt.LeftButton)
        settle()
        assert calls == [("101", "br")]
        QTest.mouseClick(window.play_album_button, Qt.LeftButton)
        assert played == [songs]
        QTest.mouseClick(window.back_button, Qt.LeftButton)
        app.processEvents()
        assert window.result_index(window.result_list.currentRow()) == 0
        assert release_group(window.results[0]) == "Live Albums"
        window.render_catalog([])
        assert window.result_list.count() == 0
    finally:
        window.close()
