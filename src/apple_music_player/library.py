"""Bounded on-device album history, independent of the disposable media cache."""

import json
import re
import sqlite3
import time
from dataclasses import asdict

from .cache import safe_path
from .model import Album
from .runtime_install import private_dir


class History:
    def __init__(self, root, limit=200):
        self.root = private_dir(root)
        path = safe_path(self.root, self.root / "history.sqlite3")
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS albums "
            "(country TEXT, id TEXT, played REAL, metadata TEXT, PRIMARY KEY(country,id))"
        )
        self.limit = limit
        self.revision = 0

    def record(self, track):
        album = track.album
        if not album or not re.fullmatch(r"[0-9]+", album.id):
            return
        self.db.execute(
            "INSERT OR REPLACE INTO albums VALUES (?,?,?,?)",
            (album.country, album.id, time.time(), json.dumps(asdict(album))),
        )
        self.db.execute(
            "DELETE FROM albums WHERE rowid NOT IN "
            "(SELECT rowid FROM albums ORDER BY played DESC LIMIT ?)",
            (self.limit,),
        )
        self.db.commit()
        self.revision += 1

    def albums(self, country):
        result = []
        for (raw,) in self.db.execute(
            "SELECT metadata FROM albums WHERE country=? ORDER BY played DESC LIMIT ?",
            (country, self.limit),
        ):
            try:
                result.append(Album(**json.loads(raw)))
            except (ValueError, TypeError):
                continue
        return result

    def artists(self, country):
        return list(
            dict.fromkeys(
                a.artist_id for a in self.albums(country) if re.fullmatch(r"[0-9]+", a.artist_id)
            )
        )[:5]

    def clear(self):
        self.db.execute("DELETE FROM albums")
        self.db.commit()
        self.revision += 1

    def close(self):
        self.db.close()
