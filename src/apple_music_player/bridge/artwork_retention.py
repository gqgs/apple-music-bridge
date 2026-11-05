"""Bounded artist identities and newest-known covers, independent of discography eviction.

Rows are refreshed when artists are browsed; album updates do not displace other
artists. This is a known-release cache, not a promise of catalog freshness.
"""

import json
from dataclasses import asdict
from datetime import date

from ..artwork import BADGE_URL, allowed_url
from ..model import Album, Artist

ARTIST_LIMIT = 5000


def saved_album(raw, artist_key, identity):
    """A damaged retained payload is repairable, never evidence of absence."""
    try:
        album = Album(**json.loads(raw))
        key = identity(album)
        if key.rsplit(":album:", 1)[0] != artist_key.rsplit(":artist:", 1)[0]:
            return None
        if not album.id.isdecimal() or not 1 <= len(album.id) <= 30:
            return None
        rank(album)
        return album
    except (ValueError, TypeError, AttributeError):
        return None


def rank(album):
    if not allowed_url(album.artwork_url) or album.artwork_url == BADGE_URL:
        return None
    try:
        released = date.fromisoformat(album.release_date[:10]) if album.release_date else date.min
    except ValueError:
        return None
    return (released, album.id) if released <= date.today() else None


def migrate_lookup_provenance(db):
    """Upgrade compact provenance and indexed selection keys in one transaction."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(bridge_artist_artwork)")}
    if "lookup" not in columns:
        db.execute("ALTER TABLE bridge_artist_artwork ADD COLUMN lookup INTEGER NOT NULL DEFAULT 0")
    if "album_id" not in columns:
        db.execute("ALTER TABLE bridge_artist_artwork ADD COLUMN album_id TEXT")
    if not {"lookup", "album_id"} <= columns:
        for key, raw in db.execute("SELECT id,album FROM bridge_artist_artwork").fetchall():
            profile_quality = key.split(":")[2]

            def identity(item, quality=profile_quality):
                return f"am:{item.country}:{quality}:album:{item.id}"

            album = saved_album(raw, key, identity) if raw else None
            if album:
                album_key = identity(album)
                if "lookup" not in columns and (
                    album.artist_id != key.rsplit(":", 1)[1] or lookup(db, key, album_key)
                ):
                    # Older retained collaborators required an exact lookup.
                    db.execute("UPDATE bridge_artist_artwork SET lookup=1 WHERE id=?", (key,))
                db.execute(
                    "UPDATE bridge_artist_artwork SET album_id=? WHERE id=?", (album_key, key)
                )
    db.execute(
        "CREATE INDEX IF NOT EXISTS bridge_selected_album_idx ON bridge_artist_artwork(album_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS bridge_release_album_idx "
        "ON bridge_artist_releases(album,artist)"
    )


def lookup(db, artist_key, album_key):
    return bool(
        db.execute(
            "SELECT 1 FROM bridge_artist_releases WHERE artist=? AND album=?",
            (artist_key, album_key),
        ).fetchone()
    )


def batches(db, sql, keys):
    """Bound SQL parameters even for a full migration/backfill."""
    keys = list(keys)
    for start in range(0, len(keys), 400):
        batch = keys[start : start + 400]
        yield from db.execute(sql.format(",".join("?" for _ in batch)), batch)


def retain(db, items, identity):
    """Read only affected owners and indexed candidates; replace before ranking."""
    artists, updates = {}, {}
    for item in items:
        if isinstance(item, Artist):
            artists[identity(item)] = item
        elif isinstance(item, Album):
            updates[identity(item)] = item
    if not artists and not updates:
        return
    for key, artist in artists.items():
        db.execute(
            "INSERT OR REPLACE INTO bridge_artist_artwork(id,data,album,lookup,album_id) "
            "VALUES (?,?,(SELECT album FROM bridge_artist_artwork WHERE id=?),"
            "COALESCE((SELECT lookup FROM bridge_artist_artwork WHERE id=?),0),"
            "(SELECT album_id FROM bridge_artist_artwork WHERE id=?))",
            (key, json.dumps(asdict(artist)), key, key, key),
        )
    if artists:
        db.execute(
            "DELETE FROM bridge_artist_artwork WHERE rowid IN "
            "(SELECT rowid FROM bridge_artist_artwork ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
            (ARTIST_LIMIT,),
        )
    affected = set(artists)
    affected.update(
        key.rsplit(":album:", 1)[0] + ":artist:" + album.artist_id for key, album in updates.items()
    )
    affected.update(
        row[0]
        for row in batches(
            db, "SELECT id FROM bridge_artist_artwork WHERE album_id IN ({})", updates
        )
    )
    affected.update(
        row[0]
        for row in batches(
            db, "SELECT artist FROM bridge_artist_releases WHERE album IN ({})", updates
        )
    )
    candidates = {}
    for key, raw, exact, album_key in batches(
        db, "SELECT id,album,lookup,album_id FROM bridge_artist_artwork WHERE id IN ({})", affected
    ):
        album = saved_album(raw, key, identity) if raw else None
        candidates[key] = (
            {album_key: (album, bool(exact))} if album and identity(album) == album_key else {}
        )
    if not candidates:
        return
    pairs = set(
        batches(
            db, "SELECT artist,album FROM bridge_artist_releases WHERE artist IN ({})", candidates
        )
    )
    associated = {}
    for owner, album_key in pairs:
        associated.setdefault(album_key, set()).add(owner)
    # Primary-artist expression index and album primary key avoid the old
    # per-owner LEFT JOIN/OR full metadata scan. Decode each candidate once.
    rows = dict(
        batches(
            db,
            "SELECT id,data FROM bridge_metadata WHERE json_extract(data, '$.artist_id') IN ({})",
            {key.rsplit(":", 1)[1] for key in candidates},
        )
    )
    rows.update(batches(db, "SELECT id,data FROM bridge_metadata WHERE id IN ({})", associated))
    for album_key, raw in rows.items():
        if ":album:" not in album_key or album_key in updates:
            continue
        prefix = album_key.rsplit(":album:", 1)[0]
        album = saved_album(raw, prefix + ":artist:0", identity)
        if not album or identity(album) != album_key:
            continue
        owners = associated.get(album_key, set()) | {prefix + ":artist:" + album.artist_id}
        for owner in owners & candidates.keys():
            if owner.rsplit(":artist:", 1)[0] != prefix:
                continue
            previous = candidates[owner].get(album_key)
            exact = (owner, album_key) in pairs or bool(previous and previous[1])
            candidates[owner][album_key] = (album, exact)
    # Incoming metadata is authoritative even when invalid or reassigned.
    # Include previous selected owners, preserving only proven lookup access.
    selected_owners = {}
    for owner, values in candidates.items():
        for album_key in values.keys() & updates.keys():
            selected_owners.setdefault(album_key, set()).add(owner)
    for album_key, album in updates.items():
        prefix = album_key.rsplit(":album:", 1)[0]
        primary = prefix + ":artist:" + album.artist_id
        owners = (
            associated.get(album_key, set()) | selected_owners.get(album_key, set()) | {primary}
        )
        for owner in owners & candidates.keys():
            if owner.rsplit(":artist:", 1)[0] != prefix:
                continue
            previous = candidates[owner].pop(album_key, None)
            exact = (owner, album_key) in pairs or bool(previous and previous[1])
            if owner == primary or exact:
                candidates[owner][album_key] = (album, exact)
    for key, values in candidates.items():
        eligible = [(rank(a), a, exact) for a, exact in values.values() if rank(a) is not None]
        selected = max(eligible, key=lambda value: value[0]) if eligible else None
        db.execute(
            "UPDATE bridge_artist_artwork SET album=?,lookup=?,album_id=? WHERE id=?",
            (
                json.dumps(asdict(selected[1])) if selected else None,
                int(selected[2]) if selected else 0,
                identity(selected[1]) if selected else None,
                key,
            ),
        )
