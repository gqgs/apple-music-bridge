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
    recorded: bool = False

    @property
    def key(self):
        return self.track.key(self.quality)


class Controller:
    def __init__(self, cache, player, downloader="apple-music-downloader", device="auto"):
        self.cache, self.player = cache, player
        self.downloader, self.device = downloader, device
        self.backend = "lite"
        self.runtime = None
        self.queue = []
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.job = None
        self.cancel = None
        self.current = None
        self.media = None
        self.error = ""
        self.stopped = False
        self.on_played = None

    def add(self, track, quality):
        if self.reject_storefront(track):
            return
        key = track.key(quality)
        if not any(entry.key == key for entry in self.queue):
            self.queue.append(Entry(track, quality))
        self.protect()

    def reject_storefront(self, track):
        regions = getattr(self.runtime, "regions", ()) if self.backend == "lite" else ()
        if len(regions) == 1 and track.country != regions[0]:
            self.error = (
                f"Track is {track.country.upper()}; managed backend reports {regions[0].upper()}. "
                "Use the backend storefront, search again, and replace this queue entry. "
                "Song IDs will not be remapped automatically."
            )
            return True
        return False

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
        if self.future and self.backend == "lite" and (not self.runtime or not self.runtime.ready):
            self.cancel.set()
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
                    if (
                        not self.current.recorded
                        and self.current.state == "Playing"
                        and (self.player.properties.get("time-pos") or 0) > 0
                    ):
                        self.current.recorded = True
                        if self.on_played:
                            try:
                                self.on_played(self.current.track)
                            except Exception:
                                self.error = "Local listening history could not be saved."
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
                    if self.reject_storefront(entry.track):
                        entry.state = "Failed — storefront mismatch"
                        return
                    if self.cache.get(entry.key):
                        entry.state = "Ready"
                        continue
                    binary = self.downloader
                    if self.backend == "lite":
                        if not self.runtime or not self.runtime.ready:
                            if (
                                self.runtime
                                and not self.runtime.cancel.is_set()
                                and (
                                    self.runtime.starting
                                    or (self.runtime.mode == "service" and self.runtime.process)
                                )
                            ):
                                return  # Leave queued while saved service startup is in progress.
                            entry.state = "Failed"
                            self.error = (
                                "Managed lite is not ready. Open Backend setup, then Retry."
                            )
                            return
                        binary = str(self.runtime.store.downloader)
                    self.job = entry
                    self.cancel = threading.Event()
                    entry.state = "Downloading"
                    self.future = self.executor.submit(
                        Downloader(binary, self.cache.staging, backend=self.backend).run,
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
