import subprocess

import pytest


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


def wait_until(callback, timeout=8):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callback():
            return
        time.sleep(0.02)
    pytest.fail("Timed out waiting for test process")
