"""Synthetic provider records and pictures; no accounts or live image downloads."""

import base64
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
from test_bridge import ARTIST, payload, request
from test_bridge import bridge as _bridge
from test_bridge_artwork import image_bytes

from apple_music_player.artwork import ArtworkRedirect
from apple_music_player.bridge import artist_images as mod
from apple_music_player.bridge.artwork import CoverUnavailable
from apple_music_player.model import PlayerError

bridge = _bridge
MBID = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
COMMONS = "https://commons.wikimedia.org/wiki/File:Fixture.jpg"
IMAGE = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Fixture.jpg/500px-Fixture.jpg"


def info_record():
    return {
        "descriptionurl": COMMONS,
        "thumburl": IMAGE,
        "extmetadata": {
            "Artist": {"value": '<a href="https://example.invalid">Fixture photographer</a>'},
            "Credit": {"value": "Fixture collection"},
            "LicenseShortName": {"value": "CC BY-SA 4.0"},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
        },
    }


def credits():
    return mod.provenance(info_record(), MBID, "MusicBrainz image relationship")


def provider(monkeypatch, *, wiki=False, ambiguous=False, license_missing=False):
    calls = []

    def fetch(url, stop, image=False):
        assert not stop.is_set()
        calls.append(url)
        parts = urlsplit(url)
        params = parse_qs(parts.query)
        if parts.path == "/ws/2/url":
            assert params["inc"] == ["artist-rels"]
            assert all("name" not in k and "query" != k for k in params)
            relations = [{"target-type": "artist", "artist": {"id": MBID}}]
            if ambiguous:
                relations.append({"target-type": "artist", "artist": {"id": OTHER}})
            data = {"urls": [{"resource": params["resource"][0], "relations": relations}]}
        elif parts.path.startswith("/ws/2/artist/"):
            data = {
                "id": MBID,
                "relations": [
                    {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q1"}}
                    if wiki
                    else {"type": "image", "url": {"resource": COMMONS}}
                ],
            }
        elif parts.hostname == "www.wikidata.org":
            assert params["ids"] == ["Q1"] and params["maxlag"] == ["5"]
            data = {
                "entities": {
                    "Q1": {
                        "id": "Q1",
                        "claims": {
                            "P18": [
                                {
                                    "rank": "normal",
                                    "mainsnak": {
                                        "snaktype": "value",
                                        "datavalue": {"value": "Fixture.jpg"},
                                    },
                                }
                            ]
                        },
                    }
                }
            }
        elif parts.hostname == "commons.wikimedia.org":
            assert params["titles"] == ["File:Fixture.jpg"] and params["maxlag"] == ["5"]
            info = info_record()
            if license_missing:
                info["extmetadata"].pop("LicenseUrl")
            data = {"query": {"pages": {"1": {"imageinfo": [info]}}}}
        else:
            assert url == IMAGE and image
            return image_bytes("JPEG")
        return json.dumps(data).encode()

    monkeypatch.setattr(mod, "fetch", fetch)
    return calls


@pytest.mark.parametrize("wiki", [False, True])
def test_exact_relationship_then_direct_commons_or_wikidata(monkeypatch, wiki):
    calls = provider(monkeypatch, wiki=wiki)
    image, credit = mod.resolve(ARTIST, threading.Event())
    assert image.startswith(b"\x89PNG")
    assert len(calls) == (5 if wiki else 4)
    assert credit["creator"] == "Fixture photographer"
    assert credit["sourceUrl"] == COMMONS and credit["license"] == "CC BY-SA 4.0"
    assert credit["origin"] == ("Wikidata P18" if wiki else "MusicBrainz image relationship")
    assert credit["imageKind"] == "artist-image"  # Not a claim that every photo is a portrait.
    assert mod.checked_credits(credit) == credit


@pytest.mark.parametrize("ambiguous,missing", [(True, False), (False, True)])
def test_ambiguous_mapping_or_missing_license_never_fetches_image(monkeypatch, ambiguous, missing):
    calls = provider(monkeypatch, ambiguous=ambiguous, license_missing=missing)
    with pytest.raises(mod.Missing):
        mod.resolve(ARTIST, threading.Event())
    assert IMAGE not in calls
    if ambiguous:
        assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://upload.wikimedia.org/wikipedia/commons/a.jpg",
        "https://upload.wikimedia.org.evil.invalid/wikipedia/commons/a.jpg",
        "https://127.0.0.1/wikipedia/commons/a.jpg",
        "https://user@upload.wikimedia.org/wikipedia/commons/a.jpg",
        "https://upload.wikimedia.org:444/wikipedia/commons/a.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/a.jpg?secret=x",
    ],
)
def test_rejects_unsafe_image_urls_and_redirects(url):
    assert not mod.image_url(url)
    with pytest.raises(ValueError):
        ArtworkRedirect(mod.image_url).redirect_request(None, None, 302, "", {}, url)


