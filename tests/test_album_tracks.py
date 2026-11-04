"""Public metadata regression for the reported streaming-only DJ mix.

Fixture reduced from Apple Music's BR album page on 2026-11-04. No credentials,
preview URLs, playback actions, analytics, or audio are retained.
"""

import io
import json
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError

import pytest

from apple_music_player.apple_album import MAX_PAGE_BYTES, parse_album_tracks
from apple_music_player.bridge.http import ProtocolCore
from apple_music_player.catalog import Catalog, CatalogDeferred
from apple_music_player.model import PlayerError

ALBUM_ID = "1719860221"
COLLECTION = {
    "wrapperType": "collection",
    "collectionType": "Album",
    "collectionId": int(ALBUM_ID),
    "collectionName": "Julya Karma at Cercle x W Hotels at ADE 2023 (DJ Mix)",
    "artistName": "Julya Karma",
    "artistId": 1675294952,
    "trackCount": 23,
    "releaseDate": "2023-10-18T07:00:00Z",
}


@pytest.fixture
def album():
    return json.loads((Path(__file__).parent / "fixtures/julya_karma_album.json").read_text())


def document(album):
    return (
        '<script type="application/json" id="serialized-server-data">'
        + json.dumps(album)
        + "</script>"
    ).encode()


def test_real_dj_mix_retains_all_23_ids_credits_order_and_duration(album):
    rows = parse_album_tracks(document(album), "br", ALBUM_ID, 23)
    assert len(rows) == 23
    assert [r["trackNumber"] for r in rows] == list(range(1, 24))
    assert rows[0]["trackId"] == "1719860222"
    assert rows[0]["trackName"] == "Everlasting Morning (Mixed)"
    assert rows[0]["artistName"] == "Stephan Barnem"
    assert rows[0]["artistId"] == "325925747"
    assert rows[6]["artistName"] == "ID" and rows[6]["artistId"] == ""
    assert rows[-1]["trackName"] == "Villa Mercedes (Mixed)"
    assert sum(r["trackTimeMillis"] for r in rows) == 7_041_297
    assert all(r["collectionId"] == ALBUM_ID for r in rows)
    assert not any("url" in k.lower() for r in rows for k in r)


@pytest.mark.parametrize(
    "change",
    [
        lambda a: a["header"]["contentDescriptor"]["identifiers"].update(storeAdamID="999"),
        lambda a: a["header"]["contentDescriptor"].update(kind="song"),
        lambda a: a["header"]["contentDescriptor"].update(
            url="https://music.apple.com/us/album/1719860221"
        ),
        lambda a: a["header"].update(trackCount=24),
        lambda a: a["header"].update(trackCount="23"),
        lambda a: a.update(duplicate=a["header"]),
        lambda a: a.update(duplicate=a["tracks"]),
        lambda a: a["tracks"].update(itemKind="recommendation"),
        lambda a: a["tracks"].update(id="track-list - 999"),
        lambda a: a["tracks"]["items"].pop(),
        lambda a: a["tracks"]["items"].__setitem__(1, a["tracks"]["items"][0]),
        lambda a: a["tracks"]["items"][0].update(id="track-lockup - 999 - 1719860222"),
        lambda a: a["tracks"]["items"][0].update(title=None),
        lambda a: a["tracks"]["items"][0].update(trackNumber=True),
        lambda a: a["tracks"]["items"][0].update(discNumber=0),
        lambda a: a["tracks"]["items"][1].update(trackNumber=1),
        lambda a: a["tracks"]["items"][0].update(contentDescriptor=None),
    ],
)
def test_incomplete_conflicting_or_changed_page_is_rejected(album, change):
    change(album)
    assert parse_album_tracks(document(album), "br", ALBUM_ID, 23) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/br/album/1719860221?i=1719860222",
        "https://music.apple.com/us/album/1719860221?i=1719860222",
        "https://music.apple.com/br/album/999?i=1719860222",
        "https://music.apple.com/br/album/1719860221?i=999",
        "https://music.apple.com/br/album/1719860221?i=1719860222&i=999",
        "https://music.apple.com/br/album/1719860221?i=1719860222&token=private",
        "https://music.apple.com/br/album/1719860221?i=1719860222#other",
        "https://[broken/br/album/1719860221?i=1719860222",
    ],
)
def test_track_link_must_bind_exact_song_album_and_storefront(album, url):
    album["tracks"]["items"][0]["contentDescriptor"]["url"] = url
    assert parse_album_tracks(document(album), "br", ALBUM_ID, 23) is None


