"""Pinned CLI adapter. Raw stdout/stderr never leave this module or reach disk."""

import json
import os
import re
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

# Only fixed messages cross the subprocess boundary. Prefixes are from pinned
# amdl v0.1.4 internal/app/{run,rip,media}.go; never include their error suffixes.
DIAGNOSTICS = {
    "config": "Downloader rejected its private job config. Report this category.",
    "catalog-token": "Catalog access could not be obtained. Check network access to Apple Music.",
    "catalog": "Catalog lookup failed. Check network, storefront and song availability.",
    "catalog-401": "Catalog access was rejected (401). Wrapper login validity is still unknown.",
    "catalog-403": "Catalog access was denied (403). Check network and storefront restrictions.",
    "catalog-404": "Song or album was not found (404). Try its Apple Music URL in your storefront.",
    "catalog-429": "Catalog rate limit reached (429). Wait before retrying.",
    "wrapper": "Downloader could not use the local lite API. Check Backend setup status.",
    "manifest": "No usable requested audio stream was found. Check song availability and quality.",
    "download": "Audio download/decryption failed. Check network and Backend setup status.",
    "finalize": "Downloaded media could not be finalized or tagged. Report this category.",
    "unavailable": "The requested track/format is unavailable. Try another song or quality.",
    "proxy": "Downloader network configuration failed. Report this category.",
}

# The pinned wrapper returns HTTP 200 even for application errors (e.g. a
# missing manifest is {code: 404, ...}). Distinguish those from HTTP/transport
# failures without keeping the upstream message, response body or request URL.
for _endpoint in ("status", "m3u8", "key"):
    for _kind in ("api", "http"):
        for _code in (400, 401, 403, 404, 429, 500, 502, 503, 504):
            DIAGNOSTICS[f"wrapper-{_endpoint}-{_kind}-{_code}"] = (
                f"Local lite /{_endpoint} returned "
                f"{'application code' if _kind == 'api' else 'HTTP status'} {_code}. "
                + (
                    "The service was reached but could not provide this song's manifest. "
                    "Check the song/storefront; this alone does not prove expired login."
                    if _endpoint == "m3u8" and _kind == "api" and _code == 404
                    else "The service was reached; this is not a connection-refused error. "
                    "Report this category and storefront."
                )
            )
    for _kind, _message in {
        "refused": "Connection was refused. Check whether the managed service is still running.",
        "timeout": "The request timed out. Check Backend setup status and retry.",
        "invalid-response": "Response does not match the JSON contract. Report this category.",
        "unknown": "The request failed; its private details were discarded. Report this category.",
    }.items():
        DIAGNOSTICS[f"wrapper-{_endpoint}-{_kind}"] = f"Local lite /{_endpoint}: {_message}"
DIAGNOSTICS["wrapper-m3u8-empty"] = (
    "The local lite API returned an empty manifest. Check the song/storefront. "
    "This alone does not prove expired login."
)


def wrapper_category(detail, endpoint):
    match = re.match(
        rb"lite-server /(status|m3u8|key) returned (code=)?"
        rb"(400|401|403|404|429|500|502|503|504)(?:\s|:|$)",
        detail,
    )
    if match:
        endpoint = match[1].decode("ascii")
        kind = "api" if match[2] else "http"
        return f"wrapper-{endpoint}-{kind}-{match[3].decode('ascii')}"
    # Only recognize transport errors for the configured loopback endpoint.
    transport = re.match(
        rb'Get "http://127\.0\.0\.1:12340/(status|m3u8|key)(?:\?[^"\r\n]*)?": (.*)',
        detail,
    )
    if transport:
        endpoint = transport[1].decode("ascii")
        if transport[2] == b"dial tcp 127.0.0.1:12340: connect: connection refused":
            return f"wrapper-{endpoint}-refused"
        if any(
            value in transport[2]
            for value in (b"context deadline exceeded", b"i/o timeout", b"Client.Timeout")
        ):
            return f"wrapper-{endpoint}-timeout"
    if detail.startswith(
        (b"invalid character ", b"json: cannot unmarshal", b"unexpected end of JSON")
    ):
        return f"wrapper-{endpoint}-invalid-response"
    return f"wrapper-{endpoint}-unknown"


