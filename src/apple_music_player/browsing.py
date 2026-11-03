"""Small native album panels shared by Home, catalog detail and now playing."""

from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
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


def set_artwork_icon(target, image):
    """Change decoration only when its source image changes, not on every poll."""
    key = image.cacheKey() if not image.isNull() else 0
    role = Qt.UserRole + 10
    is_item = not isinstance(target, QWidget)
    previous = target.data(role) if is_item else getattr(target, "_artwork_key", None)
    if previous == key:
        return
    if is_item:
        target.setData(role, key)
    else:
        target._artwork_key = key
    target.setIcon(QIcon(QPixmap.fromImage(image)) if key else placeholder())


def set_artwork_pixmap(target, image):
    key = image.cacheKey() if not image.isNull() else 0
    if getattr(target, "_artwork_key", None) == key:
        return
    target._artwork_key = key
    target.setPixmap(
        QPixmap.fromImage(image).scaled(100, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if key
        else placeholder().pixmap(100, 100)
    )


class TrackRowDelegate(QStyledItemDelegate):
    """Native selection with a separate right-aligned text duration column."""

    duration_role = Qt.UserRole + 1
    duration_alignment = Qt.AlignRight | Qt.AlignVCenter

    @staticmethod
    def duration_rect(rect, metrics):
        width = metrics.horizontalAdvance("00:00:00") + 12
        return QRect(rect.right() - width - 8, rect.top(), width, rect.height())

    def paint(self, painter, option, index):
        duration = index.data(self.duration_role)
        if duration is None:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        right = self.duration_rect(opt.rect, opt.fontMetrics)
        left = opt.rect.adjusted(4, 0, -(right.width() + 24), 0)
        painter.save()
        painter.setClipRect(opt.rect)
        painter.setFont(opt.font)
        role = QPalette.HighlightedText if opt.state & QStyle.State_Selected else QPalette.Text
        painter.setPen(opt.palette.color(role))
        painter.drawText(right, self.duration_alignment, duration)
        painter.drawText(
            left,
            Qt.AlignLeft | Qt.AlignVCenter,
            opt.fontMetrics.elidedText(text, Qt.ElideRight, max(0, left.width())),
        )
        painter.restore()


class AlbumCard(QFrame):
    focused = Signal(object)

    def __init__(self, album, artwork, opened, compact=False):
        super().__init__()
        self.album, self.artwork = album, artwork
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedWidth(210 if not compact else 190)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        self.cover = QPushButton()
        set_artwork_icon(self.cover, QImage())
        self.cover.setIconSize(QSize(100, 100))
        self.cover.setFixedHeight(110)
        self.cover.setAccessibleName("Open " + album.title)
        self.cover.installEventFilter(self)
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
        if compact:
            self.setFixedWidth(145)
            self.cover.setFixedHeight(54)
            self.cover.setIconSize(QSize(48, 48))
            self.title.hide()
            artist.hide()
        self.revision = -1

    def eventFilter(self, watched, event):
        if watched is self.cover and event.type() in (QEvent.Enter, QEvent.FocusIn):
            self.focused.emit(self.album)
        return super().eventFilter(watched, event)

    def refresh(self):
        # The window supplies one shared official badge beside the visible artwork view.
        if not allowed_url(self.album.artwork_url):
            return
        badge = self.artwork.get(BADGE_URL)
        if badge.isNull():
            set_artwork_icon(self.cover, QImage())
            return
        image = self.artwork.get(self.album.artwork_url)
        if not image.isNull():
            set_artwork_icon(self.cover, image)


class AlbumHeader(QWidget):
    """One album identity above its compact track list; no per-track artwork."""

    def __init__(self, artwork):
        super().__init__()
        self.artwork, self.album = artwork, None
        row = QHBoxLayout(self)
        self.cover = QLabel()
        self.cover.setFixedSize(100, 100)
        set_artwork_pixmap(self.cover, QImage())
        row.addWidget(self.cover, 0, Qt.AlignTop)
        text = QVBoxLayout()
        self.title = QLabel()
        self.title.setTextFormat(Qt.PlainText)
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 20px; font-weight: 600")
        self.artist = QLabel()
        self.artist.setTextFormat(Qt.PlainText)
        self.details = QLabel()
        self.details.setTextFormat(Qt.PlainText)
        text.addWidget(self.title)
        text.addWidget(self.artist)
        text.addWidget(self.details)
        self.actions = QHBoxLayout()
        text.addLayout(self.actions)
        row.addLayout(text, 1)
        self.hide()

    def set_album(self, album, count):
        self.album = album
        self.setVisible(album is not None)
        if album:
            self.title.setText(album.album_title or album.title.removesuffix(" — " + album.artist))
            self.artist.setText(album.artist)
            self.details.setText(
                " · ".join(x for x in (album.release_date[:10], f"{count} tracks") if x)
            )
            set_artwork_pixmap(self.cover, QImage())

    def refresh(self):
        if not self.album or not allowed_url(self.album.artwork_url):
            return
        if self.artwork.get(BADGE_URL).isNull():
            set_artwork_pixmap(self.cover, QImage())
            return
        image = self.artwork.get(self.album.artwork_url)
        if not image.isNull():
            set_artwork_pixmap(self.cover, image)


class Shelf(QWidget):
    focused = Signal(object)

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
        self.scroll.setMinimumHeight(220)
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
            card.focused.connect(self.focused.emit)
            self.row.addWidget(card)
        self.note.setText(
            empty if not albums else f"{len(albums)} albums · click a cover to open tracks"
        )
        self.scroll.setVisible(bool(albums))

    def refresh(self):
        for card in self.cards:
            card.refresh()
