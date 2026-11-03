import io
import json
import threading
from urllib.parse import parse_qs, urlsplit

from conftest import wait_until
from PySide6.QtWidgets import QApplication

from apple_music_player.app import Window
from apple_music_player.catalog import Catalog
from apple_music_player.model import Album, Track

QUERY = "Atlanta Millionaires Club"
ATLANTA = dict(
    wrapperType="collection",
    collectionType="Album",
    collectionId=101,
    collectionName=QUERY,
    artistName="Faye Webster",
)
UNRELATED = dict(
    wrapperType="collection",
    collectionType="Album",
    collectionId=202,
    collectionName="Season Of The Witch - Single",
    artistName="Alanis Morissette",
)


def test_actual_request_parameters_cache_isolation_and_unrelated_result_evidence(monkeypatch):
    now = [100.0]
    calls = []

    def opened(url, **kwargs):
        params = parse_qs(urlsplit(url).query)
        calls.append((urlsplit(url).path, params))
        row = UNRELATED if params.get("term") == [QUERY] else ATLANTA
        return io.BytesIO(json.dumps({"results": [row], "resultCount": 1}).encode())

    monkeypatch.setattr("apple_music_player.catalog.urlopen", opened)
    monkeypatch.setattr("apple_music_player.catalog.time.monotonic", lambda: now[0])
    catalog = Catalog()
    first = catalog.search(QUERY, "br", "album")
    assert calls[-1] == (
        "/search",
        {
            "term": [QUERY],
            "country": ["br"],
            "entity": ["album"],
            "media": ["music"],
            "limit": ["200"],
        },
    )
    assert (
        first[0].id == "202"
    )  # Keep unexpected results; diagnostics must expose, not conceal them.
    assert first.diagnostics["query"] == QUERY and first.diagnostics["source"] == "network"
    assert first.diagnostics["raw_result_count"] == first.diagnostics["accepted_result_count"] == 1
    assert first.diagnostics["raw_public_results"][0]["title"] == UNRELATED["collectionName"]
    now[0] += 4
    second = catalog.search("faye webster Atlanta Millionaires Club", "br", "album")
    assert second[0].id == "101"
    again = catalog.search(QUERY, "br", "album")
    assert again[0].id == "202" and again.diagnostics["source"] == "memory cache"
    assert len(calls) == 2 and first.diagnostics["source"] == "network"
    now[0] += 4
    catalog.artist_albums("301", "br")
    assert first.diagnostics["query"] == QUERY
    now[0] += 4
    catalog.search(QUERY, "gb", "album")
    assert calls[-1][1]["country"] == ["gb"] and len(calls) == 4


def test_diagnostics_public_allowlist_and_parser_rejections(monkeypatch):
    catalog = Catalog()
    rows = [
        dict(UNRELATED, token="TEST_SECRET", credentials={"password": "TEST_SECRET"}),
        None,
        dict(UNRELATED, collectionId="bad", collectionName={"token": "TEST_SECRET"}),
    ]
    monkeypatch.setattr(catalog, "request", lambda *a, **k: rows)
    result = catalog.search(QUERY, "br", "album")
    assert result.diagnostics["raw_result_count"] == 3
    assert result.diagnostics["accepted_result_count"] == 1
    assert "TEST_SECRET" not in json.dumps(result.diagnostics)
    assert result.diagnostics["accepted_results"][0]["id"] == "202"


def test_search_diagnostic_snapshot_survives_stale_home_album_and_edited_input(
    app, tmp_path, monkeypatch
):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    started, release = threading.Event(), threading.Event()
    wrong_response = [False]

    def request(endpoint, **params):
        if params.get("term") == "older query":
            started.set()
            assert release.wait(3)
            return [UNRELATED]
        if endpoint == "search":
            return [UNRELATED if wrong_response[0] else ATLANTA]
        if params.get("entity") == "song":
            return [
                ATLANTA,
                dict(
                    kind="song",
                    trackId=11,
                    collectionId=101,
                    trackName="Fixture",
                    artistName="Faye Webster",
                ),
            ]
        return [UNRELATED]  # Distinct Home artist-album fixture.

    monkeypatch.setattr(window.catalog, "request", request)
    try:
        window.search.setText("older query")
        window.search_catalog()
        assert started.wait(2)
        window.search.setText(QUERY)
        window.search_catalog()
        release.set()
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])
        assert window.results[0].id == "101"
        assert window.last_search_diagnostics["query"] == QUERY
        window.open_album(Album("br", "101", QUERY))
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])
        window.copy_search_button.click()
        report = json.loads(QApplication.clipboard().text())
        assert not report["currently_displaying_this_search"]
        assert report["last_completed_search"]["accepted_results"][0]["id"] == "101"
        window.history.record(Track("br", "11", "Fixture", "101", artist_id="301"))
        window.pages.setCurrentIndex(0)
        wait_until(lambda: (app.processEvents(), window.tick(), window.release_context is None)[2])
        assert window.last_search_diagnostics["accepted_results"][0]["id"] == "101"
        wrong_response[0] = True
        window.search_catalog()
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])
        assert window.results[0].id == "202"
        window.copy_search_button.click()
        report = json.loads(QApplication.clipboard().text())
        assert report["currently_displaying_this_search"]
        assert report["last_completed_search"]["query"] == QUERY
        assert report["last_completed_search"]["accepted_results"][0]["id"] == "202"
        window.search.setText("new typed text")
        assert "previous results" in window.catalog_status.text()
        window.copy_search_button.click()
        report = json.loads(QApplication.clipboard().text())
        assert report["current_input"]["query"] == "new typed text"
        assert report["last_completed_search"]["query"] == QUERY
    finally:
        release.set()
        window.close()
        QApplication.clipboard().clear()
