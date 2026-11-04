import hashlib
import io
import os
import shutil
import socket
import stat
import threading
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import wait_until

from apple_music_player.backend_pins import RuntimePin
from apple_music_player.downloader import Downloader, lite_job_config
from apple_music_player.model import PlayerError, Track
from apple_music_player.runtime import ManagedRuntime, RuntimeEvents, health_status, memory_args
from apple_music_player.runtime_install import REQUIRED, RuntimeStore, extract_runtime

FAKE_LAUNCHER = b"""#!/usr/bin/env python3
import os, pathlib, sys, time
root = pathlib.Path.cwd()
mode = os.environ.get("TEST_LITE_MODE", "ok")
if mode == "denied":
    print("unshare: Operation not permitted", file=sys.stderr, flush=True)
    sys.exit(0)  # Rootless parent exit status alone must never establish success.
args = sys.argv
if "LITE_ARGS_FILE" in os.environ:
    args = pathlib.Path(os.environ["LITE_ARGS_FILE"]).read_text().splitlines()
if "--login" in args:
    assert "fixture-secret" not in " ".join(sys.argv)
    assert os.stat(os.environ["LITE_ARGS_FILE"]).st_mode & 0o777 == 0o600
    print("raw password fixture-secret never forward", file=sys.stderr, flush=True)
    if mode == "fail":
        sys.exit(0)
    prompt = "2026-11-02 01:00:00.000 [WARN ] Enter your 2FA code into /data/2fa.txt"
    print(prompt, file=sys.stderr, flush=True)
    code = root / "rootfs/data/2fa.txt"
    while not code.exists():
        time.sleep(0.05)
    assert code.is_fifo()
    # bridge.cpp file_exists() opens/closes without reading, twice before fscanf.
    for _ in range(2):
        with code.open():
            pass
    with code.open() as f:
        assert f.read(6) == "123456"
    code.unlink()
    for name in ("DEV_TOKEN", "MUSIC_TOKEN", "STOREFRONT_ID"):
        (root / "rootfs/data" / name).write_text("synthetic-token")
    print("2026-11-02 01:00:00.000 [INFO ] login complete, exiting", file=sys.stderr, flush=True)
else:
    assert sys.argv[sys.argv.index("--host")+1] == "127.0.0.1"
    assert "--debug" not in sys.argv and "--cors" not in sys.argv
    if mode == "crash":
        sys.exit(0)
    if mode == "listen":
        import socket, signal
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        listener.bind(("127.0.0.1", 12340))
        listener.listen()
        # A child holding the socket and inherited lock must also be stopped.
        if os.fork() == 0:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(120)
"""


@pytest.fixture
def package(tmp_path, fake_downloader, monkeypatch):
    binary = Path(fake_downloader).read_bytes()
    archive = tmp_path / "pinned.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name in REQUIRED:
            z.writestr(name, FAKE_LAUNCHER if name == "wrapper-lite-rootless" else b"fixture")
        z.writestr("wrapper-lite", b"privileged launcher not installed")
        z.writestr("rootfs/data/MUSIC_TOKEN", b"never import account material")
    pin = RuntimePin(
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        hashlib.sha256(binary).hexdigest(),
        "https://github.com/test/amdl",
    )

    def download(*args, **kwargs):
        stream = io.BytesIO(binary)
        stream.url = "https://github.com/test/amdl"
        return stream

    monkeypatch.setattr("apple_music_player.runtime_install.open_artifact", download)
    monkeypatch.setattr("apple_music_player.runtime.host_problem", lambda: "")
    return archive, pin


@pytest.fixture
def installed(tmp_path, package):
    archive, pin = package
    store = RuntimeStore(tmp_path / "runtime", pin)
    previous_umask = os.umask(0o022)
    try:
        store.install(archive, threading.Event())
    finally:
        os.umask(previous_umask)
    yield store
    store.close()


def test_install_pin_permissions_existing_and_rollback(installed, package, tmp_path):
    archive, _ = package
    installed.verify()
    assert not (installed.current / "wrapper-lite").exists()
    assert not (installed.current / "rootfs/data/MUSIC_TOKEN").exists()
    for path in installed.current.rglob("*"):
        assert path.stat().st_mode & 0o077 == 0
    original = installed.launcher.read_bytes()
    installed.install(archive, threading.Event())
    assert installed.launcher.read_bytes() == original
    installed.launcher.write_bytes(b"modified")
    with pytest.raises(PlayerError, match="absent or changed"):
        installed.install(archive, threading.Event())
    assert installed.launcher.read_bytes() == b"modified"  # No silent overwrite/repair.
    with pytest.raises(PlayerError, match="Another"):
        RuntimeStore(installed.root, installed.pin)


