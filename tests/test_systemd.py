import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runtime import installed as _installed
from test_runtime import package as _package

from apple_music_player import cli, systemd
from apple_music_player.bridge.http import auth_file
from apple_music_player.model import PlayerError

package = _package
installed = _installed


@pytest.fixture
def options(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(systemd.os, "geteuid", lambda: 1000)
    return SimpleNamespace(
        auth_file=tmp_path / "auth.json",
        cache=None,
        cache_gib=10,
        country="br",
        quality="lossless",
        port=4533,
        diagnostics=False,
        replace=False,
    )


def test_install_captures_paths_without_reading_secrets_or_starting(options, tmp_path, monkeypatch):
    options.auth_file.write_text("synthetic-client-secret")
    root = tmp_path / "old runtime"
    root.mkdir()
    (root / "token").write_text("synthetic-apple-secret")
    monkeypatch.setenv("APPLE_PASSWORD", "synthetic-environment-secret")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("must not run commands"))
    assert systemd.install(options, root) == 0
    target = tmp_path / "config/systemd/user" / systemd.UNIT_NAME
    text = target.read_text()
    assert "synthetic-" not in text and "APPLE_PASSWORD" not in text
    assert f'"{sys.executable}" "-u" "-m" "apple_music_player.cli" "serve"' in text
    assert f'"--runtime-root" "{root}"' in text
    assert f'"--cache" "{tmp_path}/cache/apple-music-bridge"' in text
    assert "WantedBy=default.target" in text and "KillMode=mixed" in text
    assert "Restart=on-failure" in text and "TimeoutStopSec=30" in text
    assert "UMask=0077" in text and "StandardInput=null" in text
    assert "sudo" not in text and "login" not in text and "setup" not in text
    assert target.stat().st_mode & 0o777 == 0o600
    assert options.auth_file.read_text() == "synthetic-client-secret"
    assert (root / "token").read_text() == "synthetic-apple-secret"


def test_cli_preserves_legacy_runtime_and_explicit_options(options, tmp_path):
    legacy = tmp_path / "data/apple-music-player/installed"
    legacy.mkdir(parents=True)
    assert cli.main(["install-service", "--quality", "atmos", "--country", "us"]) == 0
    target = tmp_path / "config/systemd/user" / systemd.UNIT_NAME
    text = target.read_text()
    assert f'"--runtime-root" "{legacy.parent}"' in text
    assert '"--quality" "atmos"' in text and '"--country" "us"' in text
    assert (
        cli.main(
            [
                "install-service",
                "--replace",
                "--runtime-root",
                "custom-runtime",
                "--cache",
                "custom-cache",
                "--auth-file",
                "custom-auth",
                "--port",
                "4544",
                "--cache-gib",
                "20",
                "--diagnostics",
            ]
        )
        == 0
    )
    text = target.read_text()
    for flag, path in (
        ("runtime-root", "custom-runtime"),
        ("cache", "custom-cache"),
        ("auth-file", "custom-auth"),
    ):
        assert f'"--{flag}" "{Path(path).absolute()}"' in text
    assert '"--port" "4544"' in text and '"--cache-gib" "20"' in text
    assert '"--diagnostics"' in text


def test_existing_unit_requires_replace_and_symlinks_are_refused(options, tmp_path):
    systemd.install(options, tmp_path)
    target = tmp_path / "config/systemd/user" / systemd.UNIT_NAME
    target.write_text("existing custom unit")
    with pytest.raises(PlayerError, match="already exists"):
        systemd.install(options, tmp_path)
    assert target.read_text() == "existing custom unit"
    options.replace = True
    systemd.install(options, tmp_path)
    assert "ExecStart=" in target.read_text()
    assert len(list(target.parent.iterdir())) == 1
    target.unlink()
    destination = tmp_path / "do-not-replace"
    destination.write_text("untouched")
    target.symlink_to(destination)
    with pytest.raises(PlayerError, match="already exists"):
        systemd.install(options, tmp_path)
    assert destination.read_text() == "untouched"


@pytest.mark.parametrize(
    "field,value",
    [
        ("country", "BR"),
        ("country", "br\nExecStart=bad"),
        ("port", 0),
        ("port", 65536),
        ("cache_gib", 0),
        ("cache_gib", 1025),
        ("cache", Path("bad\npath")),
    ],
)
def test_invalid_options_do_not_write_unit(options, field, value, tmp_path):
    setattr(options, field, value)
    with pytest.raises(PlayerError):
        systemd.install(options, tmp_path)
    assert not (tmp_path / "config").exists()


