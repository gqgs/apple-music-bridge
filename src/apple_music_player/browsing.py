"""Small native album panels shared by Home, catalog detail and now playing."""

from urllib.parse import urlsplit

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .artwork import BADGE_URL, allowed_url


def store_link(album):
    try:
        p = urlsplit(album.store_url)
        if (
            p.scheme == "https"
            and p.hostname in {"itunes.apple.com", "music.apple.com"}
            and not p.username
            and not p.password
            and p.port in (None, 443)
        ):
            return album.store_url
    except ValueError:
        pass
    return album.url


def placeholder():
    pixmap = QPixmap(100, 100)
    pixmap.fill(QColor("#264653"))
    return QIcon(pixmap)


class AlbumCard(QFrame):
    def __init__(self, album, artwork, opened, compact=False):
        super().__init__()
        self.album, self.artwork = album, artwork
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedWidth(210 if not compact else 190)
        layout = QVBoxLayout(self)
        self.cover = QPushButton()
        self.cover.setIcon(placeholder())
        self.cover.setIconSize(QSize(100, 100))
        self.cover.setFixedHeight(110)
        self.cover.setAccessibleName("Open " + album.title)
        self.cover.clicked.connect(lambda: opened(album))
        layout.addWidget(self.cover)
        self.title = QLabel(album.album_title or album.title)
        self.title.setTextFormat(Qt.PlainText)
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight: 600")
        layout.addWidget(self.title)
        artist = QLabel(album.artist)
        artist.setTextFormat(Qt.PlainText)
        artist.setWordWrap(True)
        layout.addWidget(artist)
        self.store = QPushButton("View album in iTunes")
        self.store.setAccessibleName("View " + album.title + " in iTunes")
        self.store.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(store_link(album))))
        self.store.setMinimumHeight(40)
        layout.addWidget(self.store)
        if compact:
            self.setFixedWidth(145)
            self.cover.setFixedHeight(54)
            self.cover.setIconSize(QSize(48, 48))
            self.title.hide()
            artist.hide()
        self.revision = -1

    def refresh(self):
        # Show fetched artwork only alongside the unmodified approved badge.
        if not allowed_url(self.album.artwork_url):
            return
        badge = self.artwork.get(BADGE_URL)
        if badge.isNull():
            return
        self.store.setText("")
        self.store.setIcon(QIcon(QPixmap.fromImage(badge)))
        self.store.setIconSize(QSize(120, 40))
        image = self.artwork.get(self.album.artwork_url)
        if not image.isNull():
            self.cover.setIcon(QIcon(QPixmap.fromImage(image)))


class Shelf(QWidget):
    def __init__(self, artwork, opened):
        super().__init__()
        self.artwork, self.opened = artwork, opened
        self.cards = []
        outer = QVBoxLayout(self)
        self.note = QLabel()
        self.note.setWordWrap(True)
        outer.addWidget(self.note)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumHeight(260)
        self.content = QWidget()
        self.row = QHBoxLayout(self.content)
        self.row.setAlignment(Qt.AlignLeft)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll)

    def set_albums(self, albums, empty):
        for card in self.cards:
            self.row.removeWidget(card)
            card.deleteLater()
        self.cards = [AlbumCard(album, self.artwork, self.opened) for album in albums[:20]]
        for card in self.cards:
            self.row.addWidget(card)
        self.note.setText(
            empty if not albums else f"{len(albums)} albums · click a cover to open tracks"
        )
        self.scroll.setVisible(bool(albums))

    def refresh(self):
        for card in self.cards:
            card.refresh()
