import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .downloader import Downloader
from .model import PlayerError, Track


@dataclass
class Entry:
    track: Track
    quality: str
    state: str = "Queued"

    @property
    def key(self):
        return self.track.key(self.quality)


class Controller:
    def __init__(self, cache, player, downloader="apple-music-downloader", device="auto"):
        self.cache, self.player = cache, player
        self.downloader, self.device = downloader, device
        self.queue = []
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.job = None
        self.cancel = None
        self.current = None
        self.media = None
        self.error = ""
        self.stopped = False

    def add(self, track, quality):
        key = track.key(quality)
        if not any(entry.key == key for entry in self.queue):
            self.queue.append(Entry(track, quality))
        self.protect()

    def protect(self):
        self.cache.protected = {entry.key for entry in self.queue}

    def remove(self, index):
        if not 0 <= index < len(self.queue):
            return
        entry = self.queue.pop(index)
        if self.job is entry:
            self.cancel.set()
        if self.current is entry:
            self.player.close()
            self.current = self.media = None
        self.protect()
        try:
            self.cache.evict()
        except PlayerError as exc:
            self.error = str(exc)

    def next(self):
        self.remove(0)
        self.error = ""
        self.stopped = False

    def retry(self):
        self.error = ""
        self.stopped = False
        for entry in self.queue:
            if entry.state.startswith(("Failed", "Cancelled")):
                entry.state = "Queued"

    def cancel_download(self):
        if self.cancel and self.job:
            self.cancel.set()
            self.job.state = "Cancelled — Retry to resume"

    def tick(self):
        if self.future and self.future.done():
            entry = self.job
            jobdir = None
            try:
                jobdir, source, media = self.future.result()
                if not self.cancel.is_set() and any(e is entry for e in self.queue):
                    self.cache.put(entry.key, source, media)
                    entry.state = "Ready"
            except PlayerError as exc:
                if any(e is entry for e in self.queue):
                    entry.state = (
                        "Cancelled — Retry to resume" if self.cancel.is_set() else "Failed"
                    )
                    self.error = str(exc)
            except Exception:
                entry.state = "Failed"
                self.error = (
                    "Could not store downloaded audio. Check cache permissions and free space."
                )
            finally:
                if jobdir:
                    shutil.rmtree(jobdir)
                self.future = self.job = self.cancel = None
        if self.current:
            try:
                if "eof" in self.player.poll():
                    self.next()
                elif self.player.output_verified:
                    self.current.state = (
                        "Paused" if self.player.properties.get("pause") else "Playing"
                    )
                else:
                    self.current.state = "Waiting for audio output"
            except PlayerError as exc:
                self.error = str(exc)
                self.player.close()
                self.current.state = "Failed"
                self.current = self.media = None
                self.stopped = True
        if not self.queue or self.stopped:
            return
        head = self.queue[0]
        if not self.current and head.state in {"Queued", "Ready"}:
            hit = self.cache.get(head.key)
            if hit:
                try:
                    self.player.start(hit[0], hit[1], self.device)
                    self.cache.touch(head.key)
                    self.current, self.media = head, hit[1]
                    head.state = "Starting"
                except PlayerError as exc:
                    self.error = str(exc)
                    head.state = "Failed"
                    self.stopped = True
                    return
        # Only current track plus one successor; never download the whole queue eagerly.
        if not self.future:
            candidates = self.queue[:2] if self.current else self.queue[:1]
            for entry in candidates:
                if entry.state == "Queued":
                    if self.cache.get(entry.key):
                        entry.state = "Ready"
                        continue
                    self.job = entry
                    self.cancel = threading.Event()
                    entry.state = "Downloading"
                    self.future = self.executor.submit(
                        Downloader(self.downloader, self.cache.staging).run,
                        entry.track,
                        entry.quality,
                        self.cancel,
                    )
                    break

    def close(self):
        if self.cancel:
            self.cancel.set()
        self.player.close()
        self.executor.shutdown(wait=True, cancel_futures=True)
        if self.future and not self.future.cancelled():
            try:
                jobdir, _, _ = self.future.result()
                shutil.rmtree(jobdir)
            except Exception:
                pass
        self.cache.close()
