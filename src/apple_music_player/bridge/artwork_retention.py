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
    """Upgrade compact records without inventing primary-artist lookup evidence."""
    if "lookup" in {row[1] for row in db.execute("PRAGMA table_info(bridge_artist_artwork)")}:
        return
    db.execute("ALTER TABLE bridge_artist_artwork ADD COLUMN lookup INTEGER NOT NULL DEFAULT 0")
    for key, raw in db.execute("SELECT id,album FROM bridge_artist_artwork").fetchall():
        # A shared cache can contain another quality profile at upgrade time.
        profile_quality = key.split(":")[2]

        def identity(item, quality=profile_quality):
            return f"am:{item.country}:{quality}:album:{item.id}"

        album = saved_album(raw, key, identity) if raw else None
        if album and (album.artist_id != key.rsplit(":", 1)[1] or lookup(db, key, identity(album))):
            # Older retained collaborators could only have come from exact lookups.
            db.execute("UPDATE bridge_artist_artwork SET lookup=1 WHERE id=?", (key,))


def lookup(db, artist_key, album_key):
    return bool(
        db.execute(
            "SELECT 1 FROM bridge_artist_releases WHERE artist=? AND album=?",
            (artist_key, album_key),
        ).fetchone()
    )


def retain(db, items, identity):
    """Replace corrected identities first, then rank remaining eligible candidates."""
    items = list(items)
    affected = set()
    for item in items:
        if isinstance(item, Artist):
            key = identity(item)
            affected.add(key)
            db.execute(
                "INSERT OR REPLACE INTO bridge_artist_artwork(id,data,album,lookup) "
                "VALUES (?,?,(SELECT album FROM bridge_artist_artwork WHERE id=?),"
                "COALESCE((SELECT lookup FROM bridge_artist_artwork WHERE id=?),0))",
                (key, json.dumps(asdict(item)), key, key),
            )
    db.execute(
        "DELETE FROM bridge_artist_artwork WHERE rowid NOT IN "
        "(SELECT rowid FROM bridge_artist_artwork ORDER BY rowid DESC LIMIT ?)",
        (ARTIST_LIMIT,),
    )
    updates = {identity(item): item for item in items if isinstance(item, Album)}
    retained = {}
    for key, raw, exact in db.execute("SELECT id,album,lookup FROM bridge_artist_artwork"):
        album = saved_album(raw, key, identity) if raw else None
        if album and identity(album) in updates:
            affected.add(key)
            album = updates[identity(album)]
            exact = exact or lookup(db, key, identity(album))
            if album.artist_id != key.rsplit(":", 1)[1] and not exact:
                album = None
        retained[key] = (album, bool(exact))
    for album_key, album in updates.items():
        affected.add(album_key.rsplit(":album:", 1)[0] + ":artist:" + album.artist_id)
        affected.update(
            row[0]
            for row in db.execute(
                "SELECT artist FROM bridge_artist_releases WHERE album=?", (album_key,)
            )
        )
    for key in affected & retained.keys():
        prefix, artist_id = key.rsplit(":artist:", 1)
        album, exact = retained[key]
        candidates = {identity(album): (album, exact)} if album else {}
        rows = db.execute(
            "SELECT m.id,m.data,r.album FROM bridge_metadata m "
            "LEFT JOIN bridge_artist_releases r ON r.album=m.id AND r.artist=? "
            "WHERE m.id LIKE ? AND (json_extract(m.data,'$.artist_id')=? "
            "OR r.album IS NOT NULL)",
            (key, prefix + ":album:%", artist_id),
        ).fetchall()
        for album_key, raw, associated in rows:
            current = saved_album(raw, key, identity)
            if current and identity(current) == album_key:
                previous = candidates.get(album_key)
                candidates[album_key] = (
                    current,
                    bool(associated) or bool(previous and previous[1]),
                )
        # Incoming metadata is authoritative even for invalid artwork/date or a
        # changed primary artist. Never resurrect an older copy of the same ID.
        for album_key, current in updates.items():
            if album_key.rsplit(":album:", 1)[0] != prefix:
                continue
            previous = candidates.get(album_key)
            exact = lookup(db, key, album_key) or bool(previous and previous[1])
            candidates.pop(album_key, None)
            if current.artist_id == artist_id or exact:
                candidates[album_key] = (current, exact)
        eligible = [(rank(a), a, exact) for a, exact in candidates.values() if rank(a) is not None]
        selected = max(eligible, key=lambda value: value[0]) if eligible else None
        db.execute(
            "UPDATE bridge_artist_artwork SET album=?,lookup=? WHERE id=?",
            (
                json.dumps(asdict(selected[1])) if selected else None,
                int(selected[2]) if selected else 0,
                key,
            ),
        )
