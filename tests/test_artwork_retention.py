import json
import threading
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from test_bridge import ARTIST
from test_release_artwork import ID, ready_album

from apple_music_player.bridge.core import BridgeError, Core
from apple_music_player.bridge.metrics import Metrics
from apple_music_player.bridge.release_artwork import ReleaseArtwork, ReleaseUnavailable


def service(core, albums=()):
    idle = threading.Event()
    idle.set()
    return ReleaseArtwork(
        SimpleNamespace(core=core, metrics=Metrics(), metadata_idle=idle, last_metadata=0),
        SimpleNamespace(artist_albums=lambda *_, albums=albums: albums),
    )


@pytest.mark.parametrize("enrich", [False, True])
def test_32_artists_200_albums_survive_pruning_and_restart(tmp_path, enrich):
    core = Core(tmp_path, 1024)
    artists = [replace(ARTIST, id=str(i)) for i in range(1, 33)]
    core.remember(artists)
    art = service(core)
    for artist in artists:
        albums = [
            ready_album(id=str(int(artist.id) * 1000 + n), artist_id=artist.id) for n in range(200)
        ]
        if enrich:
            art.catalog = SimpleNamespace(artist_albums=lambda *_, albums=albums: albums)
            art.enrich(core.identity(artist), artist)
        else:
            core.remember(albums)
    assert core.cache.db.execute("SELECT count(*) FROM bridge_metadata").fetchone() == (5000,)
    assert core.cache.db.execute(
        "SELECT count(*) FROM bridge_metadata WHERE id LIKE '%:artist:%'"
    ).fetchone() == (0,)
    for artist in artists:
        key = core.identity(artist)
        assert core.cached_artist(key) == artist
        assert art.get(key).artist_id == artist.id
        assert art.info(key)["status"] == "ready"
    art.close()
    core.close()
    core = Core(tmp_path, 1024)
    art = service(core)
    try:
        assert art.get(ID).artist_id == "1"
        # Re-browsing and newer releases improve the retained selection.
        core.remember([ARTIST, ready_album(id="999999", release_date="2025-01-01")])
        assert art.get(ID).id == "999999"
        for bad in [
            ID.replace(":br:", ":us:"),
            ID.replace("lossless", "atmos"),
            ID.replace(":1", ":999"),
        ]:
            with pytest.raises(BridgeError):
                art.get(bad)
    finally:
        art.close()
        core.close()


def test_migration_expires_ambiguous_miss_once_and_preserves_cache(tmp_path):
    core = Core(tmp_path, 1024)
    core.remember([ARTIST, ready_album()])
    core.cache.db.execute("DROP TABLE bridge_artist_artwork")
    core.cache.db.execute(
        "INSERT INTO bridge_release_checks VALUES (?,?,?)", (ID, 9999999999, "release-unavailable")
    )
    core.cache.db.commit()
    marker = tmp_path / "keep-image-cache"
    marker.write_bytes(b"unchanged")
    core.close()
    core = Core(tmp_path, 1024)
    art = service(core)
    assert art.get(ID).id == ready_album().id
    assert core.cache.db.execute("SELECT retry_after FROM bridge_release_checks").fetchone() == (0,)
    core.cache.db.execute("UPDATE bridge_release_checks SET retry_after=9999999999")
    core.cache.db.commit()
    art.close()
    core.close()
    core = Core(tmp_path, 1024)
    try:
        assert marker.read_bytes() == b"unchanged"
        assert core.cache.db.execute(
            "SELECT retry_after FROM bridge_release_checks"
        ).fetchone() == (9999999999,)
    finally:
        core.close()


def test_collaborator_retained_after_association_and_metadata_eviction(tmp_path):
    core = Core(tmp_path, 1024)
    core.remember([ARTIST, replace(ARTIST, id="3")])
    album = ready_album(artist_id="99")
    art = service(core, [album])
    try:
        core.remember([album])
        assert art.selection(ARTIST) is None
        art.enrich(ID, ARTIST)
        core.cache.db.execute("DELETE FROM bridge_metadata")
        core.cache.db.execute("DELETE FROM bridge_artist_releases")
        core.cache.db.commit()
        assert art.get(ID) == album
        assert art.selection(replace(ARTIST, id="3")) is None
        # Corrupt or cross-profile saved payloads must trigger repair, not 404.
        for raw in ["broken", json.dumps(asdict(replace(album, country="us")))]:
            core.cache.db.execute("DELETE FROM bridge_metadata")
            core.cache.db.execute("UPDATE bridge_artist_artwork SET album=? WHERE id=?", (raw, ID))
            core.cache.db.commit()
            with pytest.raises(ReleaseUnavailable) as error:
                art.get(ID)
            assert error.value.reason == "release-pending"
            with art.lock:
                jobs = list(art.pending.values())
            for job in jobs:
                job.result(timeout=2)
            assert art.get(ID) == album
    finally:
        art.close()
        core.close()


