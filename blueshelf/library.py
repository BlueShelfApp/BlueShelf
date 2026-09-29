"""Library model and the thumbnail card delegate used by the grid view."""
from __future__ import annotations

import os
from collections import OrderedDict

from PySide6.QtCore import (QAbstractTableModel, QMimeData, QModelIndex, QRect, QRectF,
                            QSize, Qt)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from blueshelf import paths
from blueshelf.theme import ACCENT, COLORS
from blueshelf.util import fmt_date, fmt_size, fmt_time, res_label

VIDEO_ROLE = Qt.UserRole + 1
# Videos dragged out of the library carry this and nothing else. Deliberately not
# text/uri-list: that is what a file manager understands, and dropping a scene on one
# by accident would start copying tens of gigabytes. Only BlueShelf reads this.
VIDEO_MIME = "application/x-blueshelf-videos"

COLUMNS = [
    ("title", "Title"), ("performer_names", "Performers"), ("studio", "Studio"), ("release_date", "Released"),
    ("duration", "Length"), ("resolution", "Quality"), ("size", "Size"), ("rating", "Rating"),
    ("play_count", "Plays"), ("added_at", "Added"),
]
SORT_FOR_COLUMN = {
    "title": "title", "performer_names": "performers", "studio": "studio", "release_date": "date",
    "duration": "duration", "resolution": "resolution", "size": "size", "rating": "rating",
    "play_count": "plays", "added_at": "added",
}
DEFAULT_ASCENDING = {"title", "performers", "studio"}


def display_title(video: dict) -> str:
    return video.get("title") or os.path.splitext(os.path.basename(video["path"]))[0]


class ThumbCache:
    """Thumbnails loaded from disk, plus scaled copies for the sizes currently on screen."""

    def __init__(self, limit: int = 800):
        self.limit = limit
        self._raw: OrderedDict[str, QPixmap] = OrderedDict()
        self._scaled: OrderedDict[tuple, QPixmap] = OrderedDict()

    def _put(self, store: OrderedDict, key, value) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > self.limit:
            store.popitem(last=False)

    def raw(self, video: dict) -> QPixmap | None:
        name = video.get("thumb")
        if not name:
            return None
        pix = self._raw.get(name)
        if pix is None:
            pix = QPixmap(str(paths.thumbs_dir() / name))
            if pix.isNull():
                return None
            self._put(self._raw, name, pix)
        return pix

    def scaled(self, video: dict, width: int, height: int, dpr: float = 1.0) -> QPixmap | None:
        name = video.get("thumb")
        if not name:
            return None
        key = (name, width, height, dpr)
        pix = self._scaled.get(key)
        if pix is not None:
            self._scaled.move_to_end(key)
            return pix
        raw = self.raw(video)
        if raw is None:
            return None
        pw, ph = max(1, round(width * dpr)), max(1, round(height * dpr))
        s = raw.scaled(pw, ph, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        pix = s.copy((s.width() - pw) // 2, (s.height() - ph) // 2, pw, ph)
        pix.setDevicePixelRatio(dpr)
        self._put(self._scaled, key, pix)
        return pix

    def invalidate(self, video_id: int) -> None:
        name = f"{video_id}.jpg"
        self._raw.pop(name, None)
        for key in [k for k in self._scaled if k[0] == name]:
            del self._scaled[key]


class VideoModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.videos: list[dict] = []
        self._rows: dict[int, int] = {}

    def set_videos(self, videos: list[dict]) -> None:
        self.beginResetModel()
        self.videos = videos
        self._rows = {v["id"]: i for i, v in enumerate(videos)}
        self.endResetModel()

    def flags(self, index):
        base = super().flags(index)
        return base | Qt.ItemIsDragEnabled if index.isValid() else base

    def mimeTypes(self) -> list[str]:
        return [VIDEO_MIME]

    def mimeData(self, indexes) -> QMimeData:
        """The paths of the dragged videos, one per line, each listed once."""
        seen, paths_out = set(), []
        for index in indexes:
            if not index.isValid():
                continue
            path = self.videos[index.row()]["path"]
            if path not in seen:
                seen.add(path)
                paths_out.append(path)
        carried = QMimeData()
        carried.setData(VIDEO_MIME, chr(10).join(paths_out).encode("utf-8"))
        return carried

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.videos)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        video = self.videos[index.row()]
        if role == VIDEO_ROLE:
            return video
        if role == Qt.ToolTipRole:
            return video["path"]
        key = COLUMNS[index.column()][0]
        if role == Qt.TextAlignmentRole and key in ("duration", "size", "play_count"):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role != Qt.DisplayRole:
            return None
        if key == "title":
            return display_title(video)
        if key == "duration":
            return fmt_time(video["duration"]) if video["duration"] else ""
        if key == "resolution":
            label = res_label(video["width"], video["height"], bool(video["is_vr"]))
            return f"{label} VR".strip() if video["is_vr"] else label
        if key == "size":
            return fmt_size(video["size"])
        if key == "rating":
            return "★" * (video["rating"] or 0)
        if key == "added_at":
            return fmt_date(video["added_at"])
        if key == "play_count":
            return str(video["play_count"] or "")
        return video.get(key) or ""

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section][1]
        return None

    def video(self, row: int) -> dict:
        return self.videos[row]

    def row_of(self, video_id: int) -> int | None:
        return self._rows.get(video_id)

    def update_video(self, video: dict) -> None:
        row = self._rows.get(video["id"])
        if row is not None:
            self.videos[row] = video
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def set_thumb(self, video_id: int, name: str) -> None:
        row = self._rows.get(video_id)
        if row is not None:
            self.videos[row]["thumb"] = name
            self.dataChanged.emit(self.index(row, 0), self.index(row, 0))


