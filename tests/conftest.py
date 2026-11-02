import os
import subprocess

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def audio(tmp_path_factory):
    root = tmp_path_factory.mktemp("audio")
    paths = {}
    for codec, extension in [("alac", "m4a"), ("aac", "m4a"), ("ac3", "ac3"), ("eac3", "eac3")]:
        path = root / f"{codec}.{extension}"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                codec,
                str(path),
            ],
            check=True,
        )
        paths[codec] = path
    return paths


@pytest.fixture
def fake_downloader(tmp_path, audio, monkeypatch):
    monkeypatch.setenv("TEST_AUDIO", str(audio["alac"]))
    path = tmp_path / "fake-downloader"
    path.write_text("""#!/usr/bin/env python3
import json, os, shutil, pathlib, sys, time, subprocess
config = json.loads(pathlib.Path("config.yaml").read_text())
assert config["general"]["exit-on-error"] and not config["metadata"]["lyrics"]["embed"]
assert "--config" in sys.argv
assert "--json" in sys.argv
print("Config: token=TEST_SECRET_DO_NOT_LEAK", flush=True)
mode = os.environ.get("TEST_MODE", "ok")
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    pathlib.Path(os.environ["TEST_PID"]).write_text(str(child.pid))
    time.sleep(120)
if mode == "zero":
    sys.exit(0)
if mode == "malformed":
    print("[malformed secret TEST_SECRET_DO_NOT_LEAK")
    sys.exit(0)
output = pathlib.Path(config["paths"]["alac"]) / "track.m4a"
if mode == "symlink":
    output.symlink_to(os.environ["TEST_AUDIO"])
else:
    shutil.copyfile(os.environ["TEST_AUDIO"], output)
if mode == "escape":
    output = pathlib.Path(os.environ["TEST_AUDIO"])
print(json.dumps([{"path": str(output), "song": "TEST_SECRET_DO_NOT_LEAK"}]))
""")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def fake_mpv(tmp_path):
    path = tmp_path / "fake-mpv"
    path.write_text("""#!/usr/bin/env python3
import json, socket, sys, os
address = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--input-ipc-server="))
sock = socket.socket(socket.AF_UNIX)
sock.bind(address)
sock.listen(1)
conn, _ = sock.accept()
props = {"pause": True, "time-pos": 0, "duration": 1,
         "audio-params": {"format": os.environ.get("TEST_MPV_FORMAT", "s16")},
         "audio-out-params": {"format": os.environ.get("TEST_MPV_FORMAT", "s16")}}
def send(data):
    conn.sendall(json.dumps(data).encode() + b"\\n")
with conn.makefile("rb") as f:
    for line in f:
        command = json.loads(line)["command"]
        if command[0] == "observe_property":
            name = command[2]
            send({"event": "property-change", "name": name, "data": props[name]})
        elif command[:2] == ["set_property", "pause"]:
            props["pause"] = command[2]
            send({"event": "property-change", "name": "pause", "data": command[2]})
        elif command[:2] == ["cycle", "pause"]:
            props["pause"] = not props["pause"]
            send({"event": "property-change", "name": "pause", "data": props["pause"]})
        elif command[0] == "seek":
            send({"event": "property-change", "name": "time-pos", "data": command[1]})
        send({"error": "success"})
""")
    path.chmod(0o755)
    return str(path)


def wait_until(callback, timeout=8):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callback():
            return
        time.sleep(0.02)
    pytest.fail("Timed out waiting for test process")


@pytest.fixture(scope="session")
def app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