def test_apple_url_identity_rejects_wrong_id_and_external_urls():
    for url in ("https://evil.invalid/artist/1", "https://music.apple.com/br/artist/999"):
        assert url not in mod.resources(replace(ARTIST, store_url=url))
    urls = mod.resources(replace(ARTIST, store_url="https://music.apple.com/br/artist/name/1?uo=4"))
    assert "https://music.apple.com/br/artist/name/1" in urls
    assert "https://music.apple.com/us/artist/1" in urls
    assert all("?" not in url for url in urls)


def test_cache_restart_misses_corruption_and_symlinks(tmp_path, monkeypatch):
    calls = provider(monkeypatch)
    service = mod.ArtistImages(tmp_path / "art")
    assert service.info(ARTIST) == {"status": "pending"} and calls == []
    image, credit = service.get(ARTIST)
    service.close()
    restarted = mod.ArtistImages(tmp_path / "art")
    assert restarted.get(ARTIST) == (image, credit) and len(calls) == 4
    assert restarted.info(ARTIST)["credits"] == credit
    path = restarted.root / (restarted.key(ARTIST) + ".json")
    path.write_text("bad json")
    assert restarted.get(ARTIST)[0] and len(calls) == 8
    path.unlink()
    target = tmp_path / "outside"
    target.write_text("untouched")
    path.symlink_to(target)
    with pytest.raises(PlayerError):
        restarted.get(ARTIST)
    assert target.read_text() == "untouched"
    restarted.close()


@pytest.mark.parametrize("failure,status", [(mod.Missing, "unavailable"), (ValueError, "retry")])
def test_negative_cache_and_expiry(tmp_path, monkeypatch, failure, status):
    clock = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: clock[0])
    calls = []

    def resolve(*_):
        calls.append(1)
        raise failure()

    monkeypatch.setattr(mod, "resolve", resolve)
    service = mod.ArtistImages(tmp_path / "art")
    try:
        for _ in range(2):
            if failure is mod.Missing:
                assert service.get(ARTIST) == (b"", {"reason": "image-unavailable"})
            else:
                with pytest.raises(CoverUnavailable):
                    service.get(ARTIST)
        assert len(calls) == 1 and service.info(ARTIST)["status"] == status
        clock[0] += 86401
        try:
            service.get(ARTIST)
        except CoverUnavailable:
            pass
        assert len(calls) == 2
    finally:
        service.close()


def test_musicbrainz_spacing_cooldown_and_cancellation(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])

    class ClockStop:
        def wait(self, seconds):
            now[0] += seconds
            return False

    gate = mod.Gate()
    gate.enter(ClockStop())
    gate.enter(ClockStop())
    assert now[0] == pytest.approx(101.1)
    gate.throttle("120")
    with pytest.raises(CoverUnavailable):
        gate.enter(ClockStop())
    now[0] += 121
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(CoverUnavailable):
        gate.enter(stopped)


def test_fetch_headers_caps_mime_and_retry_after(monkeypatch):
    monkeypatch.setattr(mod, "MB_GATE", mod.Gate())
    headers = Message()
    headers["Content-Type"] = "application/json"
    seen = []

    class Response(io.BytesIO):
        url = "https://musicbrainz.org/ws/2/url?fmt=json"

    Response.headers = headers

    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            return Response(b"x" * (256 * 1024 + 1))

    monkeypatch.setattr(mod, "build_opener", lambda *_: Opener())
    with pytest.raises(mod.ResolutionFailure, match="musicbrainz-invalid"):
        mod.fetch(Response.url, threading.Event())
    assert "github.com/gqgs/apple-music-player" in seen[0].get_header("User-agent")
    assert seen[0].get_header("Authorization") is None

    class Throttled:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 503, "private body", {"Retry-After": "300"}, None)

    monkeypatch.setattr(mod, "build_opener", lambda *_: Throttled())
    with pytest.raises(CoverUnavailable):
        mod.fetch(Response.url, threading.Event())
    assert mod.MB_GATE.blocked > mod.time.monotonic() + 299


