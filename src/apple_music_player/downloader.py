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
    "wrapper": "Downloader could not use the local lite API. Check apple-music-bridge status.",
    "manifest": "No usable requested audio stream was found. Check song availability and quality.",
    "format-unavailable": "The requested audio format is not available for this track.",
    "download": "Audio download/decryption failed. Check network and apple-music-bridge status.",
    "finalize": "Downloaded media could not be finalized or tagged. Report this category.",
    "unavailable": "The requested track/format is unavailable. Try another song or quality.",
    "proxy": "Downloader network configuration failed. Report this category.",
    "process-exit": "Downloader stopped before completing this request.",
    "no-result": "Downloader returned no valid media summary. Check local setup or retry later.",
    "timeout": "Download timed out. Check that your wrapper is running; retry.",
    "cancelled": "Download cancelled.",
    "storage-limit": "Download exceeded the 2 GiB staging allowance.",
    "storage-space": "Not enough free disk space; download stopped.",
    "media-invalid": "Downloader returned missing, empty, unsupported or unreadable audio.",
    "probe-unavailable": "Cannot inspect audio. Install ffprobe and check the downloaded format.",
    "downloader-missing": "Downloader not found. Check installation with apple-music-bridge setup.",
    "local-io": "Download could not complete. Check paths, permissions and free disk space.",
    "download-fairplay": "The FairPlay audio route failed; the lower-level cause is unknown.",
    "fairplay-playlist": (
        "The FairPlay media-playlist structure is unsupported or lacks expected segments. "
        "This does not identify an HTTP status or prove format unavailability."
    ),
    "fairplay-container": "The FairPlay audio container did not match the expected structure.",
    "fairplay-read": "The FairPlay audio fragment could not be read; the cause is unknown.",
    "fairplay-decrypt": "The FairPlay audio fragment could not be decrypted.",
    "fairplay-write": "The FairPlay audio fragment could not be encoded or written.",
    "download-aac": "The AAC audio route failed; the lower-level cause is unknown.",
    "aac-playlist-fetch": "The AAC media playlist request failed; the cause is unknown.",
    "aac-playlist-parse": "The AAC media playlist could not be parsed or used.",
    "aac-license": (
        "The AAC content-key/license stage failed; this alone does not prove login failure."
    ),
    "aac-media": "The AAC media request or download failed; the cause is unknown.",
    "aac-container": "The AAC audio container could not be read or had an unsupported structure.",
    "aac-decrypt": "The AAC audio initialization or fragment could not be decrypted.",
    "aac-write": "The AAC audio output could not be encoded or written.",
}