def test_cancel_retry_and_hash_mismatch(tmp_path, package, monkeypatch):
    archive, pin = package
    store = RuntimeStore(tmp_path / "runtime", pin)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(PlayerError, match="cancelled"):
        store.install(archive, cancel)
    cancel.clear()
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(archive.read_bytes() + b"bad")
    with pytest.raises(PlayerError, match="Wrapper SHA"):
        store.install(corrupt, cancel)
    assert not store.current.exists() and not list(store.root.glob("install-*"))
    store.install(archive, cancel)
    store.verify()
    store.close()


def test_downloader_hash_mismatch_no_install(tmp_path, package, monkeypatch):
    archive, pin = package

    def download(*args, **kwargs):
        stream = io.BytesIO(b"wrong binary")
        stream.url = "https://github.com/test/amdl"
        return stream

    monkeypatch.setattr("apple_music_player.runtime_install.open_artifact", download)
    store = RuntimeStore(tmp_path / "runtime", pin)
    with pytest.raises(PlayerError, match="Downloader SHA"):
        store.install(archive, threading.Event())
    assert not store.current.exists()
    store.close()


@pytest.mark.parametrize(
    "name,mode",
    [
        ("../escape", 0),
        ("/absolute", 0),
        ("rootfs/system/link", stat.S_IFLNK | 0o777),
        ("rootfs/system/device", stat.S_IFCHR | 0o600),
        ("rootfs\\escape", 0),
    ],
)
def test_archive_rejects_paths_links_devices(tmp_path, name, mode):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        z.writestr(info, b"target")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(PlayerError, match="unsafe"):
        extract_runtime(archive, out, threading.Event())
    assert not (tmp_path / "escape").exists()


def test_login_memfd_and_events_redaction(tmp_path):
    fd = memory_args(["--login", "fixture-user:fixture-secret"])
    try:
        assert os.read(fd, 4096) == b"--login\nfixture-user:fixture-secret\n"
        with pytest.raises(OSError):
            os.write(fd, b"modify")
    finally:
        os.close(fd)
    with pytest.raises(OSError):
        os.fstat(fd)
    events = RuntimeEvents()
    events.accept(b"token fixture-secret login complete, exiting\n")
    assert not events.login_complete.is_set()
    assert "fixture-secret" not in repr(vars(events))


def test_login_two_factor_cancel_restore_and_ready(installed, monkeypatch):
    monkeypatch.setattr("apple_music_player.runtime.health_details", lambda: (True, ("us",)))
    runtime = ManagedRuntime(installed)
    original = runtime.data / "old-session"
    original.write_text("synthetic old state")
    runtime.login("fixture-user", "fixture-secret")

    def requested():
        runtime.poll()
        return runtime.fifo.exists()

    wait_until(requested)
    assert runtime.fifo.is_fifo() and runtime.fifo_fd is not None
    fd = runtime.args_fd
    runtime.cancel.set()
    runtime.poll()
    assert runtime.process is None and original.exists() and not runtime.fifo.exists()
    assert runtime.fifo_fd is None
    with pytest.raises(OSError):
        os.fstat(fd)
    runtime.cancel.clear()
    runtime.login("fixture-user", "fixture-secret")
    wait_until(requested)
    runtime.submit_code("123456")
    wait_until(lambda: (runtime.poll(), runtime.process is None)[1])
    assert runtime.signed_in() and "Signed in locally" in runtime.state
    assert not original.exists() and not runtime.fifo.exists() and runtime.args_fd is None
    for p in runtime.data.iterdir():
        assert p.stat().st_mode & 0o077 == 0
    runtime.start()
    wait_until(lambda: (runtime.poll(), runtime.ready)[1])
    assert "media access not yet verified" in runtime.state
    runtime.stop()
    assert not runtime.ready and runtime.process is None


def test_denied_busy_crash_unauthed_health(installed, monkeypatch):
    runtime = ManagedRuntime(installed)
    with socket.socket() as busy:
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        busy.bind(("127.0.0.1", 12340))
        busy.listen()
        with pytest.raises(PlayerError, match="busy"):
            runtime.start()
    monkeypatch.setenv("TEST_LITE_MODE", "denied")
    runtime.start()
    wait_until(lambda: (runtime.poll(), runtime.process is None)[1])
    assert "denied" in runtime.state and not runtime.ready
    monkeypatch.setenv("TEST_LITE_MODE", "crash")
    runtime.start()
    wait_until(lambda: (runtime.poll(), runtime.process is None)[1])
    assert "exited" in runtime.state
    monkeypatch.setenv("TEST_LITE_MODE", "ok")
    monkeypatch.setattr("apple_music_player.runtime.health_details", lambda: (True, ("us",)))
    runtime.start()
    runtime.poll()
    assert "authentication not verified" in runtime.state and not runtime.ready
    monkeypatch.setattr("apple_music_player.runtime.health_details", lambda: (False, ()))
    runtime.last_healthy -= 30
    runtime.poll()
    assert runtime.process is None and "health failed" in runtime.state