def test_http_lookup_does_not_block_search_and_preserves_credits(bridge, monkeypatch, tmp_path):
    server, _ = bridge
    server.actor.core.artist_images = True
    server.artist_images = mod.ArtistImages(tmp_path / "artist-images")
    entered, release = threading.Event(), threading.Event()

    def resolve(*_):
        entered.set()
        assert release.wait(3)
        return image_bytes("PNG"), credits()

    monkeypatch.setattr(mod, "resolve", resolve)
    server.actor.call("remember", [ARTIST])
    params = {"query": "fixture", "artistCount": "1", "albumCount": "0", "songCount": "0"}
    artist = payload(server, "search3", params)["searchResult3"]["artist"][0]
    assert artist["coverArt"].startswith("artist-image:") and not entered.is_set()
    identifier = artist["id"]
    with ThreadPoolExecutor(2) as pool:
        image = pool.submit(request, server, "getCoverArt", {"id": artist["coverArt"]})
        assert entered.wait(1)
        try:
            assert (
                pool.submit(payload, server, "search3", params).result(timeout=1)["status"] == "ok"
            )
            assert (
                payload(server, "getArtistArtworkInfo", {"id": identifier})["artistArtworkInfo"][
                    "status"
                ]
                == "pending"
            )
        finally:
            release.set()
        assert image.result(timeout=1)[1]["X-Artwork-Kind"] == "artist-image"
    info = payload(server, "getArtistArtworkInfo", {"id": identifier})["artistArtworkInfo"]
    assert info["status"] == "ready" and info["credits"]["sourceUrl"] == COMMONS
    assert (
        payload(server, "getArtistArtworkInfo", {"id": identifier}, auth=False)["error"]["code"]
        == 40
    )
    bio = payload(server, "getArtistInfo2", {"id": identifier})["artistInfo2"]["biography"]
    assert "not a biography" in bio and "Fixture photographer" in bio and "CC BY-SA" in bio
    assert "Fixture collection" in bio


def test_bounded_jobs_duplicates_and_shutdown(tmp_path, monkeypatch):
    import time

    service = mod.ArtistImages(tmp_path / "art")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def resolve(artist, stop):
        calls.append(artist.id)
        entered.set()
        assert release.wait(3)
        if stop.is_set():
            raise CoverUnavailable()
        return image_bytes("PNG"), credits()

    monkeypatch.setattr(mod, "resolve", resolve)
    try:
        with ThreadPoolExecutor(9) as pool:
            first = pool.submit(service.get, ARTIST)
            assert entered.wait(1)
            duplicate = pool.submit(service.get, ARTIST)
            jobs = [pool.submit(service.get, replace(ARTIST, id=str(i))) for i in range(2, 9)]
            end = time.monotonic() + 1
            while len(service.pending) != 8 and time.monotonic() < end:
                threading.Event().wait(0.01)
            assert len(service.pending) == 8
            with pytest.raises(CoverUnavailable):
                service.get(replace(ARTIST, id="99"))
            release.set()
            assert first.result(timeout=1) == duplicate.result(timeout=1)
            assert all(job.result(timeout=2)[0] for job in jobs)
        assert sorted(calls) == [str(i) for i in range(1, 9)]
        service.close()
        with pytest.raises(CoverUnavailable):
            service.get(replace(ARTIST, id="99"))
    finally:
        release.set()
        service.close()


def test_missing_identity_and_wikidata_ambiguity_fail_closed(monkeypatch):
    original_calls = provider(monkeypatch, wiki=True)
    original = mod.fetch

    def modified(url, stop, image=False):
        result = original(url, stop, image=image)
        if urlsplit(url).hostname == "www.wikidata.org":
            data = json.loads(result)
            claims = data["entities"]["Q1"]["claims"]["P18"]
            claims.append(
                {
                    "rank": "normal",
                    "mainsnak": {"snaktype": "value", "datavalue": {"value": "Someone else.jpg"}},
                }
            )
            return json.dumps(data).encode()
        return result

    monkeypatch.setattr(mod, "fetch", modified)
    with pytest.raises(mod.Missing):
        mod.resolve(ARTIST, threading.Event())
    assert len(original_calls) == 3
    monkeypatch.setattr(mod, "fetch", lambda *_: b'{"urls": []}')
    with pytest.raises(mod.Missing):
        mod.resolve(ARTIST, threading.Event())


