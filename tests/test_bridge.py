import hashlib
import http.client
import json
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import urlencode

import pytest

from apple_music_player.bridge.core import Actor, BridgeError
from apple_music_player.bridge.http import ProtocolCore, Server, auth_file, byte_range
from apple_music_player.catalog import Catalog, CatalogDeferred
from apple_music_player.downloader import Downloader
from apple_music_player.model import Album, Artist, Media, PlayerError, Track

ARTIST = Artist("br", "1", "Fixture")
ALBUM = Album("br", "2", "Record", "Fixture", "1", "Record", release_date="2019-05-24T00:00:00Z")
TRACK = Track("br", "3", "Song", "2", "Fixture", "1", "Record", track_name="Song", duration_ms=1200)
KEY = "test-bridge-secret-" + "x" * 32


class FakeCatalog(Catalog):
    def search(self, query, country, entity="song", *, limit=None):
        assert country == "br"
        return {"musicArtist": [ARTIST], "album": [ALBUM], "song": [TRACK]}[entity]

    def artist_albums(self, identifier, country):
        assert (identifier, country) == ("1", "br")
        return [ALBUM]

    def album(self, identifier, country):
        assert (identifier, country) == ("2", "br")
        return [TRACK]


@pytest.fixture
def bridge(tmp_path, fake_downloader):
    count = []

    class Counted(Downloader):
        def run(self, *args):
            count.append(1)
            return super().run(*args)

    actor = Actor(
        lambda cancel: ProtocolCore(
            tmp_path / "cache",
            1024**2,
            catalog=FakeCatalog(),
            downloader_factory=lambda stage: Counted(fake_downloader, stage),
            cancel=cancel,
        )
    )
    server = Server(actor, {"username": "bridge", "key": KEY}, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    yield server, count
    actor.cancel.set()
    server.shutdown()
    server.server_close()
    thread.join()
    actor.close()


def request(server, endpoint, params=None, method="GET", headers=None, auth=True):
    values = {"v": "1.16.1", "c": "fixture", "f": "json"}
    if auth:
        values["apiKey"] = KEY
    values.update(params or {})
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
    url = "/rest/" + endpoint + ".view"
    data = urlencode(values)
    headers = dict(headers or {})
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, url, data, headers)
    else:
        connection.request(method, url + "?" + data, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def payload(server, endpoint, params=None, **kwargs):
    status, headers, body = request(server, endpoint, params, **kwargs)
    if headers["Content-Type"].startswith("application/xml"):
        import xml.etree.ElementTree as ET

        root = ET.fromstring(body)
        result = dict(root.attrib)
        error = root.find("{http://subsonic.org/restapi}error")
        if error is not None:
            result["error"] = {
                "code": int(error.attrib["code"]),
                "message": error.attrib["message"],
            }
        return result
    return json.loads(body)["subsonic-response"]


def test_auth_all_sensitive_routes_and_secret_free_errors(bridge, capsys):
    server, _ = bridge
    for endpoint in ("ping", "search3", "getArtists", "getCoverArt", "stream"):
        assert payload(server, endpoint, auth=False)["error"]["code"] == 40
        assert payload(server, endpoint, {"apiKey": "wrong"})["error"]["code"] == 40
    assert payload(server, "ping", {"u": "bridge"})["error"]["code"] == 43
    for credentials in (
        {"u": "bridge", "p": KEY},
        {"u": "bridge", "p": "enc:" + KEY.encode().hex()},
        {
            "u": "bridge",
            "s": "randomsalt",
            "t": hashlib.md5((KEY + "randomsalt").encode()).hexdigest(),
        },
    ):
        assert payload(server, "ping", credentials, auth=False, method="POST")["status"] == "ok"
    assert payload(server, "getOpenSubsonicExtensions", auth=False)["status"] == "ok"
    assert request(server, "ping", headers={"Host": "evil.example"})[0] == 403
    assert request(server, "ping", headers={"Origin": "http://evil.example"})[0] == 403
    assert KEY not in str(capsys.readouterr())


def test_search_browse_art_and_stream_ranges(bridge, audio):
    server, count = bridge
    results = payload(server, "search3", {"query": "fixture"})["searchResult3"]
    artist = payload(server, "getArtist", {"id": results["artist"][0]["id"]})["artist"]
    album = payload(server, "getAlbum", {"id": artist["album"][0]["id"]})["album"]
    song = album["song"][0]["id"]
    assert album["songCount"] == 1 and song == "am:br:lossless:song:3"
    art = request(server, "getCoverArt", {"id": album["id"]})
    assert art[2].startswith(b"\x89PNG") and art[1]["X-Artwork-Status"] == "placeholder"
    # Cold HEAD must not trigger a media download.
    assert request(server, "stream", {"id": song}, method="HEAD")[2] == b""
    assert not count
    with ThreadPoolExecutor(3) as pool:
        streams = list(pool.map(lambda _: request(server, "stream", {"id": song}), range(3)))
    expected = audio["alac"].read_bytes()
    assert len(count) == 1
    assert all(result[0] == 200 and result[2] == expected for result in streams)
    assert streams[0][1]["Content-Type"] == "audio/mp4"
    for value, content in [
        ("bytes=0-15", expected[:16]),
        ("bytes=-12", expected[-12:]),
        ("bytes=12-", expected[12:]),
    ]:
        result = request(server, "stream", {"id": song}, headers={"Range": value})
        assert result[0] == 206 and result[2] == content
    invalid = request(server, "stream", {"id": song}, headers={"Range": "bytes=99999999-"})
    assert invalid[0] == 416 and invalid[1]["Content-Range"].startswith("bytes */")
    stale = request(
        server, "stream", {"id": song}, headers={"Range": "bytes=0-1", "If-Range": '"old"'}
    )
    assert stale[0] == 200 and stale[2] == expected
    assert payload(server, "stream", {"id": song, "format": "mp3"})["status"] == "failed"
    assert not server.actor.core.pins


def test_xml_empty_and_invalid_ids(bridge):
    server, _ = bridge
    document = request(server, "ping", {"f": "xml"})[2]
    assert ET.fromstring(document).attrib["status"] == "ok"
    extensions = request(server, "getOpenSubsonicExtensions", {"f": "xml"}, auth=False)[2]
    assert ET.fromstring(extensions).attrib["status"] == "ok"
    assert payload(server, "search3", {"query": ""})["searchResult3"]["album"] == []
    for identifier in ("../../etc/passwd", "am:us:lossless:album:2", "am:br:atmos:album:2"):
        assert payload(server, "getAlbum", {"id": identifier})["error"]["code"] == 70
    assert payload(server, "createPlaylist")["status"] == "failed"
    assert payload(server, "search3", {"albumCount": "999999"})["status"] == "failed"


def test_private_credentials_and_symlinks(tmp_path):
    path = tmp_path / "private" / "auth.json"
    auth_file(path, True)
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(auth_file(path)["key"]) >= 32
    with pytest.raises(FileExistsError):
        auth_file(path, True)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        auth_file(link)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        auth_file(path)
    path.chmod(0o600)
    for invalid in ("[]", '{"username":"bridge","key":123}', " " * 3000):
        path.write_text(invalid)
        with pytest.raises(ValueError):
            auth_file(path)


def test_cache_leases_restart_and_quality(tmp_path, audio):
    root = tmp_path / "cache"
    core = ProtocolCore(root, 1024**2, catalog=FakeCatalog())
    core.remember([TRACK])
    source = core.cache.staging / "audio"
    source.write_bytes(audio["alac"].read_bytes())
    core.cache.put(TRACK.key("lossless"), source, Media("alac"))
    key, file, size, _ = core.acquire(core.identity(TRACK))
    key2, file2, _, _ = core.acquire(core.identity(TRACK))
    core.cache.limit = size
    with pytest.raises(PlayerError):
        core.cache.evict(1)
    file.close()
    core.release(key)
    assert key in core.cache.protected
    file2.close()
    core.release(key2)
    assert not core.cache.protected
    core.close()
    core = ProtocolCore(root, 1024**2, catalog=FakeCatalog())
    try:
        assert core.resolve(core.identity(TRACK)) == TRACK
        assert core.cache.get(TRACK.key("lossless"))
        with pytest.raises(BridgeError):
            core.validate_quality(Media("aac"))
        core.quality = "atmos"
        core.validate_quality(Media("eac3", atmos=False))
        with pytest.raises(BridgeError):
            core.validate_quality(Media("ac3"))
        assert TRACK.key("lossless") != TRACK.key("atmos")
    finally:
        core.close()


def test_bounded_cooldown_and_cancel(tmp_path):
    core = ProtocolCore(tmp_path / "cache", 1024, catalog=FakeCatalog())

    def limited():
        raise CatalogDeferred(3600, True)

    try:
        with pytest.raises(BridgeError):
            core.request(limited)
        core.cancel.set()
        with pytest.raises(BridgeError):
            core.request(lambda: [])
    finally:
        core.close()


@pytest.mark.parametrize("value", ["bytes=1-0", "bytes=-0", "bytes=100-", "bytes=0-1,3-4", "../x"])
def test_invalid_ranges(value):
    with pytest.raises(ValueError):
        byte_range(value, 10)


def test_failed_downloader_does_not_leak_output(bridge, monkeypatch):
    server, _ = bridge
    payload(server, "search3", {"query": "fixture"})
    monkeypatch.setenv("TEST_MODE", "zero")
    response = request(server, "stream", {"id": "am:br:lossless:song:3"})
    assert b"TEST_SECRET" not in response[2]
    assert json.loads(response[2])["subsonic-response"]["status"] == "failed"
    assert list(server.actor.core.cache.staging.iterdir()) == []


def test_shutdown_cancels_only_bridge_download(bridge, monkeypatch, tmp_path):
    server, _ = bridge
    payload(server, "search3", {"query": "fixture"})
    monkeypatch.setenv("TEST_MODE", "hang")
    pid_file = tmp_path / "child-pid"
    monkeypatch.setenv("TEST_PID", str(pid_file))
    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(request, server, "stream", {"id": "am:br:lossless:song:3"})
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        server.actor.cancel.set()
        assert json.loads(result.result(timeout=5)[2])["subsonic-response"]["status"] == "failed"
    assert list(server.actor.core.cache.staging.iterdir()) == []


def test_stable_song_id_resolves_after_metadata_loss(tmp_path):
    catalog = FakeCatalog()
    calls = []

    def lookup(endpoint, **params):
        calls.append((endpoint, params))
        return [
            dict(
                wrapperType="track",
                kind="song",
                trackId=3,
                collectionId=2,
                trackName="Song",
                artistName="Fixture",
                artistId=1,
            )
        ]

    catalog.request = lookup
    core = ProtocolCore(tmp_path / "cache", 1024, catalog=catalog)
    try:
        assert core.resolve("am:br:lossless:song:3").id == "3"
        assert calls == [("lookup", {"id": "3", "country": "br"})]
    finally:
        core.close()


def test_runtime_ownership_is_explicit_and_mocked(tmp_path, monkeypatch):
    from apple_music_player.bridge import core as module

    calls = []

    class Store:
        downloader = "/not-executed/amdl"

        def __init__(self, root):
            calls.append("store")

    class Runtime:
        def __init__(self, store):
            self.store = store

        def signed_in(self):
            return True

        def start(self):
            calls.append("start")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(module, "RuntimeStore", Store)
    monkeypatch.setattr(module, "ManagedRuntime", Runtime)
    core = ProtocolCore(tmp_path / "cache", 1024, runtime_root=tmp_path / "runtime")
    assert calls == ["store", "start"]
    core.close()
    assert calls == ["store", "start", "close"]


def test_supersonic_library_artist_info_and_album_year(bridge):
    server, _ = bridge
    folders = payload(server, "getMusicFolders")["musicFolders"]["musicFolder"]
    assert type(folders[0]["id"]) is int  # Go MusicFolder.ID, not an opaque media ID.
    results = payload(server, "search3", {"query": "fixture"})["searchResult3"]
    for kind in ("artist", "album", "song"):
        identifier = results[kind][0]["id"]
        info = payload(server, "getArtistInfo2", {"id": identifier})
        assert info["status"] == "ok"
        assert info["artistInfo2"] == {"similarArtist": []}
    assert payload(server, "getArtistInfo2", auth=False)["error"]["code"] == 40
    for identifier in ("", "../../etc/passwd", "am:us:lossless:artist:1"):
        assert payload(server, "getArtistInfo2", {"id": identifier})["error"]["code"] == 70
    album_id = results["album"][0]["id"]
    album = payload(server, "getAlbum", {"id": album_id})["album"]
    assert type(album["year"]) is int and album["year"] == 2019
    assert album["releaseDate"] == {"year": 2019, "month": 5, "day": 24}
    assert "originalReleaseDate" not in album
    assert results["album"][0]["year"] == 2019
    ns = {"s": "http://subsonic.org/restapi"}
    folders_xml = ET.fromstring(request(server, "getMusicFolders", {"f": "xml"})[2])
    assert folders_xml.find("s:musicFolders/s:musicFolder", ns).attrib["id"] == "1"
    document = ET.fromstring(request(server, "getAlbum", {"id": album_id, "f": "xml"})[2])
    assert document.find("s:album", ns).attrib["year"] == "2019"
    info_xml = ET.fromstring(request(server, "getArtistInfo2", {"id": album_id, "f": "xml"})[2])
    assert info_xml.find("s:artistInfo2", ns) is not None


@pytest.mark.parametrize("released", ["", "invalid", "2019-02-30", "0000-01-01"])
def test_unknown_release_dates_are_not_invented(bridge, released):
    server, _ = bridge
    for item in (ALBUM, TRACK):
        result = server.actor.call("describe", replace(item, release_date=released))
        assert "year" not in result and "releaseDate" not in result
    song = server.actor.call("describe", replace(TRACK, release_date="2001-01-02T00:00:00Z"))
    assert song["year"] == 2001