ERROR_PREFIXES = (
    (b"load Config failed:", "config"),
    (b"proxy config error:", "proxy"),
    (b"Failed to get token.", "catalog-token"),
    (b"Failed to get song response.", "catalog"),
    (b"Failed to get album response.", "catalog"),
    (b"Failed to rip song:", "catalog"),
    (b"Failed to rip album:", "catalog"),
    ("\u26a0 Failed to extract info from manifest:".encode(), "manifest"),
    (b"Failed to run v4:", "download"),
    (b"Failed to dl aac-lc via lite-server:", "download"),
    (b"Defragment failed:", "finalize"),
    ("\u26a0 Failed to fix ALAC:".encode(), "finalize"),
    ("\u26a0 Failed to write tags in media:".encode(), "finalize"),
)


class Summary:
    """Bounded parser: retain a result path and fixed categories, never raw logs."""

    def __init__(self, accept_paths=True):
        self.accept_paths = accept_paths
        self.buffer = b""
        self.dropping = False
        self.path = None
        self.error = None
        self.warning = None

    def diagnose(self, line):
        # run.go prints the queue preamble without a newline before ripSong.
        line = re.sub(rb"^Queue [0-9]+ of [0-9]+: Song->", b"", line.strip())
        for prefix, category in ERROR_PREFIXES:
            if line.startswith(prefix):
                if category == "catalog":
                    match = re.fullmatch(
                        rb"Failed to rip (?:song|album): (401 Unauthorized|403 Forbidden|"
                        rb"404 Not Found|429 Too Many Requests)",
                        line,
                    )
                    if match:
                        category += "-" + match[1][:3].decode("ascii")
                # Preserve the specific HTTP category over a later general error.
                if not self.error or self.error == "catalog":
                    self.error = category
                return
        if line == b"Unavailable":
            self.error = self.error or "unavailable"
        elif line == b"Received an empty response":
            self.warning = "wrapper-m3u8-empty"
        else:
            for prefix, endpoint in (
                (b"Warning: failed to query lite-server /status: ", "status"),
                (b"Error connecting to lite-server: ", "m3u8"),
            ):
                if line.startswith(prefix):
                    # Recoverable warnings: never reject a valid completed file.
                    self.warning = wrapper_category(line[len(prefix) :], endpoint)

    def feed(self, chunk):
        for segment in chunk.splitlines(keepends=True):
            if not self.dropping:
                self.buffer += segment
                if len(self.buffer) > 65536:
                    self.buffer = b""
                    self.dropping = True
            if segment.endswith(b"\n"):
                if not self.dropping:
                    self.diagnose(self.buffer)
                    try:
                        data = json.loads(self.buffer)
                        if (
                            self.accept_paths
                            and isinstance(data, list)
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


def lite_job_config(output, country):
    # Pinned v0.1.4 nested schema.
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
    def __init__(self, binary, staging, stage_limit=2 * 1024**3, timeout=900, backend="lite"):
        if backend != "lite":
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
            raise PlayerError(
                "Downloader not found. Check the managed installation in Backend setup."
            )
        job = Path(tempfile.mkdtemp(prefix="job-", dir=self.staging))
        output = job / "output"
        output.mkdir()
        config = job / "config.yaml"
        # JSON is valid YAML and avoids a YAML parser dependency.
        config.write_text(json.dumps(lite_job_config(output, track.country)))
        config.chmod(0o600)
        process = None
        try:
            if cancel.is_set():
                raise PlayerError("Download cancelled.")
            args = [str(Path(binary).absolute()), "--json"]
            args.extend(["--config", str(config)])
            if quality == "atmos":
                args.append("--atmos")
            args.append(track.url)
            process = subprocess.Popen(
                args,
                cwd=job,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            summary = Summary()
            diagnostics = Summary(accept_paths=False)
            started = time.monotonic()
            last_check = 0.0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, summary)
                selector.register(process.stderr, selectors.EVENT_READ, diagnostics)
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
                            key.data.feed(chunk)
                        else:
                            selector.unregister(key.fileobj)
            summary.feed(b"\n")
            diagnostics.feed(b"\n")
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise PlayerError("Downloader did not exit after completing output.") from None
            if code != 0 or not summary.path:
                category = (
                    summary.error or diagnostics.error or summary.warning or diagnostics.warning
                )
                if category:
                    raise PlayerError(f"Download failed [{category}]. {DIAGNOSTICS[category]}")
                raise PlayerError(
                    f"Download failed [no-result; exit {code}]. No valid media summary. "
                    "Report this category, requested quality and storefront; do not share raw logs."
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
            if process and process.stderr:
                process.stderr.close()
            config.unlink(missing_ok=True)
