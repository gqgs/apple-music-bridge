from apple_music_player.catalog import Catalog
from apple_music_player.model import Album, Artist

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
