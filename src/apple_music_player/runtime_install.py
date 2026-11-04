"""Install only explicitly selected, pinned artifacts; never execute during installation."""

import fcntl
import hashlib
import json
import os
import platform
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import HTTPRedirectHandler, build_opener

from .backend_pins import PIN
from .cache import safe_path
from .model import PlayerError

MAX_ARCHIVE = 128 * 1024**2
MAX_EXTRACTED = 512 * 1024**2
REQUIRED = (
    "wrapper-lite-rootless",
    "rootfs/system/bin/lite",
    "rootfs/system/bin/linker64",
    "rootfs/system/lib64/libandroidappmusic.so",
)


class HTTPSOnlyRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise PlayerError("Refused an insecure artifact redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_artifact(url, timeout):
    return build_opener(HTTPSOnlyRedirect()).open(url, timeout=timeout)


def host_problem():
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        return "Managed lite currently requires Linux x86_64."
    if os.geteuid() == 0:
        return "Run the bridge as your normal user, never as root."
    if not hasattr(os, "memfd_create"):
        return "Private memory-backed login is unavailable on this host."
    for name in (
        "/proc/sys/kernel/unprivileged_userns_clone",
        "/proc/sys/user/max_user_namespaces",
    ):
        try:
            if Path(name).read_text().strip() == "0":
                return "User namespaces are disabled. Consult your administrator."
        except OSError:
            pass  # The actual launcher may still report a host-policy denial.
    return ""


def private_dir(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise PlayerError("Runtime paths must not contain symlinks.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise PlayerError("Runtime directory must belong to your user.")
    path.chmod(0o700)
    return path


def cancelled(event):
    if event.is_set():
        raise PlayerError("Setup cancelled. Existing installation was kept.")


def digest(path, cancel=None):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if cancel:
                cancelled(cancel)
            value.update(chunk)
    return value.hexdigest()


def extract_runtime(archive, destination, cancel):
    seen, size = set(), 0
    with zipfile.ZipFile(archive) as package:
        if len(package.infolist()) > 5000:
            raise PlayerError("Runtime archive contains too many entries.")
        for entry in package.infolist():
            cancelled(cancel)
            name = entry.filename
            parts = PurePosixPath(name).parts
            mode = entry.external_attr >> 16
            if (
                not parts
                or name.startswith("/")
                or "\\" in name
                or ":" in name
                or ".." in parts
                or name in seen
                or "\x00" in name
                or stat.S_ISLNK(mode)
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
            ):
                raise PlayerError("Rejected an unsafe runtime archive entry.")
            seen.add(name)
            size += entry.file_size
            if size > MAX_EXTRACTED:
                raise PlayerError("Runtime archive exceeds the extraction allowance.")
            # Do not import account state, the privileged launcher, or unrelated executables.
            allowed = name == "wrapper-lite-rootless" or name.startswith("rootfs/system/")
            if not allowed or entry.is_dir():
                continue
            target = destination.joinpath(*parts)
            safe_path(destination, target)
            directory = destination
            for part in parts[:-1]:
                directory = private_dir(directory / part)
            with package.open(entry) as src, open(target, "xb") as dst:
                os.chmod(target, 0o600)
                copied = 0
                while chunk := src.read(1024 * 1024):
                    cancelled(cancel)
                    copied += len(chunk)
                    if copied > entry.file_size:
                        raise PlayerError("Invalid runtime archive size.")
                    dst.write(chunk)
            if name == "wrapper-lite-rootless" or name.startswith("rootfs/system/bin/"):
                target.chmod(0o700)
    if not all((destination / name).is_file() for name in REQUIRED):
        raise PlayerError("Archive does not contain the pinned native rootless layout.")


class RuntimeStore:
    def __init__(self, root, pin=PIN):
        self.root = private_dir(root)
        self.pin = pin
        lock = safe_path(self.root, self.root / "lock")
        self.lock = open(lock, "a+")
        os.chmod(lock, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise PlayerError("Another process manages this runtime. Close it first.") from None
        self.current = safe_path(self.root, self.root / "installed")
        self.downloader = self.current / "amdl"
        self.launcher = self.current / "wrapper-lite-rootless"
        # Interrupted installation staging has never been active or contained login data.
        for path in self.root.glob("install-*"):
            safe_path(self.root, path)
            if path.is_dir():
                shutil.rmtree(path)

    def verify(self):
        try:
            manifest = safe_path(self.root, self.current / "manifest.json")
            if manifest.stat().st_size > 1_000_000:
                raise ValueError()
            record = json.loads(manifest.read_text())
            if record["wrapper"] != self.pin.wrapper_sha256:
                raise ValueError()
            if record["downloader"] != self.pin.downloader_sha256:
                raise ValueError()
            files = record["files"]
            if not all(name in files for name in (*REQUIRED, "amdl")):
                raise ValueError()
            for name, expected in files.items():
                path = safe_path(self.current, self.current / name)
                if path.stat().st_nlink != 1 or digest(path) != expected:
                    raise ValueError()
            if digest(self.downloader) != self.pin.downloader_sha256:
                raise ValueError()
        except Exception:
            raise PlayerError(
                "Runtime absent or changed. Use apple-music-bridge setup with the pinned archive."
            ) from None

    def install(self, archive, cancel):
        """User selected the wrapper archive and consented to downloader acquisition."""
        archive = Path(archive)
        staging = Path(tempfile.mkdtemp(prefix="install-", dir=self.root))
        try:
            cancelled(cancel)
            if self.current.exists():
                self.verify()
                return  # This pin is already installed; never overwrite account state.
            if (
                archive.is_symlink()
                or not archive.is_file()
                or archive.stat().st_size > MAX_ARCHIVE
            ):
                raise PlayerError("Select the original pinned GitHub Actions ZIP archive.")
            # Copy first, then hash exactly the immutable bytes that will be extracted.
            copied = staging / "wrapper.zip"
            with open(archive, "rb") as src, open(copied, "xb") as dst:
                os.chmod(copied, 0o600)
                total = 0
                while chunk := src.read(1024 * 1024):
                    cancelled(cancel)
                    total += len(chunk)
                    if total > MAX_ARCHIVE:
                        raise PlayerError("Runtime download is too large.")
                    dst.write(chunk)
            if digest(copied, cancel) != self.pin.wrapper_sha256:
                raise PlayerError("Wrapper SHA-256 mismatch. No runtime was installed.")
            package = private_dir(staging / "package")
            extract_runtime(copied, package, cancel)
            # URL is a code pin, never a text field. Default TLS validation stays enabled.
            if not self.pin.downloader_url.startswith("https://github.com/"):
                raise PlayerError("Downloader origin is not approved.")
            with open_artifact(self.pin.downloader_url, timeout=20) as response:
                if not response.url.startswith("https://"):
                    raise PlayerError("Refused an insecure download redirect.")
                with open(package / "amdl", "xb") as target:
                    os.chmod(package / "amdl", 0o600)
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        cancelled(cancel)
                        total += len(chunk)
                        if total > 64 * 1024**2:
                            raise PlayerError("Downloader download is too large.")
                        target.write(chunk)
            if digest(package / "amdl", cancel) != self.pin.downloader_sha256:
                raise PlayerError("Downloader SHA-256 mismatch. No runtime was installed.")
            (package / "amdl").chmod(0o700)
            files = {
                str(p.relative_to(package)): digest(p, cancel)
                for p in package.rglob("*")
                if p.is_file()
            }
            manifest = package / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "wrapper": self.pin.wrapper_sha256,
                        "downloader": self.pin.downloader_sha256,
                        "files": files,
                    }
                )
            )
            manifest.chmod(0o600)
            private_dir(package / "rootfs" / "data")
            cancelled(cancel)
            os.replace(package, self.current)
        except PlayerError:
            raise
        except Exception:
            raise PlayerError(
                "Installation failed. Check network, ZIP integrity and free disk space; retry."
            ) from None
        finally:
            shutil.rmtree(staging)

    def close(self):
        self.lock.close()