def test_zero_exit_is_not_login_success_and_crash_recovery(installed, monkeypatch):
    runtime = ManagedRuntime(installed)
    original = runtime.data / "old-session"
    original.write_text("keep")
    monkeypatch.setenv("TEST_LITE_MODE", "fail")
    runtime.login("fixture-user", "fixture-secret")
    wait_until(lambda: (runtime.poll(), runtime.process is None)[1])
    assert not runtime.signed_in() and original.exists()
    assert not runtime.transaction.exists()
    # Crash before the rename must not delete the original session.
    runtime.transaction.write_text("had-data")
    runtime.recover_login()
    assert original.read_text() == "keep"
    # Crash after the old session moved: roll back only the unfinished login.
    os.replace(runtime.data, runtime.backup)
    runtime.data.mkdir()
    (runtime.data / "partial").write_text("discard")
    runtime.transaction.write_text("had-data")
    runtime.recover_login()
    assert original.read_text() == "keep" and not (runtime.data / "partial").exists()


def test_health_schema_never_authentication(monkeypatch):
    response = io.BytesIO(b'{"code":0,"data":{"regions":["143441"]}}')
    response.url = "http://127.0.0.1:12340/status"
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr("apple_music_player.runtime.build_opener", lambda *_: opener)
    assert health_status() is True  # Service health, not authenticated-account evidence.
    response = io.BytesIO(b'{"code":500,"message":"fixture-secret"}')
    response.url = "http://127.0.0.1:12340/status"
    opener.open.return_value = response
    assert health_status() is False


def test_no_plaintext_fallback_when_memfd_unavailable(installed, monkeypatch):
    runtime = ManagedRuntime(installed)
    original = runtime.data / "original"
    original.write_text("keep")
    monkeypatch.delattr(os, "memfd_create")
    with pytest.raises(PlayerError, match="private login"):
        runtime.login("fixture-user", "fixture-secret")
    assert original.exists() and runtime.process is None
    assert not runtime.transaction.exists() and runtime.args_fd is None
    for file in installed.current.rglob("*"):
        if file.is_file() and file.name not in {"wrapper-lite-rootless", "manifest.json", "amdl"}:
            assert b"fixture-secret" not in file.read_bytes()


def test_host_policy_gate_without_executing_runtime(installed, monkeypatch):
    runtime = ManagedRuntime(installed)
    monkeypatch.setattr("apple_music_player.runtime.host_problem", lambda: "Namespaces denied")
    with pytest.raises(PlayerError, match="Namespaces denied"):
        runtime.start()
    assert runtime.process is None