# The pinned wrapper returns HTTP 200 even for application errors (e.g. a
# missing manifest is {code: 404, ...}). Distinguish those from HTTP/transport
# failures without keeping the upstream message, response body or request URL.
for _endpoint in ("status", "m3u8", "key", "webplayback", "license"):
    for _kind in ("api", "http"):
        for _code in (400, 401, 403, 404, 429, 500, 502, 503, 504):
            DIAGNOSTICS[f"wrapper-{_endpoint}-{_kind}-{_code}"] = (
                f"Local lite /{_endpoint} returned "
                f"{'application code' if _kind == 'api' else 'HTTP status'} {_code}. "
                + (
                    "The service was reached but could not provide this song's manifest. "
                    "Check the song/storefront; this alone does not prove expired login."
                    if _endpoint == "m3u8" and _kind == "api" and _code == 404
                    else "This code does not distinguish unavailable audio from network, "
                    "response or account-session failures. It is not a safe format-fallback cue."
                    if _endpoint == "webplayback" and _kind == "api" and _code == 404
                    else "The service was reached; this is not a connection-refused error. "
                    "Report this category and storefront."
                )
            )
    for _kind, _message in {
        "refused": "Connection was refused. Check whether the managed service is still running.",
        "timeout": "The request timed out. Check apple-music-bridge status and retry.",
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
        rb"lite-server /(status|m3u8|key|webplayback|license) returned (code=)?"
        rb"(400|401|403|404|429|500|502|503|504)(?:\s|:|$)",
        detail,
    )
    if match:
        endpoint = match[1].decode("ascii")
        kind = "api" if match[2] else "http"
        return f"wrapper-{endpoint}-{kind}-{match[3].decode('ascii')}"
    # Only recognize transport errors for the configured loopback endpoint.
    transport = re.fullmatch(
        rb'(Get|Post) "http://127\.0\.0\.1:12340/'
        rb'(status|m3u8|key|webplayback|license)(?:\?[^"\r\n]{0,4096})?": ([^\r\n]{1,512})',
        detail,
    )
    if transport:
        endpoint = transport[2].decode("ascii")
        if transport[1] != (b"Post" if endpoint == "license" else b"Get"):
            return f"wrapper-{endpoint}-unknown"
        if transport[3] == b"dial tcp 127.0.0.1:12340: connect: connection refused":
            return f"wrapper-{endpoint}-refused"
        if transport[3] in {
            b"context deadline exceeded",
            b"context deadline exceeded (Client.Timeout exceeded while awaiting headers)",
            b"net/http: request canceled (Client.Timeout exceeded while awaiting headers)",
            b"net/http: timeout awaiting response headers",
            b"dial tcp 127.0.0.1:12340: i/o timeout",
        } or re.fullmatch(
            rb"read tcp 127\.0\.0\.1:[0-9]{1,5}->127\.0\.0\.1:12340: i/o timeout",
            transport[3],
        ):
            return f"wrapper-{endpoint}-timeout"
    if detail.startswith(
        (b"invalid character ", b"json: cannot unmarshal", b"unexpected end of JSON")
    ):
        return f"wrapper-{endpoint}-invalid-response"
    return f"wrapper-{endpoint}-unknown"


def nested_wrapper_category(detail, endpoint):
    """Only an explicit endpoint signature identifies a nested wrapper failure."""
    path = endpoint.encode("ascii")
    method = b"Post" if endpoint == "license" else b"Get"
    if detail.startswith(b"lite-server /" + path + b" returned ") or re.match(
        method + rb' "http://127\.0\.0\.1:12340/' + path + rb'(?:\?|": )', detail
    ):
        return wrapper_category(detail, endpoint)
    return None


def download_category(detail, route):
    """Pinned amdl 452ba0b wrappers identify stages, never private error suffixes.

    FairPlay Run does not check CDN HTTP status before parsing. In particular,
    container/playlist errors must not be interpreted as HTTP or format misses.
    AAC DownloadSong propagates playlist and media errors directly, and wraps
    license exchange errors exactly once in 'acquire content key: '.
    """
    if route == "fairplay":
        endpoint = nested_wrapper_category(detail, "key")
        if endpoint:
            return endpoint
        if detail in {
            b"m3u8 not of media type",
            b"no segments extracted from playlist",
            b"non-byterange playlists are currently unsupported",
        }:
            return "fairplay-playlist"
        if detail in {b"no init segment found", b"segment number out of sync"} or re.fullmatch(
            rb"unexpected box type [^\r\n]{4}, should be ftyp or moov", detail
        ):
            return "fairplay-container"
        if detail.startswith(b"read fragment: "):
            return "fairplay-read"
        if re.match(rb"decryptFragment seq [0-9]{1,10}: ", detail):
            return "fairplay-decrypt"
        if re.match(rb"encode fragment seq [0-9]{1,10} failed: ", detail):
            return "fairplay-write"
        return "download-fairplay"

    endpoint = nested_wrapper_category(detail, "webplayback")
    if endpoint:
        return endpoint
    if detail.startswith(b"acquire content key: "):
        nested = detail.removeprefix(b"acquire content key: ")
        endpoint = nested_wrapper_category(nested, "license")
        if endpoint:
            return endpoint
        if nested == b"empty license in lite-server response" or nested.startswith(
            b"failed to decode license: "
        ):
            return "wrapper-license-invalid-response"
        return "aac-license"
    if detail.startswith(b"fetch media playlist: "):
        return "aac-playlist-fetch"
    if detail.startswith(b"parse media playlist: ") or detail in {
        b"not a media playlist",
        b"no matching key information found",
        b"no initialization segment found",
        b"invalid DRM key URI",
        b"playlist is not encrypted",
    }:
        return "aac-playlist-parse"
    if detail.startswith((b"create media request: ", b"download media: ")):
        return "aac-media"
    if detail.startswith(b"failed to decode file: ") or detail in {
        b"file is not fragmented",
        b"no init part of file",
        b"styp inside an incomplete fragment",
        b"mdat without preceding moof",
        b"truncated fragment at end of file",
    }:
        return "aac-container"
    if detail.startswith((b"failed to decrypt init: ", b"failed to decrypt segment: ")):
        return "aac-decrypt"
    if detail.startswith(
        (
            b"failed to write init: ",
            b"failed to encode segment: ",
            b"failed to write output: ",
            b"create temporary output: ",
            b"close temporary output: ",
            b"replace output: ",
        )
    ):
        return "aac-write"
    return "download-aac"


