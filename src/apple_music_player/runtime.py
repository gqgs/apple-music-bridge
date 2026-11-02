"""Owned rootless process and local login state; no raw diagnostics cross this boundary."""

import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from .backend_pins import LITE_ENDPOINT
from .cache import safe_path
from .downloader import stop_group
from .model import PlayerError
from .runtime_install import host_problem, private_dir


class RuntimeEvents:
    def __init__(self):
        self.two_factor = threading.Event()
        self.login_complete = threading.Event()
        self.denied = threading.Event()

    def drain(self, stream):
        pending = b""
        dropping = False
        try:
            while chunk := os.read(stream.fileno(), 4096):
                for part in chunk.splitlines(keepends=True):
                    if not dropping:
                        pending += part
                        if len(pending) > 8192:
                            pending = b""
                            dropping = True
                    if part.endswith(b"\n"):
                        if not dropping:
                            self.accept(pending)
                        pending, dropping = b"", False
        except OSError:
            pass
        finally:
            stream.close()

    def accept(self, line):
        # Source-defined literal messages only. Never retain or display arbitrary text.
        if line.strip() in {
            b"unshare: Operation not permitted",
            b"mount proc failed: Operation not permitted",
            b"mount /dev/urandom failed: Operation not permitted",
        }:
            self.denied.set()
        match = re.fullmatch(
            rb"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \[(INFO |WARN )\] (.*)\n", line
        )
        if match:
            if match[2] == b"Enter your 2FA code into /data/2fa.txt":
                self.two_factor.set()
            elif match[2] == b"login complete, exiting":
                self.login_complete.set()