def test_supervisor_stops_fake_service_if_player_parent_dies(tmp_path):
    import subprocess
    import sys

    script = tmp_path / "fake-service.py"
    pid = tmp_path / "service.pid"
    script.write_text(
        "import os,time,pathlib\n"
        f"pathlib.Path({str(pid)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(120)\n"
    )
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import os,subprocess,sys,time
subprocess.Popen([sys.executable, "-m", "apple_music_player.runtime_child", str(os.getpid()),
                  "-1", "-1", sys.executable, sys.argv[1]], start_new_session=True,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(120)
""",
            str(script),
        ]
    )
    try:
        wait_until(pid.exists)
        child = int(pid.read_text())
        parent.kill()
        parent.wait()

        def gone():
            statfile = Path(f"/proc/{child}/stat")
            return not statfile.exists() or statfile.read_text().split()[2] == "Z"

        wait_until(gone)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_prompt_does_not_publish_readiness_before_fifo_or_after_cancel(installed):
    runtime = ManagedRuntime(installed)
    runtime.process = Mock()
    runtime.process.poll.return_value = None
    runtime.mode = "login"
    runtime.state = "Signing in locally"
    import time

    runtime.started = time.monotonic()
    runtime.events.two_factor.set()
    try:
        assert not runtime.can_submit_code
        with pytest.raises(PlayerError, match="No verification"):
            runtime.submit_code("123456")
        runtime.poll()
        assert runtime.can_submit_code and runtime.fifo_fd is not None
        runtime.state = "Different display wording"
        assert runtime.can_submit_code  # Status text is never a control protocol.
        runtime.cancel.set()
        assert not runtime.can_submit_code
        runtime.cancel.clear()
        runtime.process.poll.return_value = 0
        assert not runtime.can_submit_code
    finally:
        runtime.process = None
        runtime.clean_fifo()


def test_fragmented_upstream_prompt_is_received_without_pipe_eof():
    events = RuntimeEvents()
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    reader = threading.Thread(target=events.drain, args=(stream,))
    reader.start()
    try:
        os.write(write_fd, b"unrecognized fixture-secret\n2026-11-02 13:00:00.000 [WA")
        assert not events.two_factor.is_set()
        os.write(write_fd, b"RN ] Enter your 2FA code into /data/2fa.txt\n")
        assert events.two_factor.wait(2)  # Writer remains open, just like a waiting login.
        assert reader.is_alive()
        assert "fixture-secret" not in repr(vars(events))
    finally:
        os.close(write_fd)
        reader.join(timeout=2)


def test_repeated_start_stop_releases_port_stream_and_inherited_lock(installed, monkeypatch):
    monkeypatch.setenv("TEST_LITE_MODE", "listen")
    runtime = ManagedRuntime(installed)

    def listening():
        with socket.socket() as connection:
            return connection.connect_ex(("127.0.0.1", 12340)) == 0

    try:
        for _ in range(3):
            runtime.start()
            wait_until(listening)
            process = runtime.process
            stream = process.stderr
            runtime.stop()
            assert process.poll() is not None and stream.closed
            with socket.socket() as available:
                available.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                available.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                available.bind(("127.0.0.1", 12340))
        root, pin = installed.root, installed.pin
        runtime.close()
        # No orphan descendant may retain the inherited flock after app shutdown.
        reopened = RuntimeStore(root, pin)
        reopened.close()
    finally:
        runtime.close()


def test_preflight_allows_time_wait_but_not_live_owner(installed):
    runtime = ManagedRuntime(installed)
    # Reproduce httplib closing a completed /status connection before the client.
    with socket.socket() as listener, socket.socket() as client:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        listener.bind(("127.0.0.1", 12340))
        listener.listen()
        with pytest.raises(PlayerError, match="busy"):
            runtime._preflight()
        probe, _ = listener.accept()
        probe.close()
        client.settimeout(2)
        client.connect(("127.0.0.1", 12340))
        peer, _ = listener.accept()
        peer.close()
        assert client.recv(1) == b""
    # The former plain bind reports busy despite there being no listener.
    with socket.socket() as old_check:
        with pytest.raises(OSError):
            old_check.bind(("127.0.0.1", 12340))
    runtime._preflight()


def test_health_region_allowlist(monkeypatch):
    from apple_music_player.runtime import health_details

    response = io.BytesIO(b'{"code":0,"data":{"regions":["BR","143441","private-value", "br"]}}')
    response.url = "http://127.0.0.1:12340/status"
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr("apple_music_player.runtime.build_opener", lambda *_: opener)
    assert health_details() == (True, ("br",))


def test_lite_contract_and_nested_output(tmp_path, audio, monkeypatch):
    binary = tmp_path / "fake-lite-downloader"
    binary.write_text("""#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
assert "--config" in sys.argv and "--json" in sys.argv
config = json.loads(pathlib.Path(sys.argv[sys.argv.index("--config") + 1]).read_text())
assert config["general"]["lite-server"] == "http://127.0.0.1:12340"
assert config["general"]["exit-on-error"]
assert not config["metadata"]["lyrics"]["embed"]
assert not config["metadata"]["artwork"]["embed"]
assert not config["convert"]["after-download"]
path = pathlib.Path(config["paths"]["alac"]) / "67890" / "12345.m4a"
path.parent.mkdir()
shutil.copyfile(os.environ["TEST_AUDIO"], path)
print("Warning: failed to query lite-server /status: fixture-secret", file=sys.stderr)
print("Queue 1 of 1: Song->Album")
print(json.dumps([{"path": str(path), "artist": "Fixture", "artist_id": "42",
                   "album": "Fixture album", "song": "ignored"}]))
""")
    binary.chmod(0o700)
    monkeypatch.setenv("TEST_AUDIO", str(audio["alac"]))
    stage = tmp_path / "stage"
    stage.mkdir()
    job, source, media = Downloader(str(binary), stage, backend="lite").run(
        Track("us", "12345", "Fixture"), "lossless", threading.Event()
    )
    assert "Lossless" in media.label and source.exists()
    shutil.rmtree(job)
    config = lite_job_config(stage, "gb")
    assert "alac-save-folder" not in config