def diagnostic_priority(category):
    if category in {"format-unavailable", "unavailable"}:
        return 0
    if category in {"catalog", "download"}:
        return 1
    if category in {"download-fairplay", "download-aac"}:
        return 2
    return 3


ERROR_PREFIXES = (
    (b"load Config failed:", "config"),
    (b"proxy config error:", "proxy"),
    (b"Failed to get token.", "catalog-token"),
    (b"Failed to get song response.", "catalog"),
    (b"Failed to get album response.", "catalog"),
    (b"Failed to rip song:", "catalog"),
    (b"Failed to rip album:", "catalog"),
    ("\u26a0 Failed to extract info from manifest:".encode(), "manifest"),
    (b"Failed to run v4:", "download-fairplay"),
    (b"Failed to dl aac-lc via lite-server:", "download-aac"),
    (b"Defragment failed:", "finalize"),
    ("\u26a0 Failed to fix ALAC:".encode(), "finalize"),
    ("\u26a0 Failed to write tags in media:".encode(), "finalize"),
)


class DownloadError(PlayerError):
    """A fixed diagnostic category, separate from arbitrary exception text."""

    def __init__(self, category, *, exit_code=None):
        self.category = (
            category if type(category) is str and category in DIAGNOSTICS else "download"
        )
        self.exit_code = exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else None
        detail = "" if self.exit_code is None else f"; exit {self.exit_code}"
        super().__init__(f"Download failed [{self.category}{detail}]. {DIAGNOSTICS[self.category]}")


class FormatUnavailable(DownloadError):
    """An explicit format miss, rather than a network, account or download failure."""


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
        # Pinned extractMedia returns this exact message only after successfully
        # reading a master playlist and finding no matching codec. Other errors
        # with the same prefix include HTTP and parsing failures: do not retry.
        if line == "\u26a0 Failed to extract info from manifest: no codec found".encode():
            self.error = self.error or "format-unavailable"
            return
        if line == b"Failed to dl aac-lc via lite-server: Unavailable":
            self.error = self.error or "unavailable"
            return
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
                elif category in {"download-fairplay", "download-aac"}:
                    # Require the source's exact delimiter; do not search suffixes
                    # for keywords or classify unrelated embedded error text.
                    if line.startswith(prefix + b" "):
                        category = download_category(
                            line[len(prefix) + 1 :], category.removeprefix("download-")
                        )
                # Preserve the specific HTTP category over a later general error.
                if not self.error or diagnostic_priority(category) > diagnostic_priority(
                    self.error
                ):
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
        raise DownloadError("media-invalid") from None
    except Exception:
        raise DownloadError("probe-unavailable") from None