def memory_args(arguments):
    """The inherited descriptor is readable inside the launcher's mounted /proc."""
    if not hasattr(os, "memfd_create"):
        raise PlayerError("Memory-backed authentication is unavailable; no password was saved.")
    fd = os.memfd_create("player-login", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(("\n".join(arguments) + "\n").encode())
        os.lseek(fd, 0, os.SEEK_SET)
        # Linux UAPI constants (linux/fcntl.h); some Python builds omit their names.
        fcntl.fcntl(fd, getattr(fcntl, "F_ADD_SEALS", 1033), 0x000F)
        return fd
    except Exception:
        os.close(fd)
        raise PlayerError(
            "Could not prepare private authentication; no password was saved."
        ) from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def health_status():
    # Deliberately local: never send unauthenticated wrapper requests through an HTTP proxy.
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(LITE_ENDPOINT + "/status", timeout=0.5) as response:
            if response.url != LITE_ENDPOINT + "/status":
                return False
            raw = response.read(4097)
        if len(raw) > 4096:
            return False
        data = json.loads(raw)
        return data.get("code") == 0 and isinstance(data.get("data", {}).get("regions"), list)
    except Exception:
        return False


class ManagedRuntime:
    """Methods are serialized by the setup worker; cancel is a thread-safe event."""

    def __init__(self, store):
        self.store = store
        self.process = None
        self.reader = None
        self.events = RuntimeEvents()
        self.cancel = threading.Event()
        self.mode = ""
        self.state = "Not started" if store.current.exists() else "Not installed"
        self.ready = False
        self.args_fd = None
        self.fifo_fd = None
        self.code_ready = threading.Event()
        self.started = 0.0
        self.last_healthy = 0.0
        self.data = store.current / "rootfs" / "data"
        self.backup = store.current / "rootfs" / "data-before-login"
        self.transaction = store.current / "login-pending"
        self.fifo = self.data / "2fa.txt"
        self.recover_login()

    def recover_login(self):
        if self.transaction.exists():
            self.restore_login()
        elif self.backup.exists():
            # A successful login committed before old-state cleanup was interrupted.
            safe_path(self.store.root, self.backup)
            shutil.rmtree(self.backup)
        if self.data.exists():
            self.clean_fifo()

    def clean_fifo(self):
        self.code_ready.clear()
        if self.fifo_fd is not None:
            os.close(self.fifo_fd)
            self.fifo_fd = None
        safe_path(self.store.root, self.fifo)
        self.fifo.unlink(missing_ok=True)

    def signed_in(self):
        try:
            for name in (".player-login-ok", "DEV_TOKEN", "MUSIC_TOKEN", "STOREFRONT_ID"):
                path = safe_path(self.store.root, self.data / name)
                if not path.is_file() or not 0 < path.stat().st_size < 65536:
                    return False
            return True
        except (PlayerError, OSError):
            return False

    def _preflight(self):
        if self.cancel.is_set():
            raise PlayerError("Setup cancelled.")
        problem = host_problem()
        if problem:
            raise PlayerError(problem)
        self.store.verify()
        if self.cancel.is_set():
            raise PlayerError("Setup cancelled.")
        if self.process:
            raise PlayerError("The managed backend is already running. Stop it before retrying.")
        with socket.socket() as test:
            try:
                test.bind(("127.0.0.1", 12340))
            except OSError:
                raise PlayerError(
                    "Port 12340 is busy. Stop its owner yourself; the player will not kill it."
                ) from None

    def _spawn(self, arguments, pass_fds=()):
        env = os.environ.copy()
        env.pop("LITE_ARGS_FILE", None)
        if pass_fds:
            env["LITE_ARGS_FILE"] = f"/proc/self/fd/{pass_fds[0]}"
        self.events = RuntimeEvents()
        fds = (self.store.lock.fileno(), *pass_fds)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "apple_music_player.runtime_child",
                str(os.getpid()),
                str(fds[0]),
                str(pass_fds[0] if pass_fds else -1),
                str(self.store.launcher),
                *arguments,
            ],
            cwd=self.store.current,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            pass_fds=fds,
            start_new_session=True,
            umask=0o077,
        )
        self.reader = threading.Thread(
            target=self.events.drain, args=(self.process.stderr,), daemon=True
        )
        self.reader.start()
        self.started = self.last_healthy = time.monotonic()

    def start(self):
        try:
            self._preflight()
            self.mode = "service"
            self._spawn(
                [
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "12340",
                    "--base-dir",
                    "/data",
                    "--log-level",
                    "error",
                ]
            )
            self.state = "Starting rootless backend"
        except PlayerError:
            raise
        except Exception:
            self.stop()
            raise PlayerError(
                "Cannot start the rootless backend. Check its installation and host policy."
            ) from None

    def login(self, username, password):
        # The upstream file parser trims whitespace; auth.cpp has a 64-byte 2FA buffer.
        if (
            not username
            or not password
            or ":" in username
            or len(password.encode()) > 56
            or any(c in username + password for c in "\r\n\x00")
            or username != username.strip()
            or password != password.strip()
        ):
            raise PlayerError(
                "Credentials cannot be empty or contain outer whitespace/newlines. "
                "This wrapper limits passwords to 56 UTF-8 bytes."
            )
        self.stop()
        self._preflight()
        try:
            safe_path(self.store.root, self.transaction)
            self.transaction.write_text("had-data" if self.data.exists() else "fresh")
            self.transaction.chmod(0o600)
            if self.data.exists():
                safe_path(self.store.root, self.data)
                os.replace(self.data, self.backup)
            private_dir(self.data)
            self.args_fd = memory_args(
                [
                    "--login",
                    username + ":" + password,
                    "--base-dir",
                    "/data",
                    "--code-from-file",
                    "--log-level",
                    "info",
                ]
            )
            self.mode = "login"
            self._spawn(["--base-dir", "/data"], (self.args_fd,))
            self.state = "Signing in locally"
        except Exception:
            self.stop()
            raise PlayerError(
                "Could not start private login. Previous session was restored."
            ) from None

    @property
    def can_submit_code(self):
        process = self.process
        return (
            self.code_ready.is_set()
            and self.mode == "login"
            and not self.cancel.is_set()
            and process is not None
            and process.poll() is None
        )

    def submit_code(self, code):
        if not self.can_submit_code:
            raise PlayerError("No verification code is currently requested.")
        if not re.fullmatch(r"[0-9]{6}", code):
            raise PlayerError("Enter the six-digit verification code locally.")
        safe_path(self.store.root, self.fifo)
        if self.cancel.is_set():
            self.stop()
            return
        if self.fifo_fd is None:
            raise PlayerError("The verification channel is no longer waiting. Cancel and retry.")
        self.code_ready.clear()
        try:
            os.write(self.fifo_fd, (code + "\n").encode())
        except OSError:
            raise PlayerError(
                "The wrapper did not accept the code. Cancel login and retry."
            ) from None
        self.state = "Verifying code locally"

    def poll(self):
        if self.cancel.is_set():
            self.stop()
            self.state = "Cancelled; previous session kept"
            return
        if not self.process:
            return
        if self.events.denied.is_set():
            self.stop()
            self.state = "Host denied namespaces/mounts. Use classic or consult your administrator."
            return
        if self.process.poll() is not None:
            if self.reader:
                self.reader.join(timeout=0.5)
            success = self.mode == "login" and self.events.login_complete.is_set()
            if success:
                for name in ("DEV_TOKEN", "MUSIC_TOKEN", "STOREFRONT_ID"):
                    p = safe_path(self.store.root, self.data / name)
                    success = success and p.is_file() and 0 < p.stat().st_size < 65536
            if success:
                receipt = self.data / ".player-login-ok"
                receipt.write_text("Upstream login-complete observed; tokens may expire.")
                receipt.chmod(0o600)
                self.transaction.unlink()
                self.clean_fifo()
                self._stop_process()
                self.mode = ""
                if self.backup.exists():
                    shutil.rmtree(self.backup)
                self.state = "Signed in locally; Start to check service readiness"
            else:
                self.stop()
                self.state = (
                    "Host denied namespaces/mounts. Use classic or consult your administrator."
                    if self.events.denied.is_set()
                    else "Backend exited or login failed. Check host support/account and retry."
                )
            return
        if self.mode == "login":
            if self.events.two_factor.is_set() and self.fifo_fd is None:
                self.clean_fifo()
                os.mkfifo(self.fifo, 0o600)
                # Upstream file_exists() uses fopen/fclose before the final fscanf.
                # Holding both ends open lets those probes complete without consuming
                # the code and keeps it buffered until the actual credential read.
                self.fifo_fd = os.open(self.fifo, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
                self.state = "Verification code required (within the overall login deadline)"
                # Publish readiness only after both FIFO endpoints are open.
                self.code_ready.set()
            if time.monotonic() - self.started > 180:
                self.stop()
                self.state = "Login timed out; previous session restored. Retry when ready."
        else:
            healthy = health_status()
            self.ready = healthy and self.signed_in()
            if healthy:
                self.last_healthy = time.monotonic()
                self.state = (
                    "Ready · locally signed in; account validity checked on download"
                    if self.ready
                    else "Running · authentication not verified; sign in locally"
                )
            else:
                self.state = "Starting or waiting for backend health"
                if time.monotonic() - self.last_healthy > 20:
                    self.stop()
                    self.state = (
                        "Backend health failed. Check host namespace/mount support, then retry."
                    )

    def _stop_process(self):
        self.code_ready.clear()
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            stop_group(self.process)
            self.process = None
        if self.reader:
            self.reader.join(timeout=1)
            self.reader = None
        if self.args_fd is not None:
            os.close(self.args_fd)
            self.args_fd = None
        self.ready = False

    def restore_login(self):
        if self.transaction.exists():
            if self.backup.exists():
                safe_path(self.store.root, self.data)
                if self.data.exists():
                    shutil.rmtree(self.data)
                safe_path(self.store.root, self.backup)
                os.replace(self.backup, self.data)
            elif self.data.exists() and self.transaction.read_text() == "fresh":
                # Fresh installation: failed login material is never kept.
                safe_path(self.store.root, self.data)
                shutil.rmtree(self.data)
                private_dir(self.data)
            safe_path(self.store.root, self.transaction).unlink()

    def stop(self):
        self._stop_process()
        self.restore_login()
        if self.data.exists():
            self.clean_fifo()
        self.mode = ""
        self.state = "Stopped"

    def close(self):
        self.stop()
        self.store.close()