def test_retention_bound_and_true_negative(tmp_path):
    core = Core(tmp_path, 1024)
    art = service(core)
    try:
        core.remember(replace(ARTIST, id=str(i)) for i in range(1, 5002))
        assert core.cache.db.execute("SELECT count(*) FROM bridge_artist_artwork").fetchone() == (
            5000,
        )
        with pytest.raises(BridgeError):
            core.cached_artist(ID)
        core.remember([ARTIST])
        art.enrich(ID, ARTIST)
        with pytest.raises(ReleaseUnavailable) as error:
            art.get(ID)
        assert error.value.reason == "release-unavailable"
        assert error.value.status == 404
        # A newly browsed release supersedes even a confirmed negative.
        core.remember([ready_album()])
        assert art.get(ID).id == ready_album().id
    finally:
        art.close()
        core.close()


def test_upgrade_repairs_old_evicted_success_and_validates_artist_payload(tmp_path):
    core = Core(tmp_path, 1024)
    # Old successful lookup after every artist row and its releases were evicted.
    core.cache.db.execute("DROP TABLE bridge_artist_artwork")
    core.cache.db.execute(
        "INSERT INTO bridge_release_checks VALUES (?,?,?)",
        (ID, 9999999999, "release-unavailable"),
    )
    core.cache.db.commit()
    core.close()
    core = Core(tmp_path, 1024)
    art = service(core, [ready_album()])
    try:
        core.remember([ARTIST])
        with pytest.raises(ReleaseUnavailable) as error:
            art.get(ID)
        assert error.value.reason == "release-pending"
        with art.lock:
            jobs = list(art.pending.values())
        for job in jobs:
            job.result(timeout=2)
        assert art.get(ID).id == ready_album().id
        core.cache.db.execute(
            "UPDATE bridge_artist_artwork SET data=? WHERE id=?",
            (json.dumps(asdict(replace(ARTIST, id="99"))), ID),
        )
        core.cache.db.commit()
        with pytest.raises(BridgeError, match="Unknown artist"):
            art.get(ID)
    finally:
        art.close()
        core.close()


def test_retained_artwork_is_quality_scoped(tmp_path):
    core = Core(tmp_path, 1024)
    core.remember([ARTIST, ready_album()])
    core.cache.db.execute("DELETE FROM bridge_metadata")
    core.cache.db.commit()
    core.close()
    core = Core(tmp_path, 1024, quality="atmos")
    art = service(core)
    try:
        with pytest.raises(BridgeError):
            art.get(ID)
        with pytest.raises(BridgeError):
            art.get(ID.replace("lossless", "atmos"))
        core.remember([ARTIST])
        assert art.selection(ARTIST) is None
    finally:
        art.close()
        core.close()


@pytest.mark.parametrize("writer", ["remember", "enrich"])
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("correction", ["empty_art", "future", "backdated", "reassigned"])
def test_same_album_corrections_replace_before_ranking(tmp_path, writer, fallback, correction):
    core = Core(tmp_path, 1024)
    other = replace(ARTIST, id="99")
    original = ready_album(id="2", release_date="2025-01-01")
    older = ready_album(id="3", release_date="2022-01-01")
    core.remember([ARTIST, other, original, *([older] if fallback else [])])
    art = service(core)
    changes = {
        "empty_art": {"artwork_url": ""},
        "future": {"release_date": "2099-01-01"},
        "backdated": {"release_date": "2020-01-01"},
        "reassigned": {"artist_id": "99"},
    }
    updated = replace(original, **changes[correction])
    assert art.get(ID) == original
    if writer == "remember":
        core.remember([updated])
    else:
        # Admit the lookup before metadata arrives, then reproduce a correction
        # returned by that in-flight lookup. A queued lookup with an already
        # usable selection now correctly retires without contacting the catalog.
        core.cache.db.execute("DELETE FROM bridge_metadata")
        core.cache.db.execute("UPDATE bridge_artist_artwork SET album=NULL,album_id=NULL")
        core.cache.db.commit()

        def lookup(*_):
            core.remember([original, *([older] if fallback else [])])
            return [updated]

        art.catalog = SimpleNamespace(artist_albums=lookup)
        owner = other if correction == "reassigned" else ARTIST
        art.enrich(core.identity(owner), owner)
    expected = older if fallback else updated if correction == "backdated" else None
    assert art.selection(ARTIST) == expected
    assert art.info(ID)["status"] == ("ready" if expected else "unavailable")
    if correction == "reassigned":
        assert art.selection(other) == updated
    # The corrected result, including fallback/absence, survives bulk eviction
    # and a restart; HTTP selection neither resurrects the old copy nor looks up.
    core.cache.db.execute("DELETE FROM bridge_metadata")
    core.cache.db.commit()
    art.close()
    core.close()
    core = Core(tmp_path, 1024)
    art = service(core)
    try:
        assert art.selection(ARTIST) == expected
        if expected:
            assert art.get(ID) == expected
        assert not art.pending
        if correction == "reassigned":
            assert art.selection(other) == updated
    finally:
        art.close()
        core.close()


