"""Pinned CLI adapter. Raw stdout/stderr never leave this module or reach disk."""

import json
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .backend_pins import LITE_ENDPOINT
from .cache import safe_path
from .model import Media, PlayerError


class Summary:
    """Bounded line parser: retain only a single final JSON path, no raw diagnostics."""

    def __init__(self):
        self.buffer = b""
        self.dropping = False
        self.path = None

    def feed(self, chunk):
        for segment in chunk.splitlines(keepends=True):
            if not self.dropping:
                self.buffer += segment
                if len(self.buffer) > 65536:
                    self.buffer = b""
                    self.dropping = True
            if segment.endswith(b"\n"):
                if not self.dropping:
                    try:
                        data = json.loads(self.buffer)
                        if (
                            isinstance(data, list)
                            and len(data) == 1
                            and isinstance(data[0], dict)
                            and isinstance(data[0].get("path"), str)
                            and len(data[0]["path"]) < 4096
                        ):
                            self.path = data[0]["path"]
                    except (ValueError, KeyError, TypeError):
                        pass
                self.buffer = b""
                self.dropping = False


def stop_group(process):
    # start_new_session guarantees this group never contains the independent wrapper.
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        # Children may outlive the leader, so kill the group even if it has exited.
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def probe(path, executable="ffprobe"):
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,profile,sample_rate,bits_per_raw_sample,channels",
                "-of",
                "json",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
        )
        return Media.from_probe(json.loads(result.stdout))
    except PlayerError:
        raise
    except Exception:
        raise PlayerError(
            "Cannot inspect audio. Install ffprobe and check the downloaded format."
        ) from None


def job_config(output, country):
    # Fresh config, not a copy of the user's existing configuration or secrets.
    return {
        "storefront": country,
        "get-m3u8-from-device": True,
        "get-m3u8-mode": "all",
        "decrypt-m3u8-port": "127.0.0.1:10020",
        "get-m3u8-port": "127.0.0.1:20020",
        "exit-on-error": True,
        "alac-max": 192000,
        "atmos-max": 2768,
        "max-memory-limit": 256,
        "limit-max": 200,
        "alac-save-folder": str(output),
        "atmos-save-folder": str(output),
        "aac-save-folder": str(output),
        "mv-save-folder": str(output),
        "album-folder-format": "{AlbumId}",
        "song-file-format": "{SongId}",
        "artist-folder-format": "",
        "embed-lrc": False,
        "save-lrc-file": False,
        "embed-cover": False,
        "save-artist-cover": False,
        "save-animated-artwork": False,
        "convert-after-download": False,
    }


def lite_job_config(output, country):
    # v0.1.4's nested schema, not the July classic flat schema.
    return {
        "general": {
            "lite-server": LITE_ENDPOINT,
            "storefront": country,
            "exit-on-error": True,
            "max-memory-limit": 256,
        },
        "media": {"get-m3u8-mode": "all", "alac-max": 192000, "atmos-max": 2768, "alac-fix": True},
        "paths": {
            "alac": str(output),
            "atmos": str(output),
            "aac": str(output),
            "mv": str(output),
            "album-folder": "{AlbumId}",
            "song-file": "{SongId}",
            "artist-folder": "",
            "limit-max": 200,
        },
        "metadata": {
            "lyrics": {"embed": False, "save-file": False},
            "artwork": {
                "embed": False,
                "save-artist": False,
                "save-animated": False,
                "emby-animated": False,
                "dl-for-playlist": False,
            },
        },
        "convert": {"after-download": False},
    }


class Downloader:
    def __init__(self, binary, staging, stage_limit=2 * 1024**3, timeout=900, backend="classic"):
        if backend not in {"classic", "lite"}:
            raise PlayerError("Unknown downloader backend.")
        self.backend = backend
        self.binary = binary
        self.staging = Path(staging)
        self.stage_limit = stage_limit
        self.timeout = timeout

    def check_stage(self, job):
        size = 0
        for path in job.rglob("*"):
            safe_path(self.staging, path)
            if path.is_file():
                size += path.stat().st_size
        if size > self.stage_limit:
            raise PlayerError("Download exceeded the 2 GiB staging allowance.")
        if shutil.disk_usage(job).free < 128 * 1024**2:
            raise PlayerError("Not enough free disk space; download stopped.")

    def run(self, track, quality, cancel):
        binary = shutil.which(self.binary)
        if not binary:
            raise PlayerError("Downloader not found. Set its absolute executable path in Settings.")
        job = Path(tempfile.mkdtemp(prefix="job-", dir=self.staging))
        output = job / "output"
        output.mkdir()
        config = job / "config.yaml"
        # JSON is valid YAML and avoids a YAML parser dependency.
        make_config = lite_job_config if self.backend == "lite" else job_config
        config.write_text(json.dumps(make_config(output, track.country)))
        config.chmod(0o600)
        process = None
        try:
            if cancel.is_set():
                raise PlayerError("Download cancelled.")
            args = [str(Path(binary).absolute()), "--json"]
            if self.backend == "lite":
                args.extend(["--config", str(config)])
            if quality == "atmos":
                args.append("--atmos")
            args.append(track.url)
            process = subprocess.Popen(
                args,
                cwd=job,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            summary = Summary()
            started = time.monotonic()
            last_check = 0.0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if cancel.is_set():
                        raise PlayerError("Download cancelled.")
                    now = time.monotonic()
                    if now - started > self.timeout:
                        raise PlayerError(
                            "Download timed out. Check that your wrapper is running; retry."
                        )
                    if now - last_check >= 0.25:
                        last_check = now
                        self.check_stage(job)
                    for key, _ in selector.select(timeout=0.1):
                        chunk = os.read(key.fileobj.fileno(), 16384)
                        if chunk:
                            summary.feed(chunk)
                        else:
                            selector.unregister(key.fileobj)
            summary.feed(b"\n")
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise PlayerError("Downloader did not exit after completing output.") from None
            if code != 0 or not summary.path:
                raise PlayerError(
                    "Download failed or returned no media. Check wrapper/dependencies and retry."
                )
            stop_group(process)
            self.check_stage(job)
            source = Path(summary.path)
            if not source.is_absolute():
                source = job / source
            source = safe_path(output, source)
            if not source.is_file() or source.stat().st_size == 0:
                raise PlayerError("Downloader returned a missing or empty media file.")
            media = probe(source)
            if cancel.is_set():
                raise PlayerError("Download cancelled.")
            return job, source, media
        except PlayerError:
            if process:
                stop_group(process)
            shutil.rmtree(job)
            raise
        except Exception:
            if process:
                stop_group(process)
            shutil.rmtree(job)
            raise PlayerError(
                "Download could not complete. Check paths, permissions and free disk space."
            ) from None
        finally:
            if process and process.stdout:
                process.stdout.close()
            config.unlink(missing_ok=True)