def test_cache_evicts_and_rejects_corrupt_attribution(tmp_path):
    import time

    service = mod.ArtistImages(tmp_path / "art")
    try:
        for i in range(130):
            service.write(f"{i:064x}", {"status": "missing", "until": time.time() + 60})
        assert len(list(service.root.glob("*.json"))) == 128
        corrupt = credits()
        corrupt["sourceUrl"] = "https://evil.invalid/image"
        service.write(
            service.key(ARTIST),
            {
                "status": "ok",
                "until": time.time() + 60,
                "image": base64.b64encode(image_bytes("PNG")).decode(),
                "credits": corrupt,
            },
        )
        assert service.info(ARTIST) == {"status": "pending"}
        assert service.root.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in service.root.glob("*.json"))
    finally:
        service.close()


def test_shutdown_interrupts_running_and_cancels_queued_lookup(tmp_path, monkeypatch):
    import time

    service = mod.ArtistImages(tmp_path / "art")
    entered = threading.Event()
    calls = []

    def resolve(artist, stop):
        calls.append(artist.id)
        entered.set()
        assert stop.wait(3)
        raise CoverUnavailable()

    monkeypatch.setattr(mod, "resolve", resolve)
    try:
        with ThreadPoolExecutor(2) as pool:
            running = pool.submit(service.get, ARTIST)
            assert entered.wait(1)
            queued = pool.submit(service.get, replace(ARTIST, id="2"))
            end = time.monotonic() + 1
            while len(service.pending) != 2 and time.monotonic() < end:
                threading.Event().wait(0.01)
            assert len(service.pending) == 2
            service.close()
            for job in (running, queued):
                with pytest.raises(CoverUnavailable):
                    job.result(timeout=1)
        assert calls == [ARTIST.id]
    finally:
        service.close()


@pytest.mark.parametrize(
    "failure,reason",
    [
        (mod.ResolutionFailure("musicbrainz-http-503"), "musicbrainz-http-503"),
        (mod.Missing("identity-missing"), "identity-missing"),
        (ValueError("SECRET raw body"), "invalid-response"),
    ],
)
def test_artist_failure_is_xml_not_cached_placeholder(
    bridge, monkeypatch, tmp_path, failure, reason
):
    import xml.etree.ElementTree as ET

    server, _ = bridge
    server.actor.core.artist_images = True
    server.artist_images = mod.ArtistImages(tmp_path / "images")
    server.actor.call("remember", [ARTIST])
    calls = []

    def resolve(*args):
        calls.append(1)
        raise failure

    monkeypatch.setattr(mod, "resolve", resolve)
    for _ in range(2):
        status, headers, body = request(
            server, "getCoverArt", {"id": "artist-image:am:br:lossless:artist:1"}
        )
        assert status == 503
        assert headers["Content-Type"].startswith("application/xml")
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Artwork-Reason"] == reason
        error = ET.fromstring(body).find("{http://subsonic.org/restapi}error")
        assert reason in error.attrib["message"]
        assert b"SECRET" not in body
    assert len(calls) == 1
    info = payload(server, "getArtistArtworkInfo", {"id": "am:br:lossless:artist:1"})
    assert info["artistArtworkInfo"]["reason"] == reason


def test_provider_network_reason_does_not_expose_exception(monkeypatch):
    from urllib.error import URLError

    monkeypatch.setattr(mod, "MB_GATE", mod.Gate())

    class Opener:
        def open(self, *args, **kwargs):
            raise URLError("SECRET proxy configuration")

    monkeypatch.setattr(mod, "build_opener", lambda *_: Opener())
    with pytest.raises(mod.ResolutionFailure, match="^musicbrainz-network$"):
        mod.fetch("https://musicbrainz.org/ws/2/url?fmt=json", threading.Event())