class CardDelegate(QStyledItemDelegate):
    MARGIN = 6
    TEXT_HEIGHT = 50

    def __init__(self, cache: ThumbCache, parent=None):
        super().__init__(parent)
        self.cache = cache
        self.card_width = 280
        self.title_font = QFont()
        self.title_font.setPointSizeF(10)
        self.title_font.setBold(True)
        self.sub_font = QFont()
        self.sub_font.setPointSizeF(9)
        self.badge_font = QFont()
        self.badge_font.setPointSizeF(8)
        self.badge_font.setBold(True)

    def thumb_height(self) -> int:
        return round(self.card_width * 9 / 16)

    def sizeHint(self, option, index) -> QSize:
        return QSize(self.card_width + 2 * self.MARGIN, self.thumb_height() + self.TEXT_HEIGHT + 2 * self.MARGIN)

    def _badge(self, painter: QPainter, x: float, y: float, text: str, bg: QColor,
               fg: QColor = QColor("white")) -> float:
        fm = QFontMetrics(self.badge_font)
        w, h = fm.horizontalAdvance(text) + 12, fm.height() + 4
        rect = QRectF(x, y, w, h)
        path = QPainterPath()
        path.addRoundedRect(rect, 4, 4)
        painter.fillPath(path, bg)
        painter.setFont(self.badge_font)
        painter.setPen(fg)
        painter.drawText(rect, Qt.AlignCenter, text)
        return w

    def _badge_width(self, text: str) -> int:
        return QFontMetrics(self.badge_font).horizontalAdvance(text) + 12

    def paint(self, painter: QPainter, option, index) -> None:
        video = index.data(VIDEO_ROLE)
        if video is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        card = QPainterPath()
        card.addRoundedRect(QRectF(rect), 8, 8)
        painter.fillPath(card, QColor(COLORS["card_hover"] if hovered or selected else COLORS["card"]))

        thumb = QRect(rect.left(), rect.top(), rect.width(), self.thumb_height())
        painter.save()
        painter.setClipPath(card)
        dpr = painter.device().devicePixelRatioF()
        pix = self.cache.scaled(video, thumb.width(), thumb.height(), dpr)
        if pix is not None:
            painter.drawPixmap(thumb.topLeft(), pix)
        else:
            painter.fillRect(thumb, QColor(COLORS["thumb_bg"]))
            painter.setPen(QColor(COLORS["placeholder"]))
            painter.setFont(self.sub_font)
            painter.drawText(thumb, Qt.AlignCenter, "Making preview…" if video.get("thumb") is None else "No preview")
        resume, duration = video.get("resume_pos") or 0, video.get("duration") or 0
        if resume and duration:
            bar = QRect(thumb.left(), thumb.bottom() - 3, thumb.width(), 4)
            painter.fillRect(bar, QColor(0, 0, 0, 160))
            painter.fillRect(QRect(bar.left(), bar.top(), int(bar.width() * min(1.0, resume / duration)), 4),
                             QColor(ACCENT))
        painter.restore()

        pad = 8
        dark = QColor(0, 0, 0, 175)
        x = thumb.left() + pad
        if video["is_vr"]:
            x += self._badge(painter, x, thumb.top() + pad, "VR", QColor(ACCENT)) + 4
        if video["favorite"]:
            self._badge(painter, x, thumb.top() + pad, "♥", dark, QColor(ACCENT))
        quality = res_label(video.get("width"), video.get("height"), bool(video["is_vr"]))
        if quality:
            self._badge(painter, thumb.right() - pad - self._badge_width(quality), thumb.top() + pad, quality, dark)
        if duration:
            text = fmt_time(duration)
            h = QFontMetrics(self.badge_font).height() + 4
            self._badge(painter, thumb.right() - pad - self._badge_width(text),
                        thumb.bottom() - pad - h - (4 if resume else 0), text, dark)

        text_rect = QRect(rect.left() + pad, thumb.bottom() + 6, rect.width() - 2 * pad, 20)
        stars = "★" * (video["rating"] or 0)
        star_w = 0
        if stars:
            painter.setFont(self.sub_font)
            painter.setPen(QColor(COLORS["star"]))
            star_w = QFontMetrics(self.sub_font).horizontalAdvance(stars) + 6
            painter.drawText(text_rect, Qt.AlignRight | Qt.AlignVCenter, stars)
        painter.setFont(self.title_font)
        painter.setPen(QColor(COLORS["title_text"]))
        title = QFontMetrics(self.title_font).elidedText(display_title(video), Qt.ElideRight,
                                                         text_rect.width() - star_w)
        painter.drawText(text_rect.adjusted(0, 0, -star_w, 0), Qt.AlignLeft | Qt.AlignVCenter, title)

        performers, studio = video.get("performer_names") or "", video.get("studio") or ""
        sub = " · ".join(x for x in (performers, studio) if x)
        sub_rect = QRect(text_rect.left(), text_rect.bottom() + 1, text_rect.width(), 18)
        painter.setFont(self.sub_font)
        painter.setPen(QColor(COLORS["sub_text"]))
        painter.drawText(sub_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(self.sub_font).elidedText(sub, Qt.ElideRight, sub_rect.width()))

        if selected:
            painter.setPen(QPen(QColor(ACCENT), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(rect).adjusted(1, 1, -1, -1), 8, 8)
        painter.restore()