def test_untrusted_script_and_count_boundaries(album):
    for bad in (b"", b"\xff", b"x" * (MAX_PAGE_BYTES + 1), document(album) * 2):
        assert parse_album_tracks(bad, "br", ALBUM_ID, 23) is None
    for count in (0, -1, True, "23", 201, 24):
        assert parse_album_tracks(document(album), "br", ALBUM_ID, count) is None


def test_only_album_track_list_is_read_and_artist_id_is_not_guessed(album):
    album["recommendation"] = deepcopy(album["tracks"]["items"][0])
    album["recommendation"]["title"] = "Wrong recommendation"
    album["tracks"]["items"][0]["subtitleLinks"] = [{"title": "Stephan Barnem", "segue": None}]
    rows = parse_album_tracks(document(album), "br", ALBUM_ID, 23)
    assert len(rows) == 23 and rows[0]["trackName"] == "Everlasting Morning (Mixed)"
    assert rows[0]["artistId"] == ""


def test_catalog_and_get_album_restore_tracks_cache_and_metadata(album, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(Catalog, "request", lambda *a, **k: [COLLECTION])
    monkeypatch.setattr(
        "apple_music_player.catalog.fetch_album_page",
        lambda *args: calls.append(args) or document(album),
    )
    core = ProtocolCore(tmp_path / "cache", 1024**2, catalog=Catalog())
    try:
        identifier = f"am:br:lossless:album:{ALBUM_ID}"
        result = core.dispatch("getAlbum", {"id": identifier})["album"]
        assert result["songCount"] == 23 and result["duration"] == 7041
        assert len(result["song"]) == 23
        first = result["song"][0]
        assert first["title"] == "Everlasting Morning (Mixed)"
        assert first["artist"] == "Stephan Barnem"
        assert first["album"] == COLLECTION["collectionName"]
        track = core.resolve(first["id"], "song")
        assert track.album_artist == "Julya Karma" and track.album_artist_id == "1675294952"
        assert track.album.artist == "Julya Karma"
        assert track.url == "https://music.apple.com/br/song/1719860222"
        assert core.dispatch("getAlbum", {"id": identifier})["album"] == result
        assert calls == [("br", ALBUM_ID)]
    finally:
        core.close()


@pytest.mark.parametrize(
    "rows", [[], [dict(COLLECTION, trackCount=0)], [dict(COLLECTION, trackCount=None)]]
)
def test_empty_or_unknown_collection_does_not_scrape(rows, monkeypatch):
    monkeypatch.setattr(Catalog, "request", lambda *a, **k: rows)
    monkeypatch.setattr(
        "apple_music_player.catalog.fetch_album_page",
        lambda *a: pytest.fail("unexpected page request"),
    )
    assert Catalog().album(ALBUM_ID, "br") == []


def test_itunes_song_rows_remain_primary(album, monkeypatch):
    rows = parse_album_tracks(document(album), "br", ALBUM_ID, 23)
    monkeypatch.setattr(Catalog, "request", lambda *a, **k: [COLLECTION, *rows])
    monkeypatch.setattr(
        "apple_music_player.catalog.fetch_album_page",
        lambda *a: pytest.fail("unexpected page request"),
    )
    assert len(Catalog().album(ALBUM_ID, "br")) == 23


def test_album_page_fetch_obeys_shared_limiter_and_cache_expiry(album, monkeypatch):
    now, calls = [100.0], []
    monkeypatch.setattr("apple_music_player.catalog.time.monotonic", lambda: now[0])
    monkeypatch.setattr(Catalog, "request", lambda *a, **k: [COLLECTION])
    monkeypatch.setattr(
        "apple_music_player.catalog.fetch_album_page", lambda *a: calls.append(a) or document(album)
    )
    catalog = Catalog()
    catalog.last_request = 99
    with pytest.raises(CatalogDeferred) as deferred:
        catalog.album(ALBUM_ID, "br")
    assert deferred.value.seconds == 2 and not calls
    now[0] = 102
    assert len(catalog.album(ALBUM_ID, "br")) == 23
    assert len(catalog.album(ALBUM_ID, "br")) == 23 and len(calls) == 1
    now[0] += 3601
    assert len(catalog.album(ALBUM_ID, "br")) == 23 and len(calls) == 2


def test_failed_fallback_reports_error_not_empty_success(monkeypatch):
    monkeypatch.setattr(Catalog, "request", lambda *a, **k: [COLLECTION])
    monkeypatch.setattr(
        "apple_music_player.catalog.fetch_album_page", lambda *a: b"PRIVATE_BAD_PAYLOAD"
    )
    with pytest.raises(PlayerError, match="Catalog unavailable") as error:
        Catalog().album(ALBUM_ID, "br")
    assert "PRIVATE" not in str(error.value)


def test_fallback_rate_limit_uses_retry_after_and_never_caches_failure(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("apple_music_player.catalog.time.monotonic", lambda: now[0])
    monkeypatch.setattr(Catalog, "request", lambda *a, **k: [COLLECTION])

    def limited(*args):
        raise HTTPError(
            "https://music.apple.com", 429, "PRIVATE", {"Retry-After": "20"}, io.BytesIO(b"PRIVATE")
        )

    monkeypatch.setattr("apple_music_player.catalog.fetch_album_page", limited)
    catalog = Catalog()
    with pytest.raises(CatalogDeferred) as error:
        catalog.album(ALBUM_ID, "br")
    assert error.value.seconds == 20 and error.value.rate_limited
    assert catalog.not_before == 120 and not catalog.responses


def test_fresh_lookup_yields_before_second_network_request(album, monkeypatch):
    now, calls = [100.0], []
    monkeypatch.setattr("apple_music_player.catalog.time.monotonic", lambda: now[0])

    def opened(url, timeout):
        calls.append(url)
        now[0] += 13  # The first read outlives the bridge's normal 12 s deadline.
        return io.BytesIO(json.dumps({"results": [COLLECTION]}).encode())

    monkeypatch.setattr("apple_music_player.catalog.urlopen", opened)
    monkeypatch.setattr(
        "apple_music_player.catalog.fetch_album_page",
        lambda *args: calls.append(args) or document(album),
    )
    catalog = Catalog()
    with pytest.raises(CatalogDeferred) as deferred:
        catalog.album(ALBUM_ID, "br")
    assert deferred.value.seconds == 0 and len(calls) == 1
    # A permitted replay reuses iTunes data and starts only the fallback read.
    assert len(catalog.album(ALBUM_ID, "br")) == 23 and len(calls) == 2


def test_actor_does_not_start_fallback_after_lookup_expires(album, monkeypatch, tmp_path):
    now = [100.0]
    monkeypatch.setattr("apple_music_player.catalog.time.monotonic", lambda: now[0])

    def opened(url, timeout):
        now[0] += 13
        return io.BytesIO(json.dumps({"results": [COLLECTION]}).encode())

    monkeypatch.setattr("apple_music_player.catalog.urlopen", opened)
    monkeypatch.setattr(
        "apple_music_player.catalog.fetch_album_page",
        lambda *args: pytest.fail("fallback started after deadline"),
    )
    from apple_music_player.bridge.core import BridgeError

    core = ProtocolCore(tmp_path / "cache", 1024**2, catalog=Catalog())
    try:
        core.operation_deadline = 112
        with pytest.raises(BridgeError, match="expired"):
            core.request(core.catalog.album, ALBUM_ID, "br")
    finally:
        core.close()
