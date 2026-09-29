"""A strip showing where the action is in a video, and letting it be corrected by hand.

The stretches under this are chosen by how much skin is on screen, which is a guess. A guess
is fine as long as it can be seen and argued with before hours of work are committed to it -
so the reading is drawn, the chosen stretches are drawn over it, and any part of the video can
be marked either way by dragging across it.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from blueshelf.passthrough import Stretch
from blueshelf.util import fmt_time

ACTION = QColor(74, 144, 226, 150)      # the stretches that will be matted
SELECTION = QColor(255, 255, 255, 60)
CURVE = QColor(150, 200, 255)
BASE = QColor(28, 32, 40)
GRID = QColor(90, 100, 115)
TEXT = QColor(200, 208, 220)


class Timeline(QWidget):
    """The whole video from left to right, with the parts worth matting picked out."""

    edited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values: list[float] = []
        self.times: list[float] = []
        self.stretches: list[Stretch] = []
        self.frames = 0
        self.fps = 0.0
        self.threshold = 0.10
        self._from: float | None = None      # a drag in progress, as a fraction across
        self._to: float | None = None
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.CrossCursor)
        self.setToolTip("Drag across a part of the video, then mark it as action or as talk.")

    # ------------------------------------------------------------ what it is showing

    def show_reading(self, reading: dict, stretches: list[Stretch]) -> None:
        self.times = list(reading.get("times") or [])
        self.values = list(reading.get("values") or [])
        self.fps = float(reading.get("fps") or 0.0)
        self.frames = int(reading.get("frames") or 0)
        self.stretches = list(stretches)
        self.update()

    def set_stretches(self, stretches: list[Stretch]) -> None:
        self.stretches = list(stretches)
        self.update()
        self.edited.emit()

    def has_reading(self) -> bool:
        return bool(self.values) and self.frames > 0

    # ------------------------------------------------------------ marking by hand

    def _frame_at(self, x: float) -> int:
        share = min(1.0, max(0.0, x / max(1.0, float(self.width()))))
        return max(1, min(self.frames, int(round(share * self.frames)) + 1))

    def mousePressEvent(self, event) -> None:
        if not self.has_reading():
            return
        self._from = self._to = float(event.position().x())
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._from is None:
            return
        self._to = float(event.position().x())
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._from is None:
            return
        self._to = float(event.position().x())
        if abs(self._to - self._from) < 3:      # a click, not a drag
            self._from = self._to = None
        self.update()

    def selection(self) -> tuple[int, int] | None:
        """The frames currently dragged over, if any."""
        if self._from is None or self._to is None:
            return None
        a, b = sorted((self._frame_at(self._from), self._frame_at(self._to)))
        return (a, b) if b > a else None

    def clear_selection(self) -> None:
        self._from = self._to = None
        self.update()

    def mark(self, action: bool) -> None:
        """Make the dragged part matted, or not."""
        span = self.selection()
        if span is None:
            return
        start, end = span
        kept: list[Stretch] = []
        for stretch in self.stretches:
            if stretch.end < start or stretch.start > end:
                kept.append(stretch)
                continue
            # Overlapping: keep whatever falls outside the selection.
            if stretch.start < start:
                kept.append(Stretch(stretch.start, start - 1))
            if stretch.end > end:
                kept.append(Stretch(end + 1, stretch.end))
        if action:
            kept.append(Stretch(start, end))
        kept.sort(key=lambda s: s.start)
        merged: list[Stretch] = []
        for stretch in kept:
            if merged and stretch.start <= merged[-1].end + 1:
                merged[-1] = Stretch(merged[-1].start, max(merged[-1].end, stretch.end))
            else:
                merged.append(stretch)
        self.clear_selection()
        self.set_stretches(merged)

    def matted_share(self) -> float:
        if not self.frames:
            return 0.0
        return min(1.0, sum(s.length for s in self.stretches) / float(self.frames))

    def summary(self) -> str:
        if not self.has_reading():
            return ""
        if not self.stretches:
            return "Nothing marked as action - the whole video would be left alone."
        total = sum(s.length for s in self.stretches)
        skipped = self.frames - total
        return (f"{len(self.stretches)} stretches, {self.matted_share():.0%} of the video - "
                f"{fmt_time(total / self.fps)} matted, "
                f"{fmt_time(skipped / self.fps)} skipped.")

    # ------------------------------------------------------------ drawing it

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = float(self.width()), float(self.height())
        painter.fillRect(self.rect(), BASE)

        if not self.has_reading():
            painter.setPen(QPen(TEXT))
            painter.drawText(self.rect(), Qt.AlignCenter,
                             "Scan the video to see where the action is")
            return

        # The stretches that will be matted, behind everything else.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(ACTION))
        for stretch in self.stretches:
            left = (stretch.start - 1) / self.frames * width
            right = stretch.end / self.frames * width
            painter.drawRect(QRectF(left, 0, max(1.0, right - left), height - 14))

        # How much skin, over time.
        painter.setPen(QPen(CURVE, 1.0))
        floor = height - 14
        step = width / max(1, len(self.values))
        top = max(0.25, max(self.values))
        for index, value in enumerate(self.values):
            x = index * step
            tall = (value / top) * (floor - 4)
            painter.drawLine(QRectF(x, floor - tall, 1, tall).topLeft(),
                             QRectF(x, floor, 1, 0).topLeft())

        # Where the threshold sits, so the slider has something to mean.
        line = floor - (self.threshold / top) * (floor - 4)
        painter.setPen(QPen(GRID, 1.0, Qt.DashLine))
        painter.drawLine(QRectF(0, line, width, 0).topLeft(),
                         QRectF(width, line, 0, 0).topLeft())

        span = self.selection()
        if span is not None:
            left = (span[0] - 1) / self.frames * width
            right = span[1] / self.frames * width
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(SELECTION))
            painter.drawRect(QRectF(left, 0, max(1.0, right - left), floor))

        # Minutes along the bottom, enough of them to place a stretch but not so many they blur.
        painter.setPen(QPen(TEXT))
        font = painter.font()
        font.setPointSizeF(7.5)
        painter.setFont(font)
        seconds = self.frames / self.fps if self.fps else 0
        if seconds > 0:
            every = 300 if seconds > 2400 else 120 if seconds > 600 else 60
            mark = 0
            while mark <= seconds:
                x = mark / seconds * width
                painter.drawLine(QRectF(x, floor, 0, 4).topLeft(),
                                 QRectF(x, floor + 4, 0, 0).topLeft())
                painter.drawText(QRectF(x + 2, floor + 1, 60, 13), Qt.AlignLeft,
                                 fmt_time(mark))
                mark += every
