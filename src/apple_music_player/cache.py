import fcntl
import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .model import Media, PlayerError

KEY = re.compile(r"[0-9a-f]{64}")


def safe_path(root, path):
    """Require a regular owned descendant, refusing every symlink component."""
    root, path = Path(root).absolute(), Path(path).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise PlayerError("Rejected a file outside the private cache.") from None
    current = root
    for part in ("", *parts):
        current = current / part
        if current.is_symlink() or part in {"..", "."}:
            raise PlayerError("Rejected a symlink or unsafe cache path.")
    if path.resolve() != path or not parts:
        raise PlayerError("Rejected an unsafe cache path.")
    return path


class Cache:
    def __init__(self, root, limit):
        self.root = Path(root).absolute()
        # Do not follow symlinked parents into an unrelated downloads directory.
        if self.root.resolve() != self.root:
            raise PlayerError("Cache directory and its parents must not be symlinks.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for name in ("lock", "index.sqlite3", "media", "staging"):
            safe_path(self.root, self.root / name)
        self.lock = open(self.root / "lock", "a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise PlayerError("Another player is using this cache. Close it first.") from None
        self.media = self.root / "media"
        self.staging = self.root / "staging"
        self.media.mkdir(exist_ok=True, mode=0o700)
        self.staging.mkdir(exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "index.sqlite3")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS media "
            "(key TEXT PRIMARY KEY, size INTEGER, used REAL, info TEXT)"
        )
        self.limit = limit
        self.protected = set()
        self.reconcile()

    def path(self, key):
        if not KEY.fullmatch(key):
            raise PlayerError("Invalid cache key.")
        return safe_path(self.root, self.media / key)

    def reconcile(self):
        for entry in self.staging.iterdir():
            # Only our random temporary job directories; never follow symlinks.
            if entry.name.startswith("job-"):
                safe_path(self.root, entry)
                if entry.is_dir():
                    shutil.rmtree(entry)
        known = set()
        for key, size in self.db.execute("SELECT key,size FROM media").fetchall():
            path = self.path(key)
            if not path.is_file() or path.stat().st_size != size:
                self.db.execute("DELETE FROM media WHERE key=?", (key,))
            else:
                known.add(key)
        for path in self.media.iterdir():
            if KEY.fullmatch(path.name) and path.name not in known:
                self.path(path.name).unlink(missing_ok=True)
        self.db.commit()
        self.evict()

    @property
    def usage(self):
        return self.db.execute("SELECT COALESCE(SUM(size),0) FROM media").fetchone()[0]

    def get(self, key):
        row = self.db.execute("SELECT info,size FROM media WHERE key=?", (key,)).fetchone()
        if row:
            path = self.path(key)
            if path.is_file() and path.stat().st_size == row[1]:
                return path, Media(**json.loads(row[0]))
            self.db.execute("DELETE FROM media WHERE key=?", (key,))
            self.db.commit()
        return None

    def touch(self, key):
        self.db.execute("UPDATE media SET used=? WHERE key=?", (time.time(), key))
        self.db.commit()

    def evict(self, incoming=0):
        if incoming > self.limit:
            raise PlayerError("Track exceeds the cache limit. Increase the limit and retry.")
        for (key,) in self.db.execute("SELECT key FROM media ORDER BY used ASC").fetchall():
            if self.usage + incoming <= self.limit:
                break
            if key not in self.protected:
                self.path(key).unlink(missing_ok=True)
                self.db.execute("DELETE FROM media WHERE key=?", (key,))
                self.db.commit()
        if self.usage + incoming > self.limit:
            raise PlayerError("Cache is full of queued tracks. Remove some or increase its limit.")

    def put(self, key, source, media):
        source = safe_path(self.staging, source)
        if not source.is_file() or source.stat().st_nlink != 1:
            raise PlayerError("Downloaded media must be a private regular file.")
        size = source.stat().st_size
        self.evict(size)
        os.replace(source, self.path(key))
        self.db.execute(
            "INSERT OR REPLACE INTO media VALUES (?,?,?,?)",
            (key, size, time.time(), json.dumps(asdict(media))),
        )
        self.db.commit()

    def close(self):
        self.db.close()
        self.lock.close()
