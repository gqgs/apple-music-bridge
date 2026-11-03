from dataclasses import replace

from conftest import wait_until
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from apple_music_player.app import Window
from apple_music_player.catalog import grouped_releases, release_group
from apple_music_player.model import Album, Artist


def album(id, title, date=""):
    return Album("br", id, title + " — Fixture", "Fixture", "301", title, release_date=date)


def test_release_classification_dates_and_deterministic_ties():
    releases = [
        album("1", "Old album", "2010-01-01"),
        album("2", "New album", "2026-01-01"),
        album("3", "No date"),
        album("4", "Short – eP", "2020-01-01"),
        album("5", "Shorter — EP", "2025-01-01"),
        album("6", "Song - Single", "2026-02-01"),
        album("7", ""),
        album("8", "Tie", "2024-01-01"),
        album("9", "Tie", "2024-01-01"),
        album("10", "Invalid date", "no-date"),
    ]
    groups = grouped_releases(releases)
    assert [(name, [a.id for a in values]) for name, values in groups] == [
        ("Albums", ["2", "8", "9", "1", "10", "3"]),
        ("Singles & EPs", ["6", "5", "4"]),
        ("Others", ["7"]),
    ]
    assert grouped_releases(list(reversed(releases))) == groups
    assert release_group(album("11", "EP Stories")) == "Albums"
    assert release_group(album("12", "Série - EP")) == "Singles & EPs"
    assert release_group(album("13", "Édition - Single")) == "Singles & EPs"
    # No unverified localization / track-count heuristics.
    assert release_group(album("14", "Title - Sencillo")) == "Singles & EPs"
    assert grouped_releases([]) == []


def test_grouped_artist_navigation_headers_and_play(app, tmp_path, monkeypatch):
    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    releases = [
        album("101", "First - Single", "2026-01-01"),
        album("102", "Second - EP", "2025-01-01"),
        album("103", "Third album (Studio Album)", "2024-01-01"),
    ]
    monkeypatch.setattr(window.catalog, "artist_albums", lambda *a: releases)
    songs = window.catalog.tracks(
        [dict(kind="song", trackId=201, collectionId=102, trackName="Start", artistName="Fixture")],
        "br",
    )
    calls, played = [], []

    def tracks(id, country):
        calls.append((id, country))
        return songs

    monkeypatch.setattr(window.catalog, "album", tracks)
    monkeypatch.setattr(
        window.controller, "play_album", lambda tracks, quality: played.append(list(tracks)) or True
    )

    def settle():
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])

    try:
        assert window.search_kind.currentData() == "musicArtist"
        assert not hasattr(window, "url")
        assert not any(button.text() == "Open URL" for button in window.findChildren(QPushButton))
        window.show()
        window.open_artist(Artist("br", "301", "Fixture"))
        settle()
        assert [window.result_list.item(i).text() for i in range(5)] == [
            "Albums",
            "Third album (Studio Album) (2024) — Fixture",
            "Singles & EPs",
            "First - Single (2026) — Fixture",
            "Second - EP (2025) — Fixture",
        ]
        for i in (0, 2):
            heading = window.result_list.item(i)
            assert not heading.flags() & Qt.ItemIsSelectable
            assert heading.sizeHint().height() >= 48
            header = window.result_list.itemWidget(heading)
            assert header.accessibleName().endswith("releases")
            assert header.autoFillBackground()
            window.result_list.setCurrentRow(i)
            window.add_selected()
            window.browse_album()
            assert not calls and not window.controller.queue
        window.result_list.setCurrentRow(4)
        QTest.mouseClick(window.selected_button, Qt.LeftButton)
        settle()
        assert calls == [("102", "br")]
        window.play_album_button.click()
        assert played == [songs]
    finally:
        window.close()


