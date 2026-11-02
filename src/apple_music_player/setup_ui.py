import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
)

from .backend_pins import LITE_ARTIFACT_EXPIRES, LITE_ARTIFACT_NAME, LITE_ORIGIN, PIN
from .model import PlayerError
from .runtime import ManagedRuntime
from .runtime_install import RuntimeStore, host_problem


class SetupService(QObject):
    def __init__(self, parent=None, root=None):
        super().__init__(parent)
        root = (
            root
            or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            / "apple-music-player"
        )
        self.runtime = ManagedRuntime(RuntimeStore(root))
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.active_kind = ""
        self.stop_requested = False
        self.error = ""
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(500)

    @property
    def busy(self):
        # A queued/running user action is busy; periodic observation is not.
        return self.stop_requested or (self.future is not None and self.active_kind != "poll")

    def finish_completed(self):
        if self.future and self.future.done():
            try:
                self.future.result()
            except PlayerError as exc:
                self.error = str(exc)
            except Exception:
                self.error = "Backend operation failed. Cancel and retry; diagnostics are withheld."
            if self.active_kind == "stop":
                self.stop_requested = False
            if self.active_kind == "start":
                self.runtime.starting = False
            self.future = None
            self.active_kind = ""

    def dispatch(self, operation, *args):
        self.finish_completed()
        if self.busy:
            self.error = "Setup is busy. Wait for the current step or Cancel."
            return False
        previous_poll = self.future
        self.error = ""
        self.active_kind = operation.__name__
        if self.active_kind == "start":
            self.runtime.starting = True
        if self.active_kind in {"install", "start", "login"}:
            self.runtime.cancel.clear()

        def perform():
            # The single worker serializes actions after an in-flight poll. Observe
            # its failure too; never discard a poll exception or submit concurrently.
            if previous_poll:
                previous_poll.result()
            return operation(*args)

        self.future = self.pool.submit(perform)
        return True

    def tick(self):
        self.finish_completed()
        if self.stop_requested:
            if not self.future:
                self.active_kind = "stop"
                self.future = self.pool.submit(self.runtime.stop)
            return
        if not self.future and self.runtime.process:
            self.active_kind = "poll"
            self.future = self.pool.submit(self.runtime.poll)

    def request_stop(self):
        # Cancellation is immediate, but cleanup is a durable serialized action.
        # Start must not clear the cancellation flag while an old poll/start ends.
        self.stop_requested = True
        self.runtime.cancel.set()
        self.runtime.ready = False
        self.runtime.state = "Stopping managed service"
        self.tick()

    def install(self, archive):
        if self.runtime.process:
            raise PlayerError("Stop the managed backend before installing.")
        self.runtime.store.install(archive, self.runtime.cancel)
        self.runtime.state = "Installed and verified; sign in or start explicitly"

    def close(self):
        self.timer.stop()
        self.runtime.cancel.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.runtime.close()


