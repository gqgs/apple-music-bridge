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


def retain(db, items, identity):
    """Call inside the metadata writer's transaction, before pruning metadata."""
    items = list(items)
    existing_albums = []
    for item in items:
        if isinstance(item, Artist):
            key = identity(item)
            db.execute(
                "INSERT OR REPLACE INTO bridge_artist_artwork(id,data,album) "
                "VALUES (?,?,(SELECT album FROM bridge_artist_artwork WHERE id=?))",
                (key, json.dumps(asdict(item)), key),
            )
            rows = db.execute(
                "SELECT data FROM bridge_metadata WHERE "
                "json_extract(data,'$.artist_id')=? OR id IN "
                "(SELECT album FROM bridge_artist_releases WHERE artist=?)",
                (item.id, key),
            ).fetchall()
            existing_albums.extend(
                album for (raw,) in rows if (album := saved_album(raw, key, identity)) is not None
            )
    db.execute(
        "DELETE FROM bridge_artist_artwork WHERE rowid NOT IN "
        "(SELECT rowid FROM bridge_artist_artwork ORDER BY rowid DESC LIMIT ?)",
        (ARTIST_LIMIT,),
    )
    for item in [*existing_albums, *items]:
        if not isinstance(item, Album) or rank(item) is None:
            continue
        album_key = identity(item)
        artist_key = album_key.rsplit(":album:", 1)[0] + ":artist:" + item.artist_id
        rows = db.execute(
            "SELECT id,album FROM bridge_artist_artwork WHERE id=? OR id IN "
            "(SELECT artist FROM bridge_artist_releases WHERE album=?)",
            (artist_key, album_key),
        ).fetchall()
        for key, old in rows:
            # Associations are exact qualified IDs, never names. Guard profiles
            # even if old databases contain a cross-storefront association.
            if key.rsplit(":artist:", 1)[0] != album_key.rsplit(":album:", 1)[0]:
                continue
            previous = saved_album(old, key, identity) if old else None
            old_rank = rank(previous) if previous else None
            if old_rank is None or rank(item) >= old_rank:
                db.execute(
                    "UPDATE bridge_artist_artwork SET album=? WHERE id=?",
                    (json.dumps(asdict(item)), key),
                )
