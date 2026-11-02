import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from .downloader import stop_group
from .model import PlayerError


class Mpv:
    def __init__(self, binary="mpv"):
        self.binary = binary
        self.process = None
        self.socket = None
        self.temp = None
        self.buffer = b""
        self.properties = {}
        self.passthrough = False
        self.loaded = False
        self.output_verified = False
        self.startup_complete = False

    def start(self, path, media, device):
        self.close()
        self.passthrough = media.codec in {"eac3", "ac3"}
        if self.passthrough and (device == "auto" or not device.strip()):
            raise PlayerError("Select your HDMI device in Settings for Dolby passthrough.")
        self.temp = tempfile.TemporaryDirectory(prefix="amp-ipc-")
        os.chmod(self.temp.name, 0o700)
        self.address = str(Path(self.temp.name) / "mpv.sock")
        args = [
            self.binary,
            "--no-config",
            "--no-video",
            "--idle=yes",
            "--pause=yes",
            "--audio-channels=auto",
            f"--input-ipc-server={self.address}",
            f"--audio-device={device}",
            "--input-terminal=no",
            "--terminal=no",
        ]
        if self.passthrough:
            args.append(f"--audio-spdif={media.codec}")
        args.extend(["--", str(path)])
        try:
            self.process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            self.close()
            raise PlayerError(
                "mpv could not start. Install mpv and check its executable path."
            ) from None
        self.started = time.monotonic()
        self.properties = {}
        self.loaded = False
        self.output_verified = False
        self.startup_complete = False
        self.buffer = b""

    def command(self, *args):
        if not self.socket:
            raise PlayerError("Playback is not ready yet.")
        try:
            self.socket.sendall(json.dumps({"command": args}).encode() + b"\n")
        except OSError:
            raise PlayerError("Lost connection to mpv. Retry playback.") from None

    def poll(self):
        if not self.process:
            return []
        if self.process.poll() is not None:
            raise PlayerError(
                "mpv exited. Check the selected audio device and HDMI connection, then retry."
            )
        if not self.socket:
            candidate = socket.socket(socket.AF_UNIX)
            candidate.settimeout(0.05)
            try:
                candidate.connect(self.address)
            except OSError:
                candidate.close()
                if time.monotonic() - self.started > 5:
                    raise PlayerError(
                        "mpv IPC did not start. Check mpv installation and audio device."
                    ) from None
                return []
            self.socket = candidate
            for i, name in enumerate(
                ("pause", "time-pos", "duration", "audio-params", "audio-out-params")
            ):
                self.command("observe_property", i, name)
        try:
            while True:
                chunk = self.socket.recv(16384)
                if not chunk:
                    raise PlayerError("mpv closed its connection. Retry playback.")
                self.buffer += chunk
                if len(self.buffer) > 1_000_000:
                    raise PlayerError("Invalid response from mpv.")
        except TimeoutError:
            pass
        events = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                event = json.loads(line)
            except ValueError:
                raise PlayerError("Invalid response from mpv.") from None
            if event.get("event") == "property-change":
                self.properties[event["name"]] = event.get("data")
                if event["name"] in {"audio-params", "audio-out-params"}:
                    self.validate_output()
            elif event.get("event") == "file-loaded":
                self.loaded = True
            elif event.get("event") == "end-file":
                if event.get("reason") == "error":
                    raise PlayerError("mpv cannot play this file/device. Check HDMI and retry.")
                if event.get("reason") == "eof":
                    events.append("eof")
            elif event.get("error", "success") not in {"success", "property unavailable"}:
                raise PlayerError("mpv rejected a playback command.")
        self.validate_output()
        return events

    def validate_output(self):
        """Recheck observations throughout playback; only startup may auto-resume."""
        params = self.properties.get("audio-params") or {}
        output = self.properties.get("audio-out-params") or {}
        fmt = params.get("format", "")
        output_fmt = output.get("format", "")
        if not fmt or not output_fmt:
            was_verified = self.output_verified
            self.output_verified = False
            if was_verified:
                # Missing parameters cannot support an active-output claim. Keep the
                # process paused so a later recovery requires an explicit user resume.
                self.command("set_property", "pause", True)
            if not self.startup_complete and time.monotonic() - self.started > 10:
                raise PlayerError(
                    "Audio output could not be verified. Check the selected audio device."
                )
            return
        if self.passthrough and not (fmt.startswith("spdif-") and output_fmt.startswith("spdif-")):
            self.output_verified = False
            self.command("set_property", "pause", True)
            raise PlayerError(
                "mpv fell back to PCM. Playback stopped; check HDMI passthrough support."
            )
        self.output_verified = True
        if not self.startup_complete:
            self.startup_complete = True
            self.command("set_property", "pause", False)

    @property
    def output_label(self):
        if not self.output_verified:
            if self.startup_complete:
                return "Audio output unavailable · playback paused"
            return "Verifying audio output…"
        if self.passthrough:
            return "Compressed passthrough active in mpv · receiver Atmos indication not verified"
        return "Decoded PCM in mpv · hardware bit-perfect output not verified"

    def close(self):
        self.output_verified = False
        self.startup_complete = False
        if self.socket:
            self.socket.close()
            self.socket = None
        if self.process:
            stop_group(self.process)
            self.process = None
        if self.temp:
            self.temp.cleanup()
            self.temp = None