class SetupDialog(QDialog):
    def __init__(self, service, controller, parent=None):
        super().__init__(parent)
        self.service, self.controller = service, controller
        self.setWindowTitle("Backend setup")
        self.resize(680, 560)
        form = QFormLayout(self)
        self.backend = QComboBox()
        self.backend.addItem("External classic — existing setup", "classic")
        self.backend.addItem("Managed rootless lite — Linux x86_64", "lite")
        self.backend.setCurrentIndex(1 if controller.backend == "lite" else 0)
        self.backend.currentIndexChanged.connect(self.select_backend)
        form.addRow("Playback backend", self.backend)
        missing = [x for x in ("mpv", "ffprobe", "ffmpeg", "MP4Box") if not shutil.which(x)]
        deps = "Missing: " + ", ".join(missing) if missing else "Media dependencies found"
        self.requirements = QLabel(
            deps
            + "\n"
            + (
                host_problem()
                or "Host precheck passed; namespace/mount policy is checked on start."
            )
        )
        self.requirements.setWordWrap(True)
        form.addRow(self.requirements)
        origin = QLabel(
            "1. Download the pinned third-party native ZIP from its GitHub Actions run.\n"
            f"Artifact: {LITE_ARTIFACT_NAME}; expires {LITE_ARTIFACT_EXPIRES}.\n"
            "GitHub may require browser sign-in. No GitHub token is requested by this app.\n"
            "Wrapper is MIT; bundled Apple/Android assets have separate provenance."
        )
        origin.setWordWrap(True)
        form.addRow(origin)
        open_origin = QPushButton("Open pinned upstream artifact run")
        open_origin.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(LITE_ORIGIN)))
        form.addRow(open_origin)
        self.consent = QCheckBox(
            "I choose to install this runtime and download amdl v0.1.4 from GitHub"
        )
        form.addRow(self.consent)
        self.install_button = QPushButton("Select ZIP, verify SHA-256 and install")
        self.install_button.clicked.connect(self.install)
        form.addRow(self.install_button)
        checksum = QLabel("Wrapper SHA-256: " + PIN.wrapper_sha256)
        checksum.setWordWrap(True)
        form.addRow(checksum)
        form.addRow(
            QLabel(
                "2. Sign in locally. Password stays in memory.\n"
                "Session files are private, not encrypted. "
                "No password is saved or passed on the command line. No raw logs are shown."
            )
        )
        self.username = QLineEdit()
        self.username.setPlaceholderText("Apple account")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("Password (upstream limit: 56 UTF-8 bytes)")
        form.addRow("Account", self.username)
        form.addRow("Password", self.password)
        self.login_button = QPushButton("Sign in using installed runtime")
        self.login_button.clicked.connect(self.login)
        form.addRow(self.login_button)
        self.code = QLineEdit()
        self.code.setMaxLength(6)
        self.code.setEchoMode(QLineEdit.Password)
        self.code.setPlaceholderText("Six-digit code when requested")
        self.code.textChanged.connect(self.refresh)
        code_row = QHBoxLayout()
        code_row.addWidget(self.code)
        self.code_button = QPushButton("Submit code")
        self.code_button.clicked.connect(self.submit_code)
        code_row.addWidget(self.code_button)
        form.addRow(code_row)
        controls = QHBoxLayout()
        self.start_button = QPushButton("3. Start / retry managed service")
        self.start_button.clicked.connect(lambda: service.dispatch(service.runtime.start))
        controls.addWidget(self.start_button)
        cancel = QPushButton("Cancel / stop managed service")
        cancel.clicked.connect(self.cancel)
        controls.addWidget(cancel)
        form.addRow(controls)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        form.addRow(self.status)
        done = QPushButton("Done")
        done.clicked.connect(self.accept)
        form.addRow(done)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(200)
        self.refresh()

    def select_backend(self):
        if self.controller.future:
            self.service.error = "Cancel or finish the active download before switching backends."
            self.backend.blockSignals(True)
            self.backend.setCurrentIndex(1 if self.controller.backend == "lite" else 0)
            self.backend.blockSignals(False)
            return
        self.controller.backend = self.backend.currentData()
        self.controller.runtime = self.service.runtime

    def install(self):
        if not self.consent.isChecked():
            self.service.error = (
                "Choose installation explicitly before selecting the pinned archive."
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Pinned wrapper artifact", "", "ZIP archive (*.zip)"
        )
        if path:
            self.service.dispatch(self.service.install, path)

    def login(self):
        if self.controller.backend == "lite" and self.controller.future:
            self.service.error = "Cancel or finish the lite download before signing in again."
            return
        if self.service.busy:
            self.service.error = "Setup is busy; wait before signing in."
            return
        username, password = self.username.text(), self.password.text()
        self.username.setText("")
        self.password.setText("")
        self.service.dispatch(self.service.runtime.login, username, password)

    def submit_code(self):
        code = self.code.text()
        if not self.service.runtime.can_submit_code or not re.fullmatch(r"[0-9]{6}", code):
            self.service.error = "Wait for the verification prompt and enter six digits locally."
            self.refresh()
            return
        if self.service.dispatch(self.service.runtime.submit_code, code):
            self.code.setText("")
        self.refresh()

    def cancel(self):
        if self.controller.backend == "lite":
            self.controller.cancel_download()
        self.password.setText("")
        self.username.setText("")
        self.code.setText("")
        self.service.request_stop()

    def refresh(self):
        r = self.service.runtime
        self.status.setText(r.state + ("\n" + self.service.error if self.service.error else ""))
        busy = self.service.busy
        self.install_button.setEnabled(not busy and not r.process)
        installed = r.store.current.exists()
        self.login_button.setEnabled(installed and not busy and r.mode != "login")
        self.start_button.setEnabled(installed and not busy and not r.process)
        self.code_button.setEnabled(
            not busy and r.can_submit_code and bool(re.fullmatch(r"[0-9]{6}", self.code.text()))
        )

    def done(self, result):
        # Closing during login cancels it; a completed service remains app-owned until quit.
        self.password.setText("")
        self.username.setText("")
        self.code.setText("")
        if self.service.runtime.mode == "login" or self.service.active_kind in {"install", "login"}:
            self.service.runtime.cancel.set()
        self.timer.stop()
        super().done(result)
