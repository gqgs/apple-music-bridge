"""Local terminal setup and authentication; the HTTP API never accepts Apple credentials."""

import argparse
import os
import select
import signal
import sys
import termios
import threading
import time
from pathlib import Path

from .backend_pins import LITE_ARTIFACT_EXPIRES, LITE_ORIGIN
from .bridge.__main__ import main as serve
from .bridge.http import auth_file
from .model import PlayerError
from .runtime import ManagedRuntime, health_status, session_saved
from .runtime_install import RuntimeStore, host_problem, private_dir


def data_root():
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    legacy = base / "apple-music-player"
    current = base / "apple-music-bridge"
    if (legacy / "installed").exists():
        if (current / "installed").exists():
            raise PlayerError("Two runtime installations found. Select --runtime-root explicitly.")
        return legacy
    return current


def default_auth():
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "apple-music-bridge"
        / "auth.json"
    )


def terminal_input(prompt, *, hidden=False, tick=None):
    """Canonical TTY input with continued runtime polling and guaranteed echo restoration."""
    if not sys.stdin.isatty():
        raise PlayerError(
            "Login requires a local interactive terminal; credentials are not read from pipes."
        )
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        changed = termios.tcgetattr(fd)
        changed[3] |= termios.ICANON | termios.ISIG
        if hidden:
            changed[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSAFLUSH, changed)
        print(prompt, end="", flush=True)
        pending = bytearray()
        while True:
            if tick:
                tick()
            if select.select([fd], [], [], 0.1)[0]:
                # A canonical Ctrl-D can deliver a partial line. TextIO.readline()
                # would then block waiting for more and prevent runtime polling.
                chunk = os.read(fd, 4096)
                if not chunk:
                    raise PlayerError("Login cancelled.")
                pending.extend(chunk)
                if len(pending) > 4096:
                    raise PlayerError("Login input is too long.")
                if b"\n" in pending:
                    return bytes(pending).split(b"\n", 1)[0].rstrip(b"\r").decode("utf-8")
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, previous)
        if hidden:
            print()


def login(runtime):
    username = terminal_input("Apple account: ")
    password = terminal_input("Password (hidden): ", hidden=True)
    try:
        runtime.login(username, password)
    finally:
        password = username = None  # Minimize retention; Python cannot promise memory erasure.
    try:

        def tick():
            runtime.poll()
            if not runtime.process:
                raise PlayerError(runtime.state)

        while runtime.process:
            runtime.poll()
            if runtime.can_submit_code:
                code = terminal_input("Verification code (hidden): ", hidden=True, tick=tick)
                try:
                    runtime.submit_code(code)
                finally:
                    code = None
            time.sleep(0.1)
        if not runtime.signed_in() or not runtime.state.startswith("Signed in locally"):
            raise PlayerError(runtime.state)
        print("Signed in locally. Media access is verified on download.")
    finally:
        runtime.stop()  # Roll back unfinished login and close memfd/FIFO on every exit.


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Apple Music OpenSubsonic bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "login", "status", "serve"):
        command = sub.add_parser(name)
        command.add_argument("--runtime-root", type=Path)
        command.add_argument("--auth-file", type=Path, default=default_auth())
        if name == "setup":
            command.add_argument("--archive", type=Path, required=True)
            command.add_argument(
                "--accept-runtime",
                action="store_true",
                help="Consent to third-party runtime installation and pinned amdl download",
            )
        if name == "serve":
            command.add_argument("--cache", type=Path)
            command.add_argument("--cache-gib", type=int, default=10)
            command.add_argument("--country", default="br")
            command.add_argument(
                "--quality",
                choices=("lossless", "atmos"),
                default="lossless",
                help="Preferred format: atmos falls back to lossless, then AAC; lossless to AAC",
            )
            command.add_argument("--port", type=int, default=4533)
            command.add_argument("--diagnostics", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    store = runtime = None
    try:
        root = args.runtime_root if args.runtime_root is not None else data_root()
        if args.command == "serve":
            options = [
                "--auth-file",
                str(args.auth_file),
                "--start-runtime",
                str(root),
                "--cache-gib",
                str(args.cache_gib),
                "--country",
                args.country,
                "--quality",
                args.quality,
                "--port",
                str(args.port),
            ]
            if args.cache:
                options += ["--cache", str(args.cache)]
            if args.diagnostics:
                options += ["--diagnostics"]
            return serve(options)
        if args.command == "setup" and not args.accept_runtime:
            print(
                f"Runtime source: {LITE_ORIGIN}; recorded artifact expiry: {LITE_ARTIFACT_EXPIRES}."
            )
            raise PlayerError("Review the runtime source, then explicitly pass --accept-runtime.")
        if args.command in {"setup", "login"}:
            problem = host_problem()
            if problem:
                raise PlayerError(problem)
        if args.command == "status" and not (root / "installed").exists():
            print("Runtime not installed. Run apple-music-bridge setup.")
            return 1
        try:
            store = RuntimeStore(root)
        except PlayerError as exc:
            if args.command == "status" and str(exc).startswith("Another"):
                print("Runtime owned by another process; session not inspected or modified.")
                print("Local service " + ("reachable" if health_status() else "not reachable"))
                return 0
            raise
        if args.command == "setup":
            store.install(args.archive, threading.Event())
            private_dir(args.auth_file.absolute().parent)
            auth_file(args.auth_file, create=not args.auth_file.exists())
            print(
                "Runtime installed; client credentials kept locally. Next: apple-music-bridge login"
            )
        elif args.command == "login":
            store.verify()
            runtime = ManagedRuntime(store)
            login(runtime)
        else:
            store.verify()
            print(
                "Runtime verified; local session "
                + ("saved" if session_saved(store) else "missing")
            )
            print("Local service " + ("reachable" if health_status() else "not reachable"))
            print("Account/media access is only verified on download.")
        return 0
    except KeyboardInterrupt:
        print("Cancelled; existing session preserved.")
        return 130
    except PlayerError as exc:
        print(str(exc), file=sys.stderr)  # Only fixed, application-owned messages.
        return 1
    except (OSError, ValueError):
        print(
            "Operation failed. Check private paths, permissions and runtime setup.", file=sys.stderr
        )
        return 1
    finally:
        if runtime:
            runtime.stop()
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
