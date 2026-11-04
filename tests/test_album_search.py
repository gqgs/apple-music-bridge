import pytest

from apple_music_player.catalog import Catalog
from apple_music_player.model import Album, PlayerError, Track

# Synthetic IDs, public metadata shape; no live API/account required.
ALBUM = {
    "wrapperType": "collection",
    "collectionType": "Album",
    "collectionId": 12345,
    "collectionName": "If You Leave",
    "artistName": "Daughter",
}
SONGS = [
    {
        "kind": "song",
        "trackId": 12346,
        "collectionId": 12345,
        "trackName": "Winter",
        "artistName": "Daughter",
    },
    {
        "kind": "song",
        "trackId": 12347,
        "collectionId": 12345,
        "trackName": "Smother",
        "artistName": "Daughter",
    },
]


def test_album_search_contract_and_lookup(monkeypatch):
    catalog = Catalog()
    calls = []

    def request(endpoint, **params):
        calls.append((endpoint, params))
        return [ALBUM, *SONGS]

    monkeypatch.setattr(catalog, "request", request)
    albums = catalog.search("daughter if you leave", "gb", "album")
    assert albums == [
        Album(
            "gb", "12345", "If You Leave — Daughter", artist="Daughter", album_title="If You Leave"
        )
    ]
    assert calls[-1] == (
        "search",
        {
            "term": "daughter if you leave",
            "country": "gb",
            "media": "music",
            "entity": "album",
            "limit": 200,
        },
    )
    tracks = catalog.album(albums[0].id, albums[0].country)
    assert [track.id for track in tracks] == ["12346", "12347"]
    assert all(isinstance(track, Track) and track.country == "gb" for track in tracks)
    assert calls[-1] == ("lookup", {"id": "12345", "country": "gb", "entity": "song", "limit": 200})
    assert catalog.albums([dict(ALBUM, collectionId="bad"), *SONGS], "gb") == []
    with pytest.raises(PlayerError):
        catalog.search("daughter", "invalid", "album")


def test_faye_album_query_uses_full_public_limit_and_direct_lookup(monkeypatch):
    from apple_music_player.model import parse_url

    catalog = Catalog()
    calls = []

    def request(endpoint, **params):
        calls.append((endpoint, params))
        return [dict(ALBUM, collectionId=n, collectionName=f"Fixture {n}") for n in range(60)]

    monkeypatch.setattr(catalog, "request", request)
    assert len(catalog.search("faye webster Atlanta Millionaires Club", "br", "album")) == 60
    assert calls[-1][1]["limit"] == 200
    assert calls[-1][1]["term"] == "faye webster Atlanta Millionaires Club"
    assert calls[-1][1]["country"] == "br"
    assert parse_url("https://music.apple.com/br/album/atlanta-millionaires-club/1452886606") == (
        "br",
        "album",
        "1452886606",
    )


def test_album_order_filters_other_collections_and_preserves_unnumbered(monkeypatch):
    catalog = Catalog()
    rows = [
        dict(SONGS[0], trackId=3, discNumber=2, trackNumber=1),
        dict(SONGS[0], trackId=2, discNumber=1, trackNumber=2),
        dict(SONGS[0], trackId=1, discNumber=1, trackNumber=1),
        dict(SONGS[0], trackId=9, collectionId=999),
    ]
    monkeypatch.setattr(catalog, "request", lambda *a, **k: rows)
    assert [t.id for t in catalog.album("12345", "br")] == ["1", "2", "3"]
    rows[0].pop("trackNumber")
    assert [t.id for t in catalog.album("12345", "br")] == ["3", "2", "1"]


def test_album_artist_duration_and_multidisc_rows(monkeypatch):
    catalog = Catalog()
    collection = dict(ALBUM, artistName="Various Artists", artistId=99)
    rows = [
        collection,
        dict(
            SONGS[0],
            artistName="Guest A",
            artistId=10,
            trackId=1,
            trackName="Finale",
            trackNumber=1,
            discNumber=2,
            trackTimeMillis=3661000,
        ),
        dict(
            SONGS[1],
            artistName="Guest B",
            artistId=11,
            trackId=2,
            trackName="Opening",
            trackNumber=1,
            discNumber=1,
            trackTimeMillis=209900,
        ),
    ]
    monkeypatch.setattr(catalog, "request", lambda *a, **k: rows)
    tracks = catalog.album("12345", "br")
    assert [t.id for t in tracks] == ["2", "1"]
    assert tracks[0].album_row_title(True) == "1.01  Opening — Various Artists"
    assert tracks[1].album_row_title(True) == "2.01  Finale — Various Artists"
    assert [t.duration_label for t in tracks] == ["3:29", "1:01:01"]
    assert tracks[0].artist == "Guest B" and tracks[0].album.artist == "Various Artists"
    assert tracks[0].album.artist_id == "99"
    collection.pop("artistId")
    assert catalog.album("12345", "br")[0].album.artist_id == ""
    rows.remove(collection)
    assert catalog.album("12345", "br")[0].album_row_title(True) == "1.01  Opening — Guest B"


@pytest.mark.parametrize("duration", [None, "unknown", "123000", -10, 0, True, 2.5])
def test_missing_or_invalid_duration_is_not_invented(duration):
    track = Catalog().tracks([dict(SONGS[0], trackTimeMillis=duration)], "br")[0]
    assert track.duration_label == "—"
    assert track.album_row_title() == "—  Winter — Daughter"
