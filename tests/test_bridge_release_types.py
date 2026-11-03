import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest
from test_bridge import ALBUM, ARTIST, TRACK, payload, request
from test_bridge import bridge as _bridge

from apple_music_player.bridge import release_types as module

bridge = _bridge


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Regular Record", ["Album"]),
        ("Short Record - EP", ["EP"]),
        ("Track - Single", ["Single"]),
        ("Track - Sencillo", ["Single"]),
        ("Single EP", ["EP", "Single"]),
        ("Concert (Live)", ["Live"]),
        ("Ao Vivo", ["Live"]),
        ("Greatest Hits", ["Compilation"]),
        ("Remixes", ["Other"]),
        ("", []),
        ("   ", []),
    ],
)
def test_release_type_mapping(title, expected):
    assert module.release_types(title) == expected


def test_unknown_classifier_result_is_not_explicit_other(monkeypatch):
    monkeypatch.setattr(module, "classify", lambda _: "Future unsupported category")
    assert module.release_types("Record") == []
    monkeypatch.setattr(module, "classify", lambda _: "Singles & EPs")
    assert module.release_types("No subtype evidence") == []


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Record", ["Album"]),
        ("Track - EP", ["EP"]),
        ("Track - Single", ["Single"]),
        ("Live Record", ["Live"]),
        ("Greatest Hits", ["Compilation"]),
        ("Remixes", ["Other"]),
        ("", []),
    ],
)
def test_all_album_serializations_and_xml(bridge, monkeypatch, title, expected):
    server, _ = bridge
    album = replace(ALBUM, title=title, album_title=title)
    server.actor.call("remember", [ARTIST, album, TRACK])
    monkeypatch.setattr(server.actor.core.catalog, "search", lambda *args: [album])
    monkeypatch.setattr(server.actor.core.catalog, "artist_albums", lambda *args: [album])
    album_id = "am:br:lossless:album:2"
    results = [
        payload(server, "getAlbum", {"id": album_id})["album"],
        payload(server, "getArtist", {"id": "am:br:lossless:artist:1"})["artist"]["album"][0],
        payload(server, "search3", {"query": "fixture", "artistCount": "0", "songCount": "0"})[
            "searchResult3"
        ]["album"][0],
        payload(server, "getAlbumList2")["albumList2"]["album"][0],
    ]
    assert all(result["releaseTypes"] == expected for result in results)
    assert all(result["id"] == album_id for result in results)
    assert "releaseTypes" not in payload(server, "getSong", {"id": "am:br:lossless:song:3"})["song"]
    ns = {"s": "http://subsonic.org/restapi"}
    for endpoint, params, path in [
        ("getAlbum", {"id": album_id}, "s:album/s:releaseTypes"),
        ("getArtist", {"id": "am:br:lossless:artist:1"}, "s:artist/s:album/s:releaseTypes"),
        (
            "search3",
            {"query": "fixture", "artistCount": "0", "songCount": "0"},
            "s:searchResult3/s:album/s:releaseTypes",
        ),
        ("getAlbumList2", {}, "s:albumList2/s:album/s:releaseTypes"),
    ]:
        response = request(server, endpoint, dict(params, f="xml"))
        root = ET.fromstring(response[2])
        assert [element.text for element in root.findall(path, ns)] == expected