def test_missing_probe_or_wrong_host_do_not_write(options, tmp_path, monkeypatch):
    monkeypatch.setattr(systemd.shutil, "which", lambda *a: None)
    with pytest.raises(PlayerError, match="ffprobe"):
        systemd.install(options, tmp_path)
    monkeypatch.setattr(systemd.os, "geteuid", lambda: 0)
    with pytest.raises(PlayerError, match="never with sudo"):
        systemd.install(options, tmp_path)
    monkeypatch.setattr(systemd.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(systemd.sys, "platform", "darwin")
    with pytest.raises(PlayerError, match="Linux"):
        systemd.install(options, tmp_path)
    assert not (tmp_path / "config").exists()


@pytest.mark.parametrize("path,message", [("python=bad", "equals"), ("dir:bad/python", "colon")])
def test_unsupported_executable_paths_fail_before_write(
    options, tmp_path, monkeypatch, path, message
):
    monkeypatch.setattr(systemd.sys, "executable", str(tmp_path / path))
    with pytest.raises(PlayerError, match=message):
        systemd.install(options, tmp_path)
    assert not (tmp_path / "config").exists()


def test_missing_auth_exits_without_prompt_or_credentials(options, tmp_path):
    unit = systemd.unit_text(options, tmp_path / "runtime")
    command = shlex.split(
        next(line[10:] for line in unit.splitlines() if line.startswith("ExecStart="))
    )
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    assert result.returncode == 1
    assert b"Cannot start bridge" in result.stdout
    assert b"Apple account:" not in result.stdout and b"Password" not in result.stdout
    assert not (tmp_path / "runtime").exists() and not options.auth_file.exists()


def test_systemd_quote_rules():
    assert systemd.quote('a "b" \\ $HOME %h', command=True) == '"a \\"b\\" \\\\ $$HOME %%h"'
    assert systemd.quote("$HOME %h") == '"$HOME %%h"'
    for value in ("new\nline", "tab\t", "null\0", "return\r", "delete\x7f"):
        with pytest.raises(PlayerError, match="control"):
            systemd.quote(value)


def test_generated_unit_passes_real_systemd_parser(options, tmp_path, monkeypatch):
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not installed")
    # env execs Python directly; systemd parses Python as an argument, without a shell.
    python = tmp_path / 'python $literal %h "quoted" \\ path'
    python.symlink_to(sys.executable)
    monkeypatch.setattr(systemd.sys, "executable", str(python))
    options.cache = tmp_path / 'cache $HOME %h "quote" \\ path'
    unit = tmp_path / systemd.UNIT_NAME
    unit.write_text(systemd.unit_text(options, tmp_path))
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    result = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(unit)],
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Unknown" not in result.stderr and "Failed" not in result.stderr


def wait_listen(port, process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert process.poll() is None, process.communicate()[0].decode()
        with socket.socket() as connection:
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    pytest.fail("bridge did not listen")


def test_generated_command_sigterm_releases_runtime_and_restarts(installed, options, tmp_path):
    """Exercise the unit's actual foreground entrypoint, without a running user manager."""
    # Reuse the suite's fake pinned runtime; no Apple account or runtime is contacted.
    for name in (".player-login-ok", "DEV_TOKEN", "MUSIC_TOKEN", "STOREFRONT_ID"):
        (installed.current / "rootfs/data" / name).write_text("synthetic-session")
    auth_file(options.auth_file, create=True)
    root, pin = installed.root, installed.pin
    installed.close()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        options.port = listener.getsockname()[1]
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "sitecustomize.py").write_text(
        "from apple_music_player import runtime, runtime_install\n"
        "from apple_music_player.backend_pins import RuntimePin\n"
        "runtime.host_problem = lambda: ''\n"
        f"runtime_install.RuntimeStore.__init__.__defaults__ = ({pin!r},)\n"
    )
    unit = systemd.unit_text(options, root)
    command = shlex.split(
        next(line[10:] for line in unit.splitlines() if line.startswith("ExecStart="))
    )
    # These fixture paths contain neither systemd specifiers nor variable expansions.
    path_setting = shlex.split(
        next(line[12:] for line in unit.splitlines() if line.startswith("Environment="))
    )[0]
    environment = {
        **os.environ,
        "PATH": path_setting.removeprefix("PATH="),
        "PYTHONPATH": str(fixture) + os.pathsep + str(Path("src").absolute()),
    }
    for _ in range(2):
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        try:
            wait_listen(options.port, process)
            process.send_signal(signal.SIGTERM)
            output = process.communicate(timeout=15)[0]
            assert process.returncode == 0, output.decode()
            assert b"synthetic-" not in output
            # The runtime lock must be available before the next service start.
            with (root / "lock").open("a+") as lock:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert (root / "installed/rootfs/data/MUSIC_TOKEN").read_text() == "synthetic-session"
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
