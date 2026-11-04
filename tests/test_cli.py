import importlib.util
import os
import pty
import select
import subprocess
import sys
import termios
import time

import pytest
from test_runtime import installed as _installed
from test_runtime import package as _package

from apple_music_player import cli
from apple_music_player.model import PlayerError
from apple_music_player.runtime import ManagedRuntime

package = _package
installed = _installed


def test_no_qt_and_compatibility_entrypoints():
    assert importlib.util.find_spec("PySide6") is None, "Run this suite in a Qt-free environment"
    for module in ("apple_music_player.cli", "apple_music_player.bridge"):
        result = subprocess.run(
            [sys.executable, "-m", module, "--help"], capture_output=True, timeout=5
        )
        assert result.returncode == 0
    import apple_music_player.runtime_child  # noqa: F401


def test_existing_runtime_paths_preserved_and_ambiguity(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert cli.data_root() == tmp_path / "apple-music-bridge"
    old = tmp_path / "apple-music-player" / "installed"
    old.mkdir(parents=True)
    receipt = old / ".player-login-ok"
    receipt.write_text("untouched")
    assert cli.data_root() == old.parent
    (tmp_path / "apple-music-bridge/installed").mkdir(parents=True)
    with pytest.raises(PlayerError, match="Two runtime"):
        cli.data_root()
    assert receipt.read_text() == "untouched"


def test_setup_requires_consent_before_paths_or_network(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    assert cli.main(["setup", "--archive", "none.zip", "--runtime-root", str(root)]) == 1
    assert not root.exists()


def test_cli_login_real_fake_process_two_factor_and_cleanup(installed, monkeypatch, capsys):
    runtime = ManagedRuntime(installed)
    answers = iter(["fixture@example.test", "fixture-secret", "123456"])
    ticks = []

    def prompt(message, *, hidden=False, tick=None):
        if tick:
            # Waiting for user input must continue polling the runtime.
            tick()
            ticks.append(True)
        return next(answers)

    monkeypatch.setattr(cli, "terminal_input", prompt)
    cli.login(runtime)
    assert ticks and runtime.signed_in()
    assert runtime.process is None and runtime.args_fd is None and runtime.fifo_fd is None
    assert not runtime.transaction.exists() and not runtime.fifo.exists()
    output = capsys.readouterr()
    assert "fixture-secret" not in output.out + output.err
    assert "123456" not in output.out + output.err


@pytest.mark.parametrize("failure", [KeyboardInterrupt, PlayerError])
def test_cli_cancel_code_restores_previous_session(installed, monkeypatch, failure):
    data = installed.current / "rootfs/data"
    (data / "MUSIC_TOKEN").write_text("previous-session")
    runtime = ManagedRuntime(installed)
    answers = iter(["fixture@example.test", "fixture-secret"])

    def prompt(message, *, hidden=False, tick=None):
        if tick:
            raise failure("cancelled")
        return next(answers)

    monkeypatch.setattr(cli, "terminal_input", prompt)
    with pytest.raises(failure):
        cli.login(runtime)
    assert (data / "MUSIC_TOKEN").read_text() == "previous-session"
    assert runtime.process is None and runtime.args_fd is None and runtime.fifo_fd is None
    assert not runtime.transaction.exists()


def test_code_wait_observes_runtime_timeout(installed, monkeypatch):
    runtime = ManagedRuntime(installed)
    answers = iter(["fixture@example.test", "fixture-secret"])

    def prompt(message, *, hidden=False, tick=None):
        if tick:
            runtime.started -= 181
            tick()
        return next(answers)

    monkeypatch.setattr(cli, "terminal_input", prompt)
    with pytest.raises(PlayerError, match="timed out"):
        cli.login(runtime)
    assert runtime.process is None and not runtime.transaction.exists()


@pytest.mark.parametrize("value", ["synthetic-secret", "paß🔒"])
def test_terminal_no_echo_polling_and_restore(value):
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    program = """from apple_music_player.cli import terminal_input
value = terminal_input("Hidden: ", hidden=True)
assert value in ("synthetic-secret", "paß🔒")
print("accepted")
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program], stdin=slave, stdout=slave, stderr=slave
    )
    output = b""
    try:
        deadline = time.monotonic() + 5
        while b"Hidden: " not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 4096)
        assert b"Hidden: " in output
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        os.write(master, (value + "\n").encode())
        process.wait(timeout=5)
        while select.select([master], [], [], 0)[0]:
            output += os.read(master, 4096)
        assert process.returncode == 0 and b"accepted" in output
        assert value.encode() not in output
        assert termios.tcgetattr(slave) == original
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)


def test_no_secret_stdin_pipe():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from apple_music_player.cli import terminal_input; terminal_input('')",
        ],
        input=b"secret\n",
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0 and b"local interactive terminal" in result.stderr
    assert b"secret" not in result.stdout


def test_serve_options_keep_existing_auth_cache_and_quality(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "serve", lambda options: calls.append(options) or 0)
    auth = tmp_path / "credentials"
    auth.write_text("untouched")
    assert (
        cli.main(
            [
                "serve",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--auth-file",
                str(auth),
                "--cache",
                str(tmp_path / "cache"),
                "--country",
                "jp",
                "--quality",
                "atmos",
            ]
        )
        == 0
    )
    assert calls[0][calls[0].index("--quality") + 1] == "atmos"
    assert calls[0][calls[0].index("--country") + 1] == "jp"
    assert auth.read_text() == "untouched"


def test_status_busy_is_non_mutating(installed, monkeypatch, capsys):
    monkeypatch.setattr(cli, "health_status", lambda: True)
    assert cli.main(["status", "--runtime-root", str(installed.root)]) == 0
    assert "owned by another process" in capsys.readouterr().out
    assert not (installed.current / "login-pending").exists()


def test_status_absent_does_not_create_runtime(tmp_path):
    root = tmp_path / "absent"
    assert cli.main(["status", "--runtime-root", str(root)]) == 1
    assert not root.exists()


def test_setup_keeps_existing_client_credentials(installed, package, tmp_path, monkeypatch):
    from apple_music_player.bridge.http import auth_file
    from apple_music_player.runtime_install import RuntimeStore

    archive, pin = package
    root = installed.root
    installed.close()
    monkeypatch.setattr(cli, "RuntimeStore", lambda path: RuntimeStore(path, pin))
    monkeypatch.setattr(cli, "host_problem", lambda: "")
    auth = tmp_path / "private" / "auth.json"
    auth.parent.mkdir(mode=0o700)
    auth_file(auth, create=True)
    original = auth.read_bytes()
    assert (
        cli.main(
            [
                "setup",
                "--archive",
                str(archive),
                "--accept-runtime",
                "--runtime-root",
                str(root),
                "--auth-file",
                str(auth),
            ]
        )
        == 0
    )
    assert auth.read_bytes() == original
    assert auth.stat().st_mode & 0o777 == 0o600


def test_hidden_terminal_restores_echo_when_poll_aborts():
    master, slave = pty.openpty()
    previous = termios.tcgetattr(slave)
    program = """from apple_music_player.cli import terminal_input
from apple_music_player.model import PlayerError
def abort():
    raise PlayerError("timeout")
try:
    terminal_input("Hidden: ", hidden=True, tick=abort)
except PlayerError:
    pass
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", program], stdin=slave, stdout=slave, stderr=slave, timeout=5
        )
        assert result.returncode == 0
        assert termios.tcgetattr(slave) == previous
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("entered", [b"123\x04", b"\x04"])
def test_partial_canonical_eof_keeps_login_deadline_polling(entered):
    master, slave = pty.openpty()
    previous = termios.tcgetattr(slave)
    program = """from apple_music_player.cli import terminal_input
from apple_music_player.model import PlayerError
import time
end = time.monotonic() + 0.5
def tick():
    if time.monotonic() >= end:
        raise PlayerError("deadline")
try:
    terminal_input("Hidden: ", hidden=True, tick=tick)
except PlayerError as exc:
    print("deadline observed" if str(exc) == "deadline" else "cancelled")
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program], stdin=slave, stdout=slave, stderr=slave
    )
    output = b""
    try:
        deadline = time.monotonic() + 5
        while b"Hidden: " not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 4096)
        assert b"Hidden: " in output
        os.write(master, entered)
        process.wait(timeout=3)
        while select.select([master], [], [], 0)[0]:
            output += os.read(master, 4096)
        assert process.returncode == 0
        assert (b"deadline observed" if entered.startswith(b"123") else b"cancelled") in output
        assert b"123" not in output
        assert termios.tcgetattr(slave) == previous
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)