@pytest.mark.parametrize("wiki", [False, True])
def test_http_full_provider_pipeline_returns_decodable_png(bridge, monkeypatch, tmp_path, wiki):
    from PySide6.QtGui import QImage

    server, _ = bridge
    server.actor.core.artist_images = True
    server.artist_images = mod.ArtistImages(tmp_path / "images")
    server.actor.call("remember", [ARTIST])
    calls = provider(monkeypatch, wiki=wiki)
    result = request(server, "getCoverArt", {"id": "artist-image:am:br:lossless:artist:1"})
    assert result[0] == 200 and result[1]["Content-Type"] == "image/png"
    assert not QImage.fromData(result[2]).isNull()
    assert len(calls) == (5 if wiki else 4)
    info = payload(server, "getArtistArtworkInfo", {"id": "am:br:lossless:artist:1"})
    assert info["artistArtworkInfo"]["status"] == "ready"


def test_missing_direct_image_credits_reason_is_preserved(monkeypatch):
    provider(monkeypatch, license_missing=True)
    with pytest.raises(mod.Missing) as error:
        mod.resolve(ARTIST, threading.Event())
    assert error.value.reason == "attribution-unavailable"


@pytest.mark.parametrize("country", ["us", "br", "gb"])
def test_itunes_canonical_forms_keep_country_and_id_prefix(country):
    artist = replace(
        ARTIST, store_url=f"https://itunes.apple.com/{country}/artist/fixture/id1?uo=4"
    )
    urls = mod.resources(artist)
    assert f"https://itunes.apple.com/{country}/artist/id1" in urls
    assert "https://itunes.apple.com/us/artist/id1" in urls
    assert "https://itunes.apple.com/br/artist/id1" in urls
    assert f"https://itunes.apple.com/{country}/artist/1" not in urls
    assert len(urls) == len(set(urls)) and len(urls) < 12