def test_many_grouped_releases_keep_viewport_loading_stable(app, tmp_path, monkeypatch):
    from PySide6.QtGui import QColor, QImage

    window = Window(tmp_path / "cache")
    window.startup_attempted = True
    monkeypatch.setattr(window.controller, "tick", lambda: None)
    loaded = []

    def load(url):
        loaded.append(url)
        image = QImage(100, 100, QImage.Format_RGB32)
        image.fill(QColor("#536e75"))
        return image

    monkeypatch.setattr(window.artwork.cache, "load", load)
    releases = [
        replace(
            album(str(n), f"Release {n}" + (" - EP" if n % 2 else "")),
            artwork_url=f"https://is1-ssl.mzstatic.com/image/thumb/f{n}/100x100bb.jpg",
        )
        for n in range(1, 201)
    ]
    monkeypatch.setattr(window.catalog, "artist_albums", lambda *a: releases)
    try:
        window.show()
        window.resize(1000, 480)
        window.open_artist(Artist("br", "301", "Fixture"))
        wait_until(lambda: (app.processEvents(), window.tick(), window.catalog_future is None)[2])
        assert window.result_list.count() == 202
        QTest.qWait(700)
        before = len(loaded)
        QTest.qWait(700)
        assert len(loaded) == before < 20
        assert window.result_list.item(0).icon().isNull()
        window.result_list.setCurrentRow(150)
        index = window.result_index(150)
        window.result_list.scrollToItem(window.result_list.item(150))
        QTest.qWait(700)
        before = len(loaded)
        QTest.qWait(700)
        assert len(loaded) == before < 30
        assert window.detail_card.album == window.results[index]
        assert window.result_list.currentRow() == 150
    finally:
        window.close()


def test_bjork_title_hints_and_ambiguous_names():
    # Names verified on Apple's public Björk artist page; these expectations test
    # our documented heuristic, not a claim of matching Apple's editorial categories.
    expected = {
        "Human Behaviour (UK Remixes) - EP": "Singles & EPs",
        "Big Time Sensuality (UK CD1 Single)": "Singles & EPs",
        "Vulnicura (Live)": "Live Albums",
        "Björk's Greatest Hits": "Compilations",
        "Fossora": "Albums",
        "Bastards": "Albums",  # No title hint: approximate Albums fallback.
        "Cornucopia Live": "Albums",  # Bare word 'Live' is ambiguous; use Albums fallback.
    }
    for title, group in expected.items():
        assert release_group(album("1", title)) == group
    for title in (
        "Olive",
        "Live Forever",
        "I Live",
        "The Best of Me",
        "Single Minded",
        "EP Stories",
    ):
        assert release_group(album("1", title)) == "Albums"
    for title in ("Show (Ao Vivo)", "Show - En Vivo", "Live at the Hall"):
        assert release_group(album("1", title)) == "Live Albums"
    assert release_group(album("1", "Show (Live) - Single")) == "Singles & EPs"
    assert release_group(album("1", "Greatest Hits (Live)")) == "Compilations"
    assert release_group(album("1", "Coletânea")) == "Compilations"
    assert release_group(album("1", "Record (Studio Album)")) == "Albums"
    groups = grouped_releases(
        [
            album("1", ""),
            album("2", "Coletânea"),
            album("3", "Show (Live)"),
            album("4", "Short - EP"),
            album("5", "Record (Studio Album)"),
        ]
    )
    assert [group for group, _ in groups] == [
        "Albums",
        "Singles & EPs",
        "Live Albums",
        "Compilations",
        "Others",
    ]


def test_release_year_uses_only_valid_returned_date():
    assert album("1", "Record", "1997-09-22T00:00:00Z").release_title == "Record (1997)"
    for invalid in ("", "unknown", "2024", "2024-02-30"):
        release = album("1", "Record", invalid)
        assert release.release_year == ""
        assert release.release_title == "Record"
    original = album("1", "Record", "1997-09-22")
    assert original.album_title == "Record" and original.title == "Record — Fixture"
