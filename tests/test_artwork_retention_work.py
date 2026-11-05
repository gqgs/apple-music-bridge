"""Saturated-cache bounds exclude setup and metadata writes, as does the reproduction."""

import json
import time
from dataclasses import asdict, replace

import pytest
from test_bridge import ARTIST, TRACK
from test_release_artwork import ready_album

from apple_music_player.bridge import artwork_retention
from apple_music_player.bridge.core import Core


@pytest.mark.parametrize(
    "scenario", ["single_album", "artists50", "albums200", "lookup200", "song"]
)
def test_saturated_retention_work_is_proportional_to_affected_rows(tmp_path, monkeypatch, scenario):
    core = Core(tmp_path, 1024)
    db = core.cache.db
    artists = [replace(ARTIST, id=str(i)) for i in range(1, 5001)]
    albums = [ready_album(id=str(10000 + i), artist_id=str(i)) for i in range(1, 5001)]
    db.executemany(
        "INSERT INTO bridge_artist_artwork(id,data,album,lookup,album_id) VALUES (?,?,?,0,?)",
        [
            (core.identity(a), json.dumps(asdict(a)), json.dumps(asdict(b)), core.identity(b))
            for a, b in zip(artists, albums, strict=True)
        ],
    )
    db.executemany(
        "INSERT INTO bridge_metadata VALUES (?,?)",
        [(core.identity(a), json.dumps(asdict(a))) for a in albums],
    )
    if scenario == "single_album":
        incoming = [replace(albums[0], release_date="2025-01-01")]
    elif scenario == "artists50":
        incoming = artists[:50]
    elif scenario in {"albums200", "lookup200"}:
        incoming = [
            ready_album(
                id=str(20000 + i),
                artist_id=str(1 if scenario == "lookup200" else i % 40 + 1),
                release_date="2025-01-01",
            )
            for i in range(200)
        ]
    else:
        incoming = [TRACK]
    # Incoming metadata is written before retain() in all real writers.
    db.executemany(
        "INSERT OR REPLACE INTO bridge_metadata VALUES (?,?)",
        [(core.identity(a), json.dumps(asdict(a))) for a in incoming],
    )
    db.execute(
        "DELETE FROM bridge_metadata WHERE rowid IN "
        "(SELECT rowid FROM bridge_metadata ORDER BY rowid DESC LIMIT -1 OFFSET 5000)"
    )
    if scenario == "lookup200":
        db.executemany(
            "INSERT INTO bridge_artist_releases VALUES (?,?)",
            [(core.identity(artists[0]), core.identity(a)) for a in incoming],
        )
    db.commit()
    assert db.execute("SELECT count(*) FROM bridge_metadata").fetchone() == (5000,)
    assert db.execute("SELECT count(*) FROM bridge_artist_artwork").fetchone() == (5000,)
    decoded = 0
    instructions = 0
    statements = []
    original_loads = json.loads

    def counted_loads(*args, **kwargs):
        nonlocal decoded
        decoded += 1
        return original_loads(*args, **kwargs)

    def progressed():
        nonlocal instructions
        instructions += 100
        return 0

    monkeypatch.setattr(artwork_retention.json, "loads", counted_loads)
    db.set_progress_handler(progressed, 100)
    db.set_trace_callback(statements.append)
    try:
        start = time.perf_counter()
        artwork_retention.retain(db, incoming, core.identity)
        elapsed = time.perf_counter() - start
    finally:
        db.set_progress_handler(None, 0)
        db.set_trace_callback(None)
    print(
        f"{scenario}: {elapsed * 1000:.2f}ms, {decoded} JSON decodes, "
        f"~{instructions} VM instructions, {len(statements)} statements"
    )
    try:
        # The previous correction decoded all 5000 selected payloads and scanned
        # all 5000 metadata rows once per affected artist. Neither is allowed.
        assert decoded <= 150
        assert instructions <= 150000
        assert len(statements) <= 130
        if scenario == "song":
            assert decoded == instructions == len(statements) == 0
        else:
            selected = db.execute(
                "SELECT album FROM bridge_artist_artwork WHERE id=?", (core.identity(artists[0]),)
            ).fetchone()[0]
            assert selected is not None
            assert json.loads(selected)["artist_id"] == "1"
        assert db.execute("SELECT count(*) FROM bridge_artist_artwork").fetchone() == (5000,)
    finally:
        core.close()
