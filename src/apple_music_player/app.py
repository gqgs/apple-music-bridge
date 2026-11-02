import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .artwork import BADGE_URL, Artwork
from .browsing import AlbumCard, Shelf, placeholder
from .cache import Cache
from .catalog import Catalog, CatalogDeferred, recent_releases
from .controller import Controller
from .library import History
from .model import Album, PlayerError, Track, parse_url
from .playback import Mpv

GIB = 1024**3


def application_icon():
    return QIcon(str(Path(__file__).parent / "assets" / "logo.svg"))


class Window(QMainWindow):
    def __init__(self, root=None):
        super().__init__()
        self.setWindowTitle("Apple Music Player")
        self.setWindowIcon(application_icon())
        self.resize(980, 740)
        custom_root = root is not None
        root = (
            root
            or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "apple-music-player"
        )
        self.cache = Cache(root, 10 * GIB)
        data_root = (
            self.cache.root.parent / (self.cache.root.name + "-library")
            if custom_root
            else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            / "apple-music-player"
            / "library"
        )
        self.history = History(data_root)
        self.artwork = Artwork(self.cache.root / "artwork")
        self.home_revision = None
        self.release_artists = []
        self.release_results = []
        self.release_context = None
        self.release_checked = {}
        self.detail_card = None
        self.now_card = None
        self.now_album = None
        self.settings_path = self.cache.root / "settings.json"
        if self.settings_path.is_symlink():
            raise PlayerError("Settings file must not be a symlink.")
        self.settings = {
            "downloader": "apple-music-downloader",
            "device": "auto",
            "limit": 10,
            "backend": "classic",
            "storefront": "br",
        }
        try:
            saved = json.loads(self.settings_path.read_text())
            for key in self.settings:
                if key in saved and isinstance(saved[key], type(self.settings[key])):
                    self.settings[key] = saved[key]
        except (OSError, ValueError, TypeError):
            pass
        if not re.fullmatch(r"[a-z]{2}", self.settings["storefront"]):
            self.settings["storefront"] = "br"
        self.cache.limit = max(1, min(1024, self.settings["limit"])) * GIB
        self.controller = Controller(
            self.cache, Mpv(), self.settings["downloader"], self.settings["device"]
        )
        self.controller.on_played = self.history.record
        self.controller.backend = "lite" if self.settings.get("backend") == "lite" else "classic"
        self.setup_service = None
        self.closing = False
        self.startup_attempted = False
        self.catalog = Catalog()
        self.catalog_pool = ThreadPoolExecutor(max_workers=1)
        self.catalog_future = None
        self.catalog_request = None
        self.catalog_retries = 0
        self.catalog_timer = QTimer(self)
        self.catalog_timer.setSingleShot(True)
        self.catalog_timer.timeout.connect(self.resume_catalog)
        self.generation = 0
        self.results = []
        self.pending_result_kind = "songs"
        self.pending_album_title = ""
        self.rendered_queue = None
        outer = QWidget()
        self.setCentralWidget(outer)
        layout = QVBoxLayout(outer)
        title_row = QHBoxLayout()
        title = QLabel("Apple Music Player")
        title.setStyleSheet("font-size: 24px; font-weight: 600")
        title_row.addWidget(title)
        title_row.addStretch()
        self.button(title_row, "Backend setup", self.backend_setup)
        self.button(title_row, "Settings", self.edit_settings)
        layout.addLayout(title_row)
        self.backend_status = QLabel()
        self.backend_status.setTextFormat(Qt.PlainText)
        self.backend_status.setWordWrap(True)
        layout.addWidget(self.backend_status)
        root_layout = layout
        self.pages = QTabWidget()
        root_layout.addWidget(self.pages, 1)
        home = QWidget()
        home_layout = QVBoxLayout(home)
        home_title = QLabel("Home · on this device")
        home_title.setStyleSheet("font-size: 24px; font-weight: 600")
        home_layout.addWidget(home_title)
        home_layout.addWidget(QLabel("Recently played albums · this device only"))
        self.recent_shelf = Shelf(self.artwork, self.open_album)
        home_layout.addWidget(self.recent_shelf)
        home_layout.addWidget(QLabel("Recent releases from artists you’ve played"))
        self.release_shelf = Shelf(self.artwork, self.open_album)
        home_layout.addWidget(self.release_shelf)
        self.release_status = QLabel(
            "Last 90 days · limited public catalog; dates may include reissues"
        )
        self.release_status.setWordWrap(True)
        home_layout.addWidget(self.release_status)
        actions = QHBoxLayout()
        self.button(actions, "Refresh releases", self.refresh_releases)
        self.button(actions, "Clear local history", self.clear_history)
        home_layout.addLayout(actions)
        self.pages.addTab(home, "Home")
        search_page = QWidget()
        self.pages.addTab(search_page, "Search")
        layout = QVBoxLayout(search_page)
        catalog_note = QLabel("Search public iTunes metadata · Apple Music availability may differ")
        layout.addWidget(catalog_note)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search songs or artists")
        self.search.textChanged.connect(self.invalidate_search)
        self.search.returnPressed.connect(self.search_catalog)
        search_row.addWidget(self.search, 1)
        self.search_kind = QComboBox()
        self.search_kind.addItem("Songs", "song")
        self.search_kind.addItem("Albums", "album")
        self.search_kind.setCurrentIndex(1)
        self.search_kind.currentIndexChanged.connect(self.invalidate_search)
        search_row.addWidget(self.search_kind)
        search_row.addWidget(QLabel("Storefront"))
        self.country = QLineEdit(self.settings["storefront"])
        self.country.setMaximumWidth(60)
        self.country.setMaxLength(2)
        self.country.setPlaceholderText("e.g. br")
        self.country.setToolTip(
            "Account storefront (two letters). Opening an Apple Music URL adopts its country."
        )
        self.country.textChanged.connect(self.storefront_changed)
        self.country.editingFinished.connect(self.remember_storefront)
        search_row.addWidget(self.country)
        self.button(search_row, "Search", self.search_catalog)
        layout.addLayout(search_row)
        storefront_row = QHBoxLayout()
        self.storefront_notice = QLabel()
        self.storefront_notice.setTextFormat(Qt.PlainText)
        self.storefront_notice.setWordWrap(True)
        self.storefront_notice.setStyleSheet("font-size: 15px; font-weight: 600")
        storefront_row.addWidget(self.storefront_notice, 1)
        self.use_backend_country = QPushButton("Use backend storefront")
        self.use_backend_country.clicked.connect(self.adopt_backend_storefront)
        storefront_row.addWidget(self.use_backend_country)
        layout.addLayout(storefront_row)
        self.catalog_status = QLabel("Search, browse a result's album, or paste a URL below.")
        self.catalog_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.catalog_status)
        self.result_list = QListWidget()
        self.result_list.itemDoubleClicked.connect(lambda _: self.add_selected())
        self.result_list.setIconSize(QSize(100, 100))
        self.result_list.currentRowChanged.connect(self.show_selected_album)
        results_row = QHBoxLayout()
        results_row.addWidget(self.result_list, 1)
        self.detail_host = QVBoxLayout()
        results_row.addLayout(self.detail_host)
        layout.addLayout(results_row, 2)
        result_buttons = QHBoxLayout()
        self.button(result_buttons, "Browse album", self.browse_album)
        self.selected_button = self.button(result_buttons, "Queue selected", self.add_selected)
        self.all_button = self.button(result_buttons, "Queue all results", self.add_all)
        result_buttons.addStretch()
        self.quality = QComboBox()
        self.quality.addItem("Prefer Lossless", "lossless")
        self.quality.addItem("Prefer Dolby Atmos", "atmos")
        result_buttons.addWidget(self.quality)
        layout.addLayout(result_buttons)
        url_row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://music.apple.com/… song or album URL")
        self.url.returnPressed.connect(self.add_url)
        url_row.addWidget(self.url)
        self.button(url_row, "Open URL", self.add_url)
        layout.addLayout(url_row)
        queue_page = QWidget()
        self.pages.addTab(queue_page, "Queue")
        layout = QVBoxLayout(queue_page)
        layout.addWidget(QLabel("Queue · downloads current track and prefetches one next track"))
        self.queue_list = QListWidget()
        layout.addWidget(self.queue_list, 1)
        queue_buttons = QHBoxLayout()
        self.button(queue_buttons, "Remove selected", self.remove_selected)
        self.button(queue_buttons, "Cancel download", self.controller.cancel_download)
        self.button(queue_buttons, "Retry", self.controller.retry)
        queue_buttons.addStretch()
        self.usage = QLabel()
        queue_buttons.addWidget(self.usage)
        layout.addLayout(queue_buttons)
        layout = root_layout
        footer = QHBoxLayout()
        self.now_host = QVBoxLayout()
        footer.addLayout(self.now_host)
        now_text = QVBoxLayout()
        footer.addLayout(now_text, 1)
        layout.addLayout(footer)
        footer_root = layout
        layout = now_text
        self.now = QLabel("Nothing playing")
        self.now.setTextFormat(Qt.PlainText)
        self.now.setStyleSheet("font-size: 18px; font-weight: 600")
        layout.addWidget(self.now)
        self.media_label = QLabel("Media format appears after download verification.")
        self.media_label.setWordWrap(True)
        layout.addWidget(self.media_label)
        self.output_label = QLabel("")
        layout.addWidget(self.output_label)
        self.fallback = QLabel("")
        self.fallback.setStyleSheet("color: #c77b00; font-weight: 600")
        layout.addWidget(self.fallback)
        layout = footer_root
        transport = QHBoxLayout()
        self.pause_button = self.button(transport, "Play / pause", self.toggle_pause)
        self.button(transport, "Next", self.controller.next)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.sliderReleased.connect(self.seek_to)
        transport.addWidget(self.seek, 1)
        self.time_label = QLabel("0:00 / 0:00")
        transport.addWidget(self.time_label)
        layout.addLayout(transport)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #c43d3d")
        layout.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        self.pages.currentChanged.connect(self.navigation_changed)
        self.update_home()
        QTimer.singleShot(0, self.start_saved_backend)

    def navigation_changed(self, index):
        self.invalidate_search()
        self.release_context = None
        if index == 0:
            self.refresh_releases()

    def open_album(self, album):
        if not album:
            return
        self.country.setText(album.country)
        self.pages.setCurrentIndex(1)
        self.request_catalog(self.catalog.album, album.id, album.country, album_title=album.title)

    def show_selected_album(self, row):
        if self.detail_card:
            self.detail_host.removeWidget(self.detail_card)
            self.detail_card.deleteLater()
            self.detail_card = None
        if 0 <= row < len(self.results):
            item = self.results[row]
            album = item if isinstance(item, Album) else item.album
            if album:
                self.detail_card = AlbumCard(album, self.artwork, self.open_album)
                self.detail_host.addWidget(self.detail_card)

    def refresh_artwork(self):
        if self.pages.currentIndex() == 0:
            self.recent_shelf.refresh()
            self.release_shelf.refresh()
        elif self.pages.currentIndex() == 1:
            if self.detail_card:
                self.detail_card.refresh()
            if (
                any(item.artwork_url for item in self.results)
                and not self.artwork.get(BADGE_URL).isNull()
            ):
                for index, item in enumerate(self.results):
                    image = self.artwork.get(item.artwork_url)
                    if not image.isNull() and index < self.result_list.count():
                        self.result_list.item(index).setIcon(QIcon(QPixmap.fromImage(image)))
        if self.now_card:
            self.now_card.refresh()

    def update_home(self):
        context = (self.history.revision, self.country.text().lower())
        if context == self.home_revision:
            return
        self.home_revision = context
        self.recent_shelf.set_albums(
            self.history.albums(context[1]),
            "Play an album track to see it here. History is local to this device/storefront.",
        )
        self.release_shelf.set_albums([], "No recent releases loaded for this storefront.")
        if self.pending_result_kind == "releases":
            self.invalidate_search()
        self.release_context = None
        if self.pages.currentIndex() == 0:
            QTimer.singleShot(0, self.refresh_releases)

    def clear_history(self):
        self.history.clear()
        self.release_checked.clear()
        self.release_context = None
        self.release_artists = []
        if self.pending_result_kind == "releases":
            self.invalidate_search()
        self.update_home()

    def refresh_releases(self):
        if self.closing or self.pages.currentIndex() != 0 or self.release_context:
            return
        country = self.country.text().lower()
        artists = tuple(self.history.artists(country))
        if not artists:
            self.release_shelf.set_albums(
                [], "Play tracks with catalog artist metadata to discover releases."
            )
            return
        key = (country, artists)
        cached = self.release_checked.get(key)
        if cached and time.monotonic() - cached[0] < 3600:
            self.release_shelf.set_albums(cached[1], "No returned releases in the last 90 days.")
            return
        self.release_context = (country, artists, self.history.revision)
        self.release_artists = list(artists)
        self.release_results = []
        self.release_status.setText(
            "Loading recent releases · up to five played artists, with request spacing"
        )
        self.next_release_artist()

    def next_release_artist(self):
        if not self.release_context:
            return
        if self.release_artists:
            self.request_catalog(
                self.catalog.artist_albums,
                self.release_artists[0],
                self.release_context[0],
                result_kind="releases",
            )
        else:
            albums = recent_releases(self.release_results)
            if len(self.release_checked) >= 8:
                self.release_checked.clear()
            self.release_checked[self.release_context[:2]] = (time.monotonic(), albums)
            self.release_shelf.set_albums(albums, "No returned releases in the last 90 days.")
            self.release_status.setText(
                "Last 90 days · limited catalog; dates may include reissues · refreshed hourly"
            )
            self.release_context = None
            self.catalog_request = None

    def receive_releases(self, albums):
        expected = self.release_context
        if (
            not expected
            or self.pages.currentIndex() != 0
            or expected[0] != self.country.text().lower()
            or expected[2] != self.history.revision
        ):
            return
        self.release_results.extend(albums)
        self.release_artists.pop(0)
        self.next_release_artist()

    def ensure_setup_service(self):
        from .setup_ui import SetupService

        if not self.setup_service:
            self.setup_service = SetupService(self)
        self.controller.runtime = self.setup_service.runtime
        return self.setup_service

    def start_saved_backend(self):
        # Exactly once per window startup; never called from the polling loop.
        if self.closing or self.startup_attempted or self.controller.backend != "lite":
            return
        self.startup_attempted = True
        try:
            service = self.ensure_setup_service()
            if service.future or service.runtime.process or service.runtime.cancel.is_set():
                return
            if service.runtime.store.current.exists() and service.runtime.signed_in():
                service.runtime.state = "Starting saved managed service"
                service.dispatch(service.runtime.start)
            else:
                service.runtime.state = "Setup incomplete · open Backend setup"
        except (PlayerError, OSError):
            self.controller.error = (
                "Managed startup failed. Open Backend setup to check its status."
            )

    def button(self, row, text, action):
        button = QPushButton(text)
        button.clicked.connect(action)
        row.addWidget(button)
        return button

    def invalidate_search(self, *_, clear=False):
        self.generation += 1
        self.catalog_timer.stop()
        self.catalog_request = None
        if self.catalog_future:
            self.catalog_future[1].cancel()
            self.catalog_future = None
        if clear:
            self.results = []
            self.result_list.clear()
            self.selected_button.setEnabled(False)
            self.all_button.setEnabled(False)

    def request_catalog(self, operation, *args, result_kind="songs", album_title=""):
        self.invalidate_search()
        self.pending_result_kind = result_kind
        self.pending_album_title = album_title
        self.catalog_request = (operation, args)
        self.catalog_retries = 0
        self.resume_catalog()

    def resume_catalog(self):
        if self.closing or self.catalog_request is None:
            return
        operation, args = self.catalog_request
        self.catalog_future = (self.generation, self.catalog_pool.submit(operation, *args))
        self.catalog_status.setText("Loading catalog…")

    def search_catalog(self):
        self.pages.setCurrentIndex(1)
        if self.search.text().strip():
            self.remember_storefront()
            self.request_catalog(
                self.catalog.search,
                self.search.text(),
                self.country.text().lower(),
                self.search_kind.currentData(),
                result_kind="albums" if self.search_kind.currentData() == "album" else "songs",
            )

    def remember_storefront(self):
        country = self.country.text().lower()
        self.settings["storefront"] = country if re.fullmatch(r"[a-z]{2}", country) else ""
        try:
            self.save_settings()
        except OSError:
            self.controller.error = "Could not save storefront. Check settings permissions."

    def storefront_changed(self):
        self.release_context = None
        self.release_artists = []
        self.home_revision = None
        self.invalidate_search(clear=True)
        self.remember_storefront()
        self.show_storefront()

    def backend_storefront(self):
        runtime = self.controller.runtime
        regions = getattr(runtime, "regions", ()) if self.controller.backend == "lite" else ()
        return regions[0] if len(regions) == 1 else ""

    def adopt_backend_storefront(self):
        country = self.backend_storefront()
        if country:
            self.country.setText(country)
            self.catalog_status.setText("Storefront changed · search again for this catalog.")

    def show_storefront(self):
        country = self.country.text().lower()
        backend = self.backend_storefront()
        mismatch = bool(backend and country != backend)
        self.use_backend_country.setVisible(mismatch)
        self.use_backend_country.setText(f"Use {backend.upper()} storefront")
        if not re.fullmatch(r"[a-z]{2}", country):
            text = "Choose your account storefront before searching."
        else:
            text = f"Browsing {country.upper()}"
        if backend:
            text += f" · Managed backend reports {backend.upper()}"
        if mismatch:
            text += " · Mismatched tracks will not download."
        self.storefront_notice.setText(text)

    def browse_album(self):
        row = self.result_list.currentRow()
        if 0 <= row < len(self.results):
            item = self.results[row]
            album_id = item.id if isinstance(item, Album) else item.album_id
            if album_id:
                self.open_album(item if isinstance(item, Album) else item.album)

    def add_selected(self):
        row = self.result_list.currentRow()
        if 0 <= row < len(self.results):
            if isinstance(self.results[row], Album):
                self.browse_album()
            else:
                self.controller.add(self.results[row], self.quality.currentData())

    def add_all(self):
        for track in self.results:
            if isinstance(track, Track):
                self.controller.add(track, self.quality.currentData())

    def add_url(self):
        try:
            country, kind, item = parse_url(self.url.text())
            changed = self.country.text().lower() != country
            self.country.setText(country)  # Invalidates old results and in-flight requests.
            self.remember_storefront()
            if changed:
                self.catalog_status.setText(
                    f"Storefront changed to {country.upper()} from URL · search again to browse."
                )
            if kind == "album":
                self.request_catalog(self.catalog.album, item, country, album_title="Album tracks")
            else:
                self.controller.add(
                    Track(country, item, f"Song {item}"), self.quality.currentData()
                )
                self.url.clear()
        except PlayerError as exc:
            self.controller.error = str(exc)

    def remove_selected(self):
        self.controller.remove(self.queue_list.currentRow())

    def toggle_pause(self):
        try:
            self.controller.player.command("cycle", "pause")
        except PlayerError as exc:
            self.controller.error = str(exc)

    def seek_to(self):
        try:
            duration = self.controller.player.properties.get("duration") or 0
            self.controller.player.command("seek", duration * self.seek.value() / 1000, "absolute")
        except PlayerError as exc:
            self.controller.error = str(exc)

    def backend_setup(self):
        from .setup_ui import SetupDialog

        try:
            self.ensure_setup_service()
            SetupDialog(self.setup_service, self.controller, self).exec()
            self.settings["backend"] = self.controller.backend
            self.save_settings()
        except PlayerError as exc:
            self.controller.error = str(exc)
        except OSError:
            self.controller.error = "Could not save backend choice. Check settings permissions."

    def save_settings(self):
        temporary = self.settings_path.with_suffix(".tmp")
        if temporary.is_symlink() or self.settings_path.is_symlink():
            raise OSError()
        temporary.write_text(json.dumps(self.settings))
        temporary.replace(self.settings_path)

    def edit_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Player settings")
        form = QFormLayout(dialog)
        downloader = QLineEdit(self.controller.downloader)
        device = QLineEdit(self.controller.device)
        device.setPlaceholderText("auto, or alsa/hdmi:CARD=HDMI,DEV=3")
        limit = QDoubleSpinBox()
        limit.setDecimals(0)
        limit.setRange(1, 1024)
        limit.setValue(self.cache.limit / GIB)
        limit.setSuffix(" GiB")
        form.addRow("Downloader executable", downloader)
        form.addRow("mpv audio device", device)
        form.addRow(
            QLabel(
                "Use mpv --audio-device=help to list devices.\n"
                "Dolby requires an explicit HDMI device; changes apply on next playback."
            )
        )
        form.addRow("Completed media cache", limit)
        form.addRow(
            QLabel(
                "Plus up to 2 GiB staging per download; brief overshoot is possible.\n"
                "The independent wrapper must already be running on ports 10020/20020."
            )
        )
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.Accepted:
            self.controller.downloader = downloader.text().strip()
            self.controller.device = device.text().strip() or "auto"
            self.cache.limit = int(limit.value()) * GIB
            self.settings = {
                "downloader": self.controller.downloader,
                "device": self.controller.device,
                "limit": int(limit.value()),
                "backend": self.controller.backend,
                "storefront": self.settings["storefront"],
            }
            try:
                self.save_settings()
                self.cache.evict()
            except (OSError, PlayerError):
                self.controller.error = (
                    "Could not apply cache limit/settings. Check free space and queued tracks."
                )

    def tick(self):
        self.artwork.poll()
        self.update_home()
        self.refresh_artwork()
        if not self.country.text() and not self.country.hasFocus() and self.backend_storefront():
            self.adopt_backend_storefront()
        self.show_storefront()
        if self.controller.backend == "lite":
            service = self.setup_service
            text = "Managed service not started · open Backend setup"
            if service:
                text = (
                    "Stopping managed service" if service.stop_requested else service.runtime.state
                )
                if service.error:
                    text += " · " + service.error
            self.backend_status.setText(text)
        else:
            self.backend_status.setText("External classic backend")
        if self.catalog_future and self.catalog_future[1].done():
            generation, future = self.catalog_future
            self.catalog_future = None
            if generation == self.generation:
                try:
                    result = future.result()
                    if self.pending_result_kind == "releases":
                        self.receive_releases(result)
                        result = None
                    if result is None:
                        return
                    self.results = result
                    self.result_list.clear()
                    self.result_list.addItems([track.title for track in self.results])
                    for index in range(self.result_list.count()):
                        self.result_list.item(index).setIcon(placeholder())
                    if self.results:
                        self.result_list.setCurrentRow(0)
                    albums = self.pending_result_kind == "albums"
                    self.selected_button.setText("Open album" if albums else "Queue selected")
                    self.selected_button.setEnabled(bool(self.results))
                    self.all_button.setText(
                        "Queue album" if self.pending_album_title else "Queue all results"
                    )
                    self.all_button.setEnabled(bool(self.results) and not albums)
                    label = "albums" if albums else "songs"
                    if len(self.results) == 1:
                        label = label[:-1]
                    action = "open album" if albums else "queue"
                    context = self.pending_album_title
                    self.catalog_status.setText(
                        f"{context + ' · ' if context else ''}{len(self.results)} {label} · "
                        + (
                            f"double-click to {action}"
                            if self.results
                            else "try another query/storefront or paste an Apple Music URL"
                        )
                    )
                except CatalogDeferred as exc:
                    if exc.rate_limited:
                        self.catalog_retries += 1
                    if self.catalog_retries > 2 or exc.seconds > 60:
                        self.catalog_request = None
                        self.catalog_status.setText(
                            "Catalog rate limit persists. Try again later; results are kept."
                        )
                    else:
                        self.catalog_status.setText(
                            "Catalog rate limited · retrying automatically…"
                            if exc.rate_limited
                            else "Waiting for catalog request spacing…"
                        )
                        self.catalog_timer.start(max(1, int(exc.seconds * 1000) + 1))
                except PlayerError as exc:
                    self.catalog_status.setText(str(exc))
                    if self.pending_result_kind == "releases":
                        self.release_status.setText("Recent releases unavailable; try again later.")
                        self.release_context = None
                except Exception:
                    if self.pending_result_kind == "releases":
                        self.release_context = None
                        self.release_status.setText("Recent releases unavailable; try again later.")
                    self.catalog_status.setText(
                        "Catalog unavailable. Check network/storefront or paste a song URL."
                    )
        if self.pending_result_kind == "releases" and self.release_context:
            self.release_status.setText(self.catalog_status.text())
            if self.catalog_request is None:
                self.release_context = None
        try:
            self.controller.tick()
        except Exception:
            self.controller.stopped = True
            self.controller.error = (
                "Cache operation failed. Check permissions/free space and restart the player."
            )
        c = self.controller
        rows = [
            f"{e.state} · {e.track.title} · {e.track.country.upper()} · requested {e.quality}"
            for e in c.queue
        ]
        if rows != self.rendered_queue:
            selected = self.queue_list.currentRow()
            self.queue_list.clear()
            self.queue_list.addItems(rows)
            self.queue_list.setCurrentRow(selected)
            self.rendered_queue = rows
        self.usage.setText(f"Cache {self.cache.usage / GIB:.2f} / {self.cache.limit / GIB:g} GiB")
        self.status.setText(c.error)
        self.now.setText(c.current.track.title if c.current else "Nothing playing")
        album = c.current.track.album if c.current else None
        if album != self.now_album:
            self.now_album = album
            if self.now_card:
                self.now_host.removeWidget(self.now_card)
                self.now_card.deleteLater()
            self.now_card = (
                AlbumCard(album, self.artwork, self.open_album, compact=True) if album else None
            )
            if self.now_card:
                self.now_host.addWidget(self.now_card)
        self.media_label.setText(
            c.media.label if c.media else "Media format appears after download verification."
        )
        self.output_label.setText(c.player.output_label if c.current else "")
        self.fallback.setText(c.media.warning(c.current.quality) if c.current and c.media else "")
        self.pause_button.setEnabled(bool(c.current and c.player.output_verified))
        self.seek.setEnabled(bool(c.current and c.player.output_verified))
        position = c.player.properties.get("time-pos") or 0 if c.current else 0
        duration = c.player.properties.get("duration") or 0 if c.current else 0
        if not self.seek.isSliderDown():
            self.seek.setValue(int(position / duration * 1000) if duration else 0)
        self.time_label.setText(
            f"{int(position) // 60}:{int(position) % 60:02} / "
            f"{int(duration) // 60}:{int(duration) % 60:02}"
        )

    def closeEvent(self, event):
        self.closing = True
        self.invalidate_search()
        self.timer.stop()
        self.catalog_pool.shutdown(wait=False, cancel_futures=True)
        self.controller.close()
        self.artwork.close()
        self.history.close()
        if self.setup_service:
            self.setup_service.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("apple-music-player")
    app.setWindowIcon(application_icon())
    try:
        window = Window()
    except PlayerError as exc:
        QMessageBox.critical(None, "Cannot start player", str(exc))
        return 1
    window.show()
    return app.exec()
