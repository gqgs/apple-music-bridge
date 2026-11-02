from concurrent.futures import Future

import pytest
from PySide6.QtWidgets import QApplication

from apple_music_player.app import Window
from apple_music_player.catalog import Catalog
from apple_music_player.model import Track


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    yield app


def test_catalog_fixture_and_no_videos():
    rows = [
        {
            "kind": "song",
            "trackId": 123,
            "collectionId": 456,
            "trackName": "Fixture song",
            "artistName": "Test artist",
        },
        {"kind": "music-video", "trackId": 789},
    ]
    tracks = Catalog().tracks(rows, "us")
    assert len(tracks) == 1 and tracks[0].album_id == "456"
    assert tracks[0].url == "https://music.apple.com/us/song/123"


def test_ui_stale_search_errors_retry_cancel(app, tmp_path):
    window = Window(tmp_path / "cache")
    window.show()
    app.processEvents()
    future = Future()
    window.catalog_future = (window.generation, future)
    window.search.setText("a newer query")
    future.set_result([Track("us", "1", "Stale result")])
    window.tick()
    assert window.result_list.count() == 0
    future = Future()
    window.catalog_future = (window.generation, future)
    future.set_result([Track("us", "2", "Fixture song")])
    window.tick()
    assert window.result_list.count() == 1
    window.result_list.setCurrentRow(0)
    window.add_selected()
    window.controller.downloader = "/missing/downloader"
    window.controller.tick()
    # Drain the already failed worker through the normal UI tick.
    from conftest import wait_until

    wait_until(lambda: (window.tick(), "not found" in window.status.text())[1])
    assert window.controller.queue[0].state == "Failed"
    window.controller.retry()
    assert window.controller.queue[0].state == "Queued"
    window.controller.cancel_download()  # No active job: safe no-op.
    window.remove_selected()  # No selection: safe no-op.
    window.close()
    app.processEvents()


def test_gui_download_play_smoke(app, tmp_path, fake_downloader, fake_mpv):
    import os

    from conftest import wait_until

    from apple_music_player.playback import Mpv

    window = Window(tmp_path / "cache")
    window.controller.downloader = fake_downloader
    window.controller.player = Mpv(fake_mpv)
    window.results = [Track("us", "12345", "Synthetic tone — Test fixture", "100")]
    window.result_list.addItems([track.title for track in window.results])
    window.result_list.setCurrentRow(0)
    window.add_selected()
    window.show()

    def playing():
        app.processEvents()
        window.tick()
        return "Lossless" in window.media_label.text() and window.pause_button.isEnabled()

    wait_until(playing)
    assert "ALAC" in window.media_label.text()
    assert "PCM" in window.output_label.text()
    assert window.pause_button.isEnabled()
    screenshot = os.environ.get("PLAYER_TEST_SCREENSHOT")
    if screenshot:
        assert window.grab().save(screenshot)
    window.close()
    app.processEvents()