@pytest.mark.parametrize("country", ["us", "br", "gb"])
def test_search_gallery_portrait_canonical_itunes_and_restart(tmp_path, monkeypatch, country):
    import time
    from contextlib import contextmanager
    from hashlib import sha256

    from PySide6.QtGui import QImage
    from test_bridge import KEY

    from apple_music_player.bridge.core import Actor
    from apple_music_player.bridge.http import ProtocolCore, Server
    from apple_music_player.catalog import Catalog

    # Synthetic artist; URL forms and relationship shape follow MusicBrainz source.
    store_url = f"https://itunes.apple.com/{country}/artist/fixture/id1?uo=4"
    rows = [
        {
            "wrapperType": "artist",
            "artistId": 1,
            "artistName": "Fixture",
            "artistLinkUrl": store_url,
        }
    ]

    class FixtureCatalog(Catalog):
        def search(self, query, country, entity="song", *, limit=None):
            assert entity == "musicArtist" and country == "br"
            return self.artists(rows, country)

    calls = provider(monkeypatch)
    original = mod.fetch
    canonical = f"https://itunes.apple.com/{country}/artist/id1"

    def source_fetch(url, stop, image=False):
        if urlsplit(url).path == "/ws/2/url":
            resources = parse_qs(urlsplit(url).query)["resource"]
            if canonical not in resources:
                return b'{"urls": [], "url-count": 0, "url-offset": 0}'
            data = json.loads(original(url, stop, image=image))
            data["urls"][0]["resource"] = canonical
            return json.dumps(data).encode()
        return original(url, stop, image=image)

    monkeypatch.setattr(mod, "fetch", source_fetch)

    @contextmanager
    def running():
        actor = Actor(
            lambda cancel: ProtocolCore(
                tmp_path / "cache",
                1024**2,
                catalog=FixtureCatalog(),
                cancel=cancel,
                artist_images=True,
            )
        )
        server = Server(actor, {"username": "bridge", "key": KEY}, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield server
        finally:
            actor.cancel.set()
            server.shutdown()
            server.server_close()
            thread.join()
            actor.close()

    with running() as server:
        found = payload(
            server,
            "search3",
            {"query": "fixture", "artistCount": "1", "albumCount": "0", "songCount": "0"},
        )["searchResult3"]["artist"][0]
        known = payload(server, "getArtists")["artists"]["index"][0]["artist"][0]
        assert known == found
        artist = server.actor.core.cached_artist(known["id"])
        assert artist.store_url == store_url
        old_resources = list(
            dict.fromkeys(
                [
                    "https://music.apple.com/artist/1",
                    "https://itunes.apple.com/artist/id1",
                    "https://music.apple.com/br/artist/1",
                    "https://music.apple.com/us/artist/1",
                    store_url.split("?")[0],
                    f"https://itunes.apple.com/{country}/artist/1",
                ]
            )
        )
        old_key = sha256(json.dumps(["br", "1", old_resources]).encode()).hexdigest()
        server.artist_images.write(
            old_key,
            {"status": "missing", "reason": "identity-missing", "until": time.time() + 86400},
        )
        assert old_key != server.artist_images.key(artist)
        response = request(server, "getCoverArt", {"id": known["coverArt"]})
        assert response[0] == 200 and not QImage.fromData(response[2]).isNull()
        assert len(calls) == 4  # One multi-resource request, not extra per-URL requests.
        assert (server.artist_images.root / (old_key + ".json")).exists()

    with running() as server:
        restored = payload(server, "getArtists")["artists"]["index"][0]["artist"][0]
        assert restored == known
        assert server.actor.core.cached_artist(restored["id"]).store_url == store_url
        response = request(server, "getCoverArt", {"id": restored["coverArt"]})
        assert response[0] == 200 and not QImage.fromData(response[2]).isNull()
        assert len(calls) == 4  # Positive cache survives full server/SQLite restart.


def test_album_cover_during_pending_portrait_and_portrait_success_cache_policy(
    bridge, monkeypatch, tmp_path
):
    from PySide6.QtGui import QImage
    from test_bridge import ALBUM
    from test_bridge_artwork import raw_cover_response

    server, _ = bridge
    server.actor.core.artist_images = True
    server.artist_images = mod.ArtistImages(tmp_path / "portraits")
    server.actor.call("remember", [ARTIST, ALBUM])
    entered, release = threading.Event(), threading.Event()

    def resolve(*_):
        entered.set()
        assert release.wait(3)
        return image_bytes("PNG"), credits()

    monkeypatch.setattr(mod, "resolve", resolve)
    monkeypatch.setattr(server.covers, "get", lambda *args: image_bytes("PNG"))
    with ThreadPoolExecutor(2) as pool:
        portrait = pool.submit(raw_cover_response, server, "artist-image:am:br:lossless:artist:1")
        assert entered.wait(1)
        try:
            album = pool.submit(raw_cover_response, server, "am:br:lossless:album:2").result(
                timeout=1
            )
            assert album[0] == 200 and not QImage.fromData(album[2]).isNull()
            assert album[1].get_all("Cache-Control") == ["private, max-age=86400"]
        finally:
            release.set()
        status, headers, body = portrait.result(timeout=1)
        assert status == 200 and not QImage.fromData(body).isNull()
        assert headers.get_all("Cache-Control") == ["private, max-age=86400"]


def test_pending_portrait_http_is_503_then_completed_lookup_is_png(bridge, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from test_bridge_artwork import raw_cover_response

    server, _ = bridge
    server.actor.core.artist_images = True
    service = server.artist_images = mod.ArtistImages(tmp_path / "portraits")
    server.actor.call("remember", [ARTIST])
    entered, release = threading.Event(), threading.Event()

    def resolve(*_):
        entered.set()
        assert release.wait(3)
        return image_bytes("PNG"), credits()

    monkeypatch.setattr(mod, "resolve", resolve)
    submit = service.pool.submit
    jobs = []

    def short_wait(*args, **kwargs):
        future = submit(*args, **kwargs)
        jobs.append(future)
        return SimpleNamespace(
            result=lambda **kw: future.result(timeout=0.02),
            add_done_callback=future.add_done_callback,
        )

    monkeypatch.setattr(service.pool, "submit", short_wait)
    try:
        status, headers, body = raw_cover_response(server, "artist-image:am:br:lossless:artist:1")
        assert entered.is_set() and status == 503
        assert headers.get_all("Cache-Control") == ["no-store"]
        assert headers["X-Artwork-Reason"] == "lookup-pending"
        assert headers.get_content_type() == "application/xml" and not body.startswith(b"\x89PNG")
        assert service.info(ARTIST)["status"] == "pending"
    finally:
        release.set()
    jobs[0].result(timeout=1)
    status, headers, body = raw_cover_response(server, "artist-image:am:br:lossless:artist:1")
    assert status == 200 and body.startswith(b"\x89PNG")
    assert headers.get_all("Cache-Control") == ["private, max-age=86400"]
    assert len(jobs) == 1