@pytest.mark.parametrize("primary", ["1", "99"])
def test_corrected_primary_keeps_exact_lookup_provenance_after_pruning(tmp_path, primary):
    core = Core(tmp_path, 1024)
    core.remember([ARTIST])
    original = ready_album(id="2", artist_id=primary, release_date="2025-01-01")
    art = service(core, [original])
    art.enrich(ID, ARTIST)
    assert art.get(ID) == original
    core.cache.db.execute("DELETE FROM bridge_artist_releases")
    core.cache.db.execute("DELETE FROM bridge_metadata")
    core.cache.db.commit()
    art.close()
    core.close()
    core = Core(tmp_path, 1024)
    art = service(core)
    try:
        updated = replace(original, artist_id="88", release_date="2020-01-01")
        core.remember([updated])
        assert art.get(ID) == updated
        assert core.cache.db.execute(
            "SELECT lookup FROM bridge_artist_artwork WHERE id=?", (ID,)
        ).fetchone() == (1,)
    finally:
        art.close()
        core.close()


@pytest.mark.parametrize("primary,association", [("1", False), ("1", True), ("99", False)])
@pytest.mark.parametrize("upgrade_quality", ["lossless", "atmos"])
def test_provenance_upgrade_does_not_invent_primary_lookup(
    tmp_path, primary, association, upgrade_quality
):
    core = Core(tmp_path, 1024)
    core.remember([ARTIST])
    original = ready_album(id="2", artist_id=primary, release_date="2025-01-01")
    core.cache.db.execute(
        "UPDATE bridge_artist_artwork SET album=? WHERE id=?",
        (json.dumps(asdict(original)), ID),
    )
    if association:
        core.cache.db.execute(
            "INSERT INTO bridge_artist_releases VALUES (?,?)", (ID, core.identity(original))
        )
    core.cache.db.execute("ALTER TABLE bridge_artist_artwork DROP COLUMN lookup")
    core.cache.db.commit()
    core.close()
    upgraded = Core(tmp_path, 1024, quality=upgrade_quality)
    upgraded.close()
    core = Core(tmp_path, 1024)
    art = service(core)
    try:
        expected_lookup = association or primary != "1"
        assert core.cache.db.execute(
            "SELECT lookup FROM bridge_artist_artwork WHERE id=?", (ID,)
        ).fetchone() == (int(expected_lookup),)
        core.cache.db.execute("DELETE FROM bridge_artist_releases")
        core.cache.db.commit()
        updated = replace(original, artist_id="88")
        core.remember([updated])
        assert art.selection(ARTIST) == (updated if expected_lookup else None)
    finally:
        art.close()
        core.close()


@pytest.mark.parametrize("exact", [False, True])
def test_indexed_selection_upgrade_preserves_existing_provenance(tmp_path, exact):
    core = Core(tmp_path, 1024)
    original = ready_album(id="2", release_date="2025-01-01")
    core.remember([ARTIST, original])
    core.cache.db.execute("UPDATE bridge_artist_artwork SET lookup=? WHERE id=?", (int(exact), ID))
    core.cache.db.execute("DROP INDEX bridge_selected_album_idx")
    core.cache.db.execute("ALTER TABLE bridge_artist_artwork DROP COLUMN album_id")
    core.cache.db.commit()
    core.close()
    core = Core(tmp_path, 1024)
    art = service(core)
    try:
        assert core.cache.db.execute(
            "SELECT album_id,lookup FROM bridge_artist_artwork WHERE id=?", (ID,)
        ).fetchone() == (core.identity(original), int(exact))
        updated = replace(original, artist_id="99")
        core.remember([updated])
        assert art.selection(ARTIST) == (updated if exact else None)
    finally:
        art.close()
        core.close()