def lite_job_config(output, country):
    # Pinned v0.1.4 nested schema.
    return {
        "general": {
            "lite-server": LITE_ENDPOINT,
            "storefront": country,
            "exit-on-error": True,
            "max-memory-limit": 256,
        },
        "media": {
            "get-m3u8-mode": "all",
            "alac-max": 192000,
            "atmos-max": 2768,
            "alac-fix": True,
            "aac-type": "aac-lc",
        },
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
            raise DownloadError("storage-limit")
        if shutil.disk_usage(job).free < 128 * 1024**2:
            raise DownloadError("storage-space")

    def run(self, track, quality, cancel):
        if quality not in {"atmos", "lossless"}:
            raise PlayerError("Unknown audio quality preference.")
        # One bounded request, with fresh staging and subprocesses per attempt.
        # The managed wrapper/session and next track's preference never change.
        qualities = ("atmos", "lossless", "aac") if quality == "atmos" else ("lossless", "aac")
        deadline = time.monotonic() + self.timeout
        for candidate in qualities:
            try:
                result = self._run(track, candidate, cancel, deadline)
            except FormatUnavailable:
                if candidate == qualities[-1]:
                    raise
                continue
            job, _, media = result
            if candidate == "atmos" and media.codec == "ac3":
                # amdl --atmos can select ordinary Dolby Digital. Prefer actual
                # lossless stereo over this non-Atmos lossy variant.
                shutil.rmtree(job)
                continue
            return result

    def _run(self, track, quality, cancel, deadline):
        binary = shutil.which(self.binary)
        if not binary:
            raise DownloadError("downloader-missing")
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
                raise DownloadError("cancelled")
            if time.monotonic() >= deadline:
                raise DownloadError("timeout")
            args = [str(Path(binary).absolute()), "--json"]
            args.extend(["--config", str(config)])
            if quality == "atmos":
                args.append("--atmos")
            elif quality == "aac":
                args.append("--aac")
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
            last_check = 0.0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, summary)
                selector.register(process.stderr, selectors.EVENT_READ, diagnostics)
                while selector.get_map():
                    if cancel.is_set():
                        raise DownloadError("cancelled")
                    now = time.monotonic()
                    if now >= deadline:
                        raise DownloadError("timeout")
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
                raise DownloadError("timeout") from None
            if code != 0 or not summary.path:
                misses = {"format-unavailable", "unavailable"}
                errors = (summary.error, diagnostics.error)
                # Hard errors/warnings must not be hidden by a format miss on
                # the other pipe (or an unavailable web-manifest fallback).
                category = max(
                    (error for error in errors if error and error not in misses),
                    key=diagnostic_priority,
                    default=None,
                )
                category = (
                    category
                    or summary.warning
                    or diagnostics.warning
                    or next((error for error in errors if error), None)
                )
                if category:
                    # Pinned amdl exits zero for an unavailable codec. A crash,
                    # signal or other nonzero exit is not a safe downgrade cue.
                    if category in misses and code != 0:
                        raise DownloadError("process-exit", exit_code=code)
                    if category in misses:
                        raise FormatUnavailable(category)
                    raise DownloadError(category)
                raise DownloadError("no-result", exit_code=code)
            stop_group(process)
            self.check_stage(job)
            source = Path(summary.path)
            if not source.is_absolute():
                source = job / source
            source = safe_path(output, source)
            if not source.is_file() or source.stat().st_size == 0:
                raise DownloadError("media-invalid")
            media = probe(source)
            if cancel.is_set():
                raise DownloadError("cancelled")
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
            raise DownloadError("local-io") from None
        finally:
            if process and process.stdout:
                process.stdout.close()
            if process and process.stderr:
                process.stderr.close()
            config.unlink(missing_ok=True)
