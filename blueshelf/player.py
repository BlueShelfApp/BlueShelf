"""Embedded mpv player with MPC-style controls and a flat view for VR videos."""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QColor, QCursor, QFontMetrics, QKeyEvent, QPainter
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QSlider,
                               QStyle, QToolButton, QToolTip, QVBoxLayout, QWidget)

from blueshelf import paths
from blueshelf.db import Database
from blueshelf.library import display_title
from blueshelf.theme import ACCENT
from blueshelf.parser import LAYOUT_MONO, LAYOUT_SBS, LAYOUT_TB, PROJ_EQUI180, PROJ_EQUI360, PROJ_FISHEYE
from blueshelf.theme import draw_icon
from blueshelf.util import VR_CROP, VR_FLAT, VR_OFF, fmt_time

paths.prepare_mpv_dll()
import mpv  # noqa: E402  (needs the DLL path set up first)

VR_MODE_LABELS = {VR_OFF: "Show whole frame", VR_FLAT: "Flat view (look around)", VR_CROP: "One eye, no reprojection"}
LAYOUT_LABELS = {LAYOUT_SBS: "Side by side", LAYOUT_TB: "Top / bottom", LAYOUT_MONO: "Mono"}
PROJECTION_CHOICES = [("180° equirectangular", PROJ_EQUI180, 190.0), ("360° equirectangular", PROJ_EQUI360, 190.0),
                      ("Fisheye 190°", PROJ_FISHEYE, 190.0), ("Fisheye 200°", PROJ_FISHEYE, 200.0),
                      ("Fisheye 220°", PROJ_FISHEYE, 220.0)]
DEFAULT_FOV = 100.0
AUTO_DEVICE = "auto"


def _device_list(instance) -> list[tuple[str, str]]:
    devices = []
    for entry in instance.audio_device_list or []:
        name = entry.get("name") or ""
        if name and name != AUTO_DEVICE:
            devices.append((name, entry.get("description") or name))
    return devices


def audio_devices() -> list[tuple[str, str]]:
    """(name, description) for the audio outputs mpv can play to, without needing a player open."""
    instance = mpv.MPV(vo="null", idle=True)
    try:
        return _device_list(instance)
    finally:
        instance.terminate()


class VideoSurface(QWidget):
    """Native child window mpv renders into. mpv passes mouse input through to it."""

    clicked = Signal()
    double_clicked = Signal()
    dragged = Signal(int, int)
    wheel = Signal(int)
    mouse_moved = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet("background: black;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._press: QPoint | None = None
        self._last: QPoint | None = None
        self._moved = False
        self._click_timer = QTimer(self, singleShot=True)
        self._click_timer.timeout.connect(self.clicked)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press = self._last = event.position().toPoint()
            self._moved = False

    def mouseMoveEvent(self, event):
        self.mouse_moved.emit()
        pos = event.position().toPoint()
        if self._press is not None and event.buttons() & Qt.LeftButton:
            if (pos - self._press).manhattanLength() > 4:
                self._moved = True
            delta = pos - self._last
            self._last = pos
            if self._moved and (delta.x() or delta.y()):
                self.dragged.emit(delta.x(), delta.y())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press is not None:
            if not self._moved:
                self._click_timer.start(QApplication.doubleClickInterval())
            self._press = None

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._click_timer.stop()
            self.double_clicked.emit()

    def wheelEvent(self, event):
        self.wheel.emit(event.angleDelta().y())


class SeekSlider(QSlider):
    seek_requested = Signal(float, bool)  # seconds, exact

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setRange(0, 0)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.dragging = False
        self.sliderPressed.connect(self._pressed)
        self.sliderReleased.connect(self._released)
        self.sliderMoved.connect(lambda v: self.seek_requested.emit(v / 10, False))

    def set_duration(self, duration: float) -> None:
        self.setRange(0, max(0, int(duration * 10)))

    # Scenes marked in this film, drawn as bands along the bottom of the bar, and the one being
    # marked now, from where it was started to where the film is.
    scenes: list[tuple[float, float]] = []
    marking: tuple[float, float] | None = None

    def set_scenes(self, scenes: list[tuple[float, float]], marking: tuple[float, float] | None = None) -> None:
        self.scenes, self.marking = list(scenes), marking
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.maximum() <= 0 or not (self.scenes or self.marking):
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        total, width = self.maximum() / 10, self.width()
        bands = [(start, end, QColor("#7a8a5e")) for start, end in self.scenes]
        if self.marking:
            colour = QColor(ACCENT)
            colour.setAlpha(150)
            bands.append((min(self.marking), max(self.marking), colour))
        for start, end, colour in bands:
            x0, x1 = width * start / total, width * end / total
            painter.setBrush(colour)
            painter.drawRoundedRect(int(x0), self.height() - 5, max(3, int(x1 - x0)), 4, 2, 2)
        painter.end()

    def _pressed(self):
        self.dragging = True

    def _released(self):
        self.dragging = False
        self.seek_requested.emit(self.value() / 10, True)

    def _value_at(self, x: float) -> int:
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(x), self.width())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.maximum() > 0:
            value = self._value_at(event.position().x())
            self.setValue(value)
            self.seek_requested.emit(value / 10, True)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.maximum() > 0:
            QToolTip.showText(event.globalPosition().toPoint(), fmt_time(self._value_at(event.position().x()) / 10),
                              self)
        super().mouseMoveEvent(event)


class PlayerWidget(QWidget):
    mute_changed = Signal(bool)  # so the library screen's mute button keeps up
    back_requested = Signal()
    next_requested = Signal()
    prev_requested = Signal()
    delete_requested = Signal(int)
    rate_requested = Signal(int, int)
    favorite_requested = Signal(int, bool)
    cleanup_keep = Signal()
    cleanup_delete = Signal()
    cleanup_undo = Signal()
    cleanup_finish = Signal()
    fullscreen_requested = Signal()
    video_changed = Signal(int)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.mpv: mpv.MPV | None = None
        self.video: dict | None = None
        self.fullscreen = False
        self.vr_mode = VR_OFF
        self.eye = 0
        self.yaw = self.pitch = 0.0
        self.fov = DEFAULT_FOV
        self.layout = LAYOUT_SBS
        self.proj = PROJ_EQUI180
        self.lens = 190.0
        self._crop_pending = False
        self._view_dirty = False
        self._since_resume_save = 0
        self._eof_seen = False
        self._full_title = ""
        self._full_subtitle = ""
        self.cleanup = False
        self.marking: float | None = None     # where a scene being marked started

        self.setFocusPolicy(Qt.StrongFocus)
        self.surface = VideoSurface(self)
        self.surface.clicked.connect(self.toggle_pause)
        self.surface.double_clicked.connect(self.fullscreen_requested)
        self.surface.dragged.connect(self._drag)
        self.surface.wheel.connect(self._wheel)
        self.surface.mouse_moved.connect(self._reveal_controls)

        self._icons = {name: draw_icon(name) for name in
                       ("back", "prev", "play", "pause", "next", "volume", "muted", "fullscreen", "exit_fullscreen",
                        "close", "rewind", "forward", "trash", "heart_outline",
                        "chevron_left", "chevron_right")}
        self._icons["heart"] = draw_icon("heart", color=ACCENT)
        self.header = QWidget(self)
        self.header.setObjectName("playerHeader")
        self._build_header()
        self.controls = QWidget(self)
        self.controls.setObjectName("playerControls")
        self._build_controls()
        self.cleanup_bar = QWidget(self)
        self.cleanup_bar.setObjectName("playerControls")
        self.cleanup_bar.hide()
        self._build_cleanup_bar()

        # mpv draws into its own native window, which Qt widgets cannot float on top of, so the
        # previous/next arrows sit in slim columns either side of the video instead of over it.
        self.prev_arrow = self._side_arrow("chevron_left", "Previous video (Page Up or Alt+←)",
                                           self.prev_requested)
        self.next_arrow = self._side_arrow("chevron_right", "Next video (Page Down or Alt+→)",
                                           self.next_requested)
        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        middle.addWidget(self.prev_arrow)
        middle.addWidget(self.surface, 1)
        middle.addWidget(self.next_arrow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addLayout(middle, 1)
        layout.addWidget(self.cleanup_bar)
        layout.addWidget(self.controls)

        self._tick = QTimer(self, interval=250)
        self._tick.timeout.connect(self._update_ui)
        self._view_timer = QTimer(self, interval=16)
        self._view_timer.timeout.connect(self._flush_view)
        self._hide_timer = QTimer(self, singleShot=True, interval=2500)
        self._hide_timer.timeout.connect(self._hide_controls)

    # ------------------------------------------------------------ construction

    def refresh_icons(self) -> None:
        """Redraw the control icons after the light/dark theme changed."""
        self._icons = {name: draw_icon(name) for name in self._icons}
        self._icons["heart"] = draw_icon("heart", color=ACCENT)
        paused = self.mpv.pause if self.mpv is not None else True
        muted = self.mpv.mute if self.mpv is not None else False
        favorite = bool(self.video and self.video.get("favorite"))
        for button, name in ((self.back_btn, "back"), (self.prev_btn, "prev"), (self.rewind_btn, "rewind"),
                             (self.play_btn, "play" if paused else "pause"), (self.forward_btn, "forward"),
                             (self.next_btn, "next"), (self.mute_btn, "muted" if muted else "volume"),
                             (self.fav_btn, "heart" if favorite else "heart_outline"),
                             (self.delete_btn, "trash"),
                             (self.fs_btn, "exit_fullscreen" if self.fullscreen else "fullscreen"),
                             (self.prev_arrow, "chevron_left"), (self.next_arrow, "chevron_right")):
            button.setIcon(self._icons[name])

    def _tool(self, icon: str | None, tip: str, slot, text: str = "") -> QToolButton:
        b = QToolButton(self.controls)
        if icon is not None:
            b.setIcon(self._icons[icon])
            b.setIconSize(QSize(20, 20))
        if text:
            b.setText(text)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(slot)
        return b

    def _side_arrow(self, icon: str, tip: str, slot) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("sideArrow")
        button.setIcon(self._icons[icon])
        button.setIconSize(QSize(26, 26))
        button.setFixedWidth(46)
        button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        button.setToolTip(tip)
        button.setCursor(Qt.PointingHandCursor)
        button.setFocusPolicy(Qt.NoFocus)
        button.clicked.connect(slot)
        return button

    def _build_cleanup_bar(self) -> None:
        """The row shown while going through the library deciding what to keep."""
        row = QHBoxLayout(self.cleanup_bar)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(10)
        self.cleanup_progress = QLabel(objectName="muted")
        keep = QPushButton("Keep  (K)", objectName="primary")
        keep.clicked.connect(self.cleanup_keep)
        delete = QPushButton("Delete  (D)", objectName="danger")
        delete.clicked.connect(self.cleanup_delete)
        self.cleanup_undo_btn = QPushButton("Undo last delete  (U)")
        self.cleanup_undo_btn.setEnabled(False)
        self.cleanup_undo_btn.clicked.connect(self.cleanup_undo)
        finish = QPushButton("Finish  (Esc)")
        finish.clicked.connect(self.cleanup_finish)
        for widget in (self.cleanup_progress, keep, delete, self.cleanup_undo_btn):
            widget.setFocusPolicy(Qt.NoFocus)
        finish.setFocusPolicy(Qt.NoFocus)
        row.addWidget(self.cleanup_progress, 1)
        row.addWidget(keep)
        row.addWidget(delete)
        row.addWidget(self.cleanup_undo_btn)
        row.addWidget(finish)

    def set_cleanup(self, on: bool) -> None:
        self.cleanup = on
        self.cleanup_bar.setVisible(on)
        if on:
            self.setFocus()

    def set_cleanup_progress(self, text: str, can_undo: bool = False) -> None:
        self.cleanup_progress.setText(text)
        self.cleanup_undo_btn.setEnabled(can_undo)

    def _build_header(self) -> None:
        row = QHBoxLayout(self.header)
        row.setContentsMargins(14, 6, 8, 6)
        row.setSpacing(10)
        self.title_label = QLabel(self.header, objectName="playerTitle")
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.subtitle_label = QLabel(self.header, objectName="muted")
        self.subtitle_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.queue_label = QLabel(self.header, objectName="muted")
        close_btn = QToolButton(self.header)
        close_btn.setIcon(self._icons["close"])
        close_btn.setIconSize(QSize(20, 20))
        close_btn.setToolTip("Close and go back to the library (Esc)")
        close_btn.setAutoRaise(True)
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.clicked.connect(self.back_requested)
        row.addWidget(self.title_label, 3)
        row.addWidget(self.subtitle_label, 2)
        row.addStretch()
        row.addWidget(self.queue_label)
        row.addWidget(close_btn)

    def _build_controls(self) -> None:
        row = QHBoxLayout(self.controls)
        row.setContentsMargins(8, 4, 8, 6)
        row.setSpacing(4)
        self.back_btn = self._tool("back", "Back to library (Esc)", self.back_requested, "Library")
        self.back_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.prev_btn = self._tool("prev", "Previous video (Page Up)", self.prev_requested)
        self.rewind_btn = self._tool("rewind", "Back 10 seconds", lambda: self.seek_relative(-10))
        self.play_btn = self._tool("play", "Play / pause (Space)", self.toggle_pause)
        self.forward_btn = self._tool("forward", "Forward 10 seconds", lambda: self.seek_relative(10))
        self.next_btn = self._tool("next", "Next video (Page Down)", self.next_requested)
        self.seek = SeekSlider(self.controls)
        self.seek.seek_requested.connect(self._seek_to)
        self.time_label = QLabel("0:00 / 0:00", self.controls)
        self.time_label.setMinimumWidth(110)
        self.time_label.setAlignment(Qt.AlignCenter)
        self.mute_btn = self._tool("volume", "Mute (M). Right-click to choose the audio device.", self.toggle_mute)
        self.mute_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.mute_btn.customContextMenuRequested.connect(lambda _pos: self._audio_menu())
        self.volume = QSlider(Qt.Horizontal, self.controls)
        self.volume.setRange(0, 130)
        self.volume.setValue(int(self.db.get_setting("volume", 80)))
        self.volume.setFixedWidth(90)
        self.volume.setFocusPolicy(Qt.NoFocus)
        self.volume.valueChanged.connect(self._set_volume)
        self.speed_btn = self._tool(None, "Playback speed ([ and ])", self._speed_menu, "1.0×")
        self.vr_btn = self._tool(None, "VR options (V cycles mode, E swaps eye, R resets view)",
                                 self._vr_menu, "VR")
        self.mark_btn = self._tool(None, "Mark a scene: press where it starts, then where it ends (T). "
                                   "Right-click for the scenes marked in this film.", self.mark_scene, "Mark scene")
        self.mark_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.mark_btn.customContextMenuRequested.connect(lambda _pos: self._scenes_menu())
        self.fav_btn = self._tool("heart_outline", "Favorite", self.toggle_favorite)
        self.delete_btn = self._tool("trash", "Delete this video (Delete)", self._request_delete)
        self.fs_btn = self._tool("fullscreen", "Fullscreen (F / double-click)", self.fullscreen_requested)
        for w in (self.back_btn, self.prev_btn, self.rewind_btn, self.play_btn, self.forward_btn, self.next_btn):
            row.addWidget(w)
        row.addWidget(self.seek, 1)
        for w in (self.time_label, self.mute_btn, self.volume, self.speed_btn, self.vr_btn, self.mark_btn,
                  self.fav_btn, self.delete_btn, self.fs_btn):
            row.addWidget(w)

    def _ensure_mpv(self) -> None:
        if self.mpv is not None:
            return
        self.mpv = mpv.MPV(
            wid=str(int(self.surface.winId())),
            vo="gpu-next",
            hwdec="auto-safe" if self.db.get_setting("hwdec", True) else "no",
            keep_open="yes",
            idle="yes",
            input_default_bindings=False,
            input_vo_keyboard=False,
            input_cursor_passthrough=True,
            osd_level=1,
            osd_duration=1500,
            osd_font_size=30,
            cache="yes",
            demuxer_max_bytes="300MiB",
            demuxer_readahead_secs=20,
            volume=self.volume.value(),
            volume_max=130,
            audio_device=self._validated_device()[0],
            audio_channels=self.db.get_setting("audio_channels", "auto-safe"),
            ytdl=False,
        )

    def reload_settings(self) -> None:
        """Apply settings that were changed while the player was already open."""
        self.apply_audio_device()
        if self.mpv is None:
            return
        self.mpv["audio-channels"] = self.db.get_setting("audio_channels", "auto-safe")
        self.mpv["hwdec"] = "auto-safe" if self.db.get_setting("hwdec", True) else "no"

    # ------------------------------------------------------------ playback

    def play(self, video: dict) -> None:
        self._ensure_mpv()
        self._save_resume()
        self.video = video
        self.layout, self.proj, self.lens = video["vr_layout"], video["vr_proj"], video["vr_lens_fov"]
        self.eye = int(self.db.get_setting("vr_eye", 0))
        self.vr_mode = int(self.db.get_setting("vr_mode", VR_FLAT)) if video["is_vr"] else VR_OFF
        self.yaw = self.pitch = 0.0
        self.fov = float(self.db.get_setting("vr_fov", DEFAULT_FOV))
        duration = video.get("duration") or 0
        resume = (video.get("resume_pos") or 0) if self.db.get_setting("remember_position", True) else 0
        start = resume if resume > 30 and (not duration or resume < duration - 60) else 0
        self._eof_seen = False
        self.mpv["start"] = f"{start:.1f}"
        self.mpv["pause"] = False
        # Muting is chosen before pressing play as often as during, so it is a setting the player
        # obeys rather than something that only exists once a video is running.
        self.mpv.mute = bool(self.db.get_setting("muted", False))
        self.mpv.play(video["path"])
        self._apply_vr()
        if start:
            self.mpv.show_text(f"Resumed at {fmt_time(start)} - press Home to start over", 3000)
        self.db.record_play(video["id"])
        self.seek.set_duration(duration)
        self.marking = None
        self._show_scenes()
        self._full_title = display_title(video)
        self._full_subtitle = " · ".join(x for x in (", ".join(video.get("performers") or []),
                                                     video.get("studio") or "") if x)
        self._update_header()
        self.fav_btn.setIcon(self._icons["heart" if video.get("favorite") else "heart_outline"])
        self._tick.start()
        self.video_changed.emit(video["id"])
        self.setFocus()

    # ------------------------------------------------------------ scenes

    def mark_scene(self) -> None:
        """Press where a scene starts, and again where it ends. The same scenes as marked in the
        headset: made into passthrough copies from Make passthrough copies, on its Scenes tab."""
        if self.video is None or self.mpv is None or self.mpv.time_pos is None:
            return
        now = float(self.mpv.time_pos)
        if self.marking is None:
            self.marking = now
            self.mpv.show_text(f"Scene started at {fmt_time(now)} - press T again where it ends", 2500)
        else:
            start, self.marking = self.marking, None
            try:
                self.db.add_scene(self.video["id"], start, now)
                self.mpv.show_text(f"Scene kept: {fmt_time(min(start, now))} - {fmt_time(max(start, now))}", 2500)
            except ValueError as exc:
                self.mpv.show_text(f"Not kept: {exc}", 2500)
        self._show_scenes()

    def _show_scenes(self) -> None:
        scenes = self.db.scenes(self.video["id"]) if self.video is not None else []
        now = float(self.mpv.time_pos or 0) if self.mpv is not None else 0.0
        self.seek.set_scenes([(x["start"], x["end"]) for x in scenes],
                             None if self.marking is None else (self.marking, now))
        self.mark_btn.setText("Mark scene" if self.marking is None else f"End scene · {fmt_time(self.marking)}")

    def _scenes_menu(self) -> None:
        if self.video is None:
            return
        menu = QMenu(self)
        scenes = self.db.scenes(self.video["id"])
        if not scenes:
            menu.addAction("No scenes marked in this film yet").setEnabled(False)
        for scene in scenes:
            span = f"{fmt_time(scene['start'])} - {fmt_time(scene['end'])}"
            menu.addAction(f"{scene['label']}   {span}",
                           lambda start=scene["start"]: self._seek_to(start, True))
        if scenes:
            remove = menu.addMenu("Remove a scene")
            for scene in scenes:
                remove.addAction(scene["label"], lambda sid=scene["id"]: (self.db.delete_scene(sid), self._show_scenes()))
        if self.marking is not None:
            menu.addSeparator()
            menu.addAction("Forget the scene being marked", lambda: (setattr(self, "marking", None), self._show_scenes()))
        menu.exec(QCursor.pos())

    def stop(self) -> None:
        """Stop playback and release the file (needed before it can be moved or deleted)."""
        self._save_resume()
        self._tick.stop()
        if self.mpv is not None:
            self.mpv.command("stop")
        self.video = None

    def shutdown(self) -> None:
        self.stop()
        if self.mpv is not None:
            self.mpv.terminate()
            self.mpv = None

    def _save_resume(self) -> None:
        if self.mpv is None or self.video is None or not self.db.get_setting("remember_position", True):
            return
        pos = self.mpv.time_pos
        duration = self.mpv.duration or self.video.get("duration") or 0
        if pos is None:
            return
        keep = pos if pos > 30 and (not duration or pos < duration - 60) else 0
        self.db.save_resume(self.video["id"], keep)
        self.video["resume_pos"] = keep

    def _update_header(self) -> None:
        for label, text in ((self.title_label, self._full_title), (self.subtitle_label, self._full_subtitle)):
            metrics = QFontMetrics(label.font())
            label.setText(metrics.elidedText(text, Qt.ElideRight, max(60, label.width())))

    def set_queue_position(self, index: int, total: int) -> None:
        self.queue_label.setText(f"{index} of {total}" if total > 1 else "")
        self.prev_arrow.setEnabled(index > 1)
        self.next_arrow.setEnabled(0 < index < total)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_header()

    def toggle_favorite(self) -> None:
        if self.video is None:
            return
        favorite = not self.video.get("favorite")
        self.video["favorite"] = int(favorite)
        self.fav_btn.setIcon(self._icons["heart" if favorite else "heart_outline"])
        self.favorite_requested.emit(self.video["id"], favorite)
        if self.mpv is not None:
            self.mpv.show_text("Added to favorites" if favorite else "Removed from favorites")

    def _request_delete(self) -> None:
        if self.video is not None:
            self.delete_requested.emit(self.video["id"])

    def toggle_pause(self) -> None:
        if self.mpv is not None:
            self.mpv.pause = not self.mpv.pause
            self._update_ui()

    def toggle_mute(self) -> None:
        self.set_muted(not self.muted())

    def muted(self) -> bool:
        if self.mpv is not None:
            return bool(self.mpv.mute)
        return bool(self.db.get_setting("muted", False))

    def set_muted(self, muted: bool) -> None:
        """Remembered, so it holds between videos and between sittings."""
        self.db.set_setting("muted", bool(muted))
        if self.mpv is not None:
            self.mpv.mute = bool(muted)
            self._update_ui()
        self.mute_changed.emit(bool(muted))

    def _audio_menu(self) -> None:
        menu = QMenu(self)
        mute = menu.addAction("Mute")
        mute.setCheckable(True)
        mute.setChecked(bool(self.mpv.mute) if self.mpv is not None else False)
        mute.triggered.connect(self.toggle_mute)
        menu.addSeparator()
        heading = menu.addAction("Play sound through")
        heading.setEnabled(False)
        current = self.db.get_setting("audio_device", AUTO_DEVICE)
        group = QActionGroup(menu)
        for name, description in [(AUTO_DEVICE, "System default")] + self.audio_devices():
            action = menu.addAction(description)
            action.setCheckable(True)
            action.setChecked(name == current)
            group.addAction(action)
            action.triggered.connect(lambda _=False, device=name: self.set_audio_device(device))
        menu.exec(QCursor.pos())

    def audio_devices(self) -> list[tuple[str, str]]:
        try:
            return _device_list(self.mpv) if self.mpv is not None else audio_devices()
        except Exception:
            return []

    def device_label(self, name: str) -> str:
        if name == AUTO_DEVICE:
            return "System default"
        return next((d for n, d in self.audio_devices() if n == name), name)

    def _validated_device(self) -> tuple[str, bool]:
        """The audio device to use, and whether the saved choice still exists.

        mpv accepts any string for audio-device and only fails when it opens the device, which would
        mean silent playback with no explanation, so the saved name is checked against real devices.
        """
        name = self.db.get_setting("audio_device", AUTO_DEVICE)
        known = {AUTO_DEVICE} | {device for device, _ in self.audio_devices()}
        if name in known:
            return name, True
        self.db.set_setting("audio_device", AUTO_DEVICE)
        return AUTO_DEVICE, False

    def set_audio_device(self, name: str) -> None:
        self.db.set_setting("audio_device", name)
        self.apply_audio_device(announce=True)

    def apply_audio_device(self, announce: bool = False) -> None:
        """Send the saved audio device to mpv, falling back to the default if it has gone away."""
        if self.mpv is None:
            return
        name, still_there = self._validated_device()
        self.mpv["audio-device"] = name
        if not still_there:
            self.mpv.show_text("That audio device isn't available any more, using the system default")
            return
        if announce:
            self.mpv.show_text(f"Sound: {self.device_label(name)}")

    def _set_volume(self, value: int) -> None:
        if self.mpv is not None:
            self.mpv.volume = value
        self.db.set_setting("volume", value)

    def _seek_to(self, seconds: float, exact: bool) -> None:
        if self.mpv is not None and self.video is not None:
            self.mpv.seek(seconds, "absolute" if exact else "absolute+keyframes")

    def seek_relative(self, seconds: float) -> None:
        if self.mpv is not None and self.video is not None:
            self.mpv.seek(seconds, "relative")

    def _speed_menu(self) -> None:
        menu = QMenu(self)
        for speed in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            action = menu.addAction(f"{speed:g}×")
            action.triggered.connect(lambda _=False, s=speed: self._set_speed(s))
        menu.exec(QCursor.pos())

    def _set_speed(self, speed: float) -> None:
        if self.mpv is not None:
            self.mpv.speed = max(0.1, min(4.0, round(speed, 2)))
            self.mpv.show_text(f"Speed {self.mpv.speed:g}×")
            self._update_ui()

    def _update_ui(self) -> None:
        if self.mpv is None:
            return
        paused = bool(self.mpv.pause)
        self.play_btn.setIcon(self._icons["play" if paused else "pause"])
        self.mute_btn.setIcon(self._icons["muted" if self.mpv.mute else "volume"])
        pos, duration = self.mpv.time_pos, self.mpv.duration
        if duration and self.seek.maximum() != int(duration * 10):
            self.seek.set_duration(duration)
        if pos is not None and not self.seek.dragging:
            self.seek.blockSignals(True)
            self.seek.setValue(int(pos * 10))
            self.seek.blockSignals(False)
        self.time_label.setText(f"{fmt_time(pos)} / {fmt_time(duration)}")
        self.speed_btn.setText(f"{self.mpv.speed or 1:g}×")
        if self.marking is not None:
            self._show_scenes()           # the band grows as the scene plays on
        self.vr_btn.setText("VR ●" if self.vr_mode != VR_OFF else "VR")
        if self._crop_pending and self.mpv.width:
            self._apply_vr()
        if self.mpv.eof_reached and not self._eof_seen:
            self._eof_seen = True
            if self.db.get_setting("autoplay_next", False):
                self.next_requested.emit()
        self._since_resume_save += 1
        if self._since_resume_save >= 40:
            self._since_resume_save = 0
            self._save_resume()

    # ------------------------------------------------------------ VR

    def _apply_vr(self) -> None:
        m = self.mpv
        if m is None:
            return
        self._crop_pending = False
        if self.vr_mode == VR_FLAT:
            m["video-crop"] = ""
            m["keepaspect"] = False
            m["glsl-shaders"] = [str(paths.SHADER_PATH)]
            self._view_dirty = True
            self._flush_view()
        else:
            m["glsl-shaders"] = []
            m["keepaspect"] = True
            if self.vr_mode == VR_CROP and self.layout != LAYOUT_MONO:
                w, h = m.width, m.height
                if not w or not h:
                    self._crop_pending = True
                    m["video-crop"] = ""
                    return
                if self.layout == LAYOUT_SBS:
                    m["video-crop"] = f"{w // 2}x{h}+{self.eye * (w // 2)}+0"
                else:
                    m["video-crop"] = f"{w}x{h // 2}+0+{self.eye * (h // 2)}"
            else:
                m["video-crop"] = ""

    def _flush_view(self) -> None:
        if not self._view_dirty or self.mpv is None or self.vr_mode != VR_FLAT:
            self._view_timer.stop()
            return
        self._view_dirty = False
        self.mpv["glsl-shader-opts"] = (
            f"vr_yaw={self.yaw:.5f},vr_pitch={self.pitch:.5f},vr_fov={self.fov:.2f},vr_eye={self.eye},"
            f"vr_layout={self.layout},vr_proj={self.proj},vr_lens_fov={self.lens:.1f}")

    def _queue_view(self) -> None:
        self._view_dirty = True
        if not self._view_timer.isActive():
            self._view_timer.start()

    def _max_yaw(self) -> float:
        if self.proj == PROJ_EQUI360:
            return math.pi
        if self.proj == PROJ_FISHEYE:
            return math.radians(self.lens / 2)
        return math.pi / 2

    def _drag(self, dx: int, dy: int) -> None:
        if self.vr_mode != VR_FLAT:
            return
        width = max(1, self.surface.width())
        per_pixel = math.radians(self.fov) / width
        self.yaw = max(-self._max_yaw(), min(self._max_yaw(), self.yaw - dx * per_pixel))
        self.pitch = max(-1.4, min(1.4, self.pitch + dy * per_pixel))
        self._queue_view()

    def _wheel(self, delta: int) -> None:
        steps = delta / 120
        if self.vr_mode == VR_FLAT:
            self.fov = max(30.0, min(150.0, self.fov - steps * 5))
            self.db.set_setting("vr_fov", self.fov)
            self._queue_view()
        else:
            self.volume.setValue(self.volume.value() + int(steps * 5))

    def look(self, dyaw: float, dpitch: float) -> None:
        self.yaw = max(-self._max_yaw(), min(self._max_yaw(), self.yaw + dyaw))
        self.pitch = max(-1.4, min(1.4, self.pitch + dpitch))
        self._queue_view()

    def set_vr_mode(self, mode: int) -> None:
        self.vr_mode = mode
        if mode != VR_OFF:
            self.db.set_setting("vr_mode", mode)
        self._apply_vr()
        if self.mpv is not None:
            self.mpv.show_text(VR_MODE_LABELS[mode])
        self._update_ui()

    def cycle_vr_mode(self) -> None:
        self.set_vr_mode((self.vr_mode + 1) % 3)

    def swap_eye(self) -> None:
        self.eye = 1 - self.eye
        self.db.set_setting("vr_eye", self.eye)
        self._apply_vr()
        if self.mpv is not None:
            self.mpv.show_text("Right eye" if self.eye else "Left eye")

    def reset_view(self) -> None:
        self.yaw = self.pitch = 0.0
        self.fov = DEFAULT_FOV
        self._queue_view()

    def _save_vr_format(self, is_vr: bool = True) -> None:
        if self.video is None:
            return
        fields = dict(is_vr=int(is_vr), vr_layout=self.layout, vr_proj=self.proj, vr_lens_fov=self.lens, vr_locked=1)
        self.db.set_fields(self.video["id"], **fields)
        self.video.update(fields)

    def _vr_menu(self) -> None:
        menu = QMenu(self)
        is_vr = bool(self.video and self.video["is_vr"])
        toggle = menu.addAction("This is a VR video")
        toggle.setCheckable(True)
        toggle.setChecked(is_vr)
        toggle.toggled.connect(self._toggle_is_vr)
        menu.addSeparator()
        modes = QActionGroup(menu)
        for mode, label in VR_MODE_LABELS.items():
            a = menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(self.vr_mode == mode)
            modes.addAction(a)
            a.triggered.connect(lambda _=False, m=mode: self.set_vr_mode(m))
        menu.addSeparator()
        eye = menu.addAction("Use right eye")
        eye.setCheckable(True)
        eye.setChecked(self.eye == 1)
        eye.triggered.connect(self.swap_eye)
        menu.addAction("Reset view (R)").triggered.connect(self.reset_view)
        layout_menu = menu.addMenu("Layout")
        group = QActionGroup(layout_menu)
        for value, label in LAYOUT_LABELS.items():
            a = layout_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(self.layout == value)
            group.addAction(a)
            a.triggered.connect(lambda _=False, v=value: self._set_layout(v))
        proj_menu = menu.addMenu("Projection")
        group = QActionGroup(proj_menu)
        for label, proj, lens in PROJECTION_CHOICES:
            a = proj_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(self.proj == proj and (proj != PROJ_FISHEYE or self.lens == lens))
            group.addAction(a)
            a.triggered.connect(lambda _=False, p=proj, l=lens: self._set_projection(p, l))
        menu.exec(QCursor.pos())

    def _toggle_is_vr(self, checked: bool) -> None:
        self._save_vr_format(checked)
        self.set_vr_mode(VR_FLAT if checked else VR_OFF)

    def _set_layout(self, layout: int) -> None:
        self.layout = layout
        self._save_vr_format()
        self._apply_vr()

    def _set_projection(self, proj: int, lens: float) -> None:
        self.proj, self.lens = proj, lens
        self._save_vr_format()
        self.reset_view()
        self._apply_vr()

    # ------------------------------------------------------------ fullscreen controls

    def set_fullscreen(self, on: bool) -> None:
        self.fullscreen = on
        self.fs_btn.setIcon(self._icons["exit_fullscreen" if on else "fullscreen"])
        if on:
            self._hide_timer.start()
        else:
            self._hide_timer.stop()
            self._show_chrome(True)
            self.surface.unsetCursor()

    def _show_chrome(self, visible: bool) -> None:
        for widget in (self.header, self.controls, self.prev_arrow, self.next_arrow):
            widget.setVisible(visible)
        self.cleanup_bar.setVisible(visible and self.cleanup)

    def _reveal_controls(self) -> None:
        if self.fullscreen:
            self._show_chrome(True)
            self.surface.unsetCursor()
            self._hide_timer.start()

    def _hide_controls(self) -> None:
        over_chrome = any(w.underMouse() for w in (self.controls, self.header, self.prev_arrow, self.next_arrow))
        if self.fullscreen and not over_chrome:
            self._show_chrome(False)
            self.surface.setCursor(Qt.BlankCursor)

    # ------------------------------------------------------------ keyboard

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key, mods = event.key(), event.modifiers()
        if self.cleanup and not mods:
            cleanup_keys = {Qt.Key_K: self.cleanup_keep, Qt.Key_D: self.cleanup_delete,
                            Qt.Key_U: self.cleanup_undo, Qt.Key_Escape: self.cleanup_finish}
            if key in cleanup_keys:
                cleanup_keys[key].emit()
                return
        if mods & Qt.AltModifier and key in (Qt.Key_Left, Qt.Key_Right):
            (self.next_requested if key == Qt.Key_Right else self.prev_requested).emit()
            self._reveal_controls()
            return
        jump = float(self.db.get_setting("seek_step", 5))
        step = 30 if mods & Qt.ControlModifier else 1 if mods & Qt.ShiftModifier else jump
        look = math.radians(5)
        actions = {
            Qt.Key_Space: self.toggle_pause,
            Qt.Key_K: self.toggle_pause,
            Qt.Key_Left: lambda: self.seek_relative(-step),
            Qt.Key_Right: lambda: self.seek_relative(step),
            Qt.Key_Up: lambda: self.volume.setValue(self.volume.value() + 5),
            Qt.Key_Down: lambda: self.volume.setValue(self.volume.value() - 5),
            Qt.Key_M: self.toggle_mute,
            Qt.Key_F: self.fullscreen_requested.emit,
            Qt.Key_Return: self.fullscreen_requested.emit,
            Qt.Key_Enter: self.fullscreen_requested.emit,
            Qt.Key_Escape: self._escape,
            Qt.Key_PageDown: self.next_requested.emit,
            Qt.Key_N: self.next_requested.emit,
            Qt.Key_PageUp: self.prev_requested.emit,
            Qt.Key_P: self.prev_requested.emit,
            Qt.Key_Home: lambda: self._seek_to(0, True),
            Qt.Key_Delete: lambda: self.video and self.delete_requested.emit(self.video["id"]),
            Qt.Key_BracketLeft: lambda: self.mpv and self._set_speed((self.mpv.speed or 1) - 0.25),
            Qt.Key_BracketRight: lambda: self.mpv and self._set_speed((self.mpv.speed or 1) + 0.25),
            Qt.Key_Backspace: lambda: self._set_speed(1.0),
            Qt.Key_Period: lambda: self.mpv and self.mpv.command("frame-step"),
            Qt.Key_Comma: lambda: self.mpv and self.mpv.command("frame-back-step"),
            Qt.Key_V: self.cycle_vr_mode,
            Qt.Key_E: self.swap_eye,
            Qt.Key_R: self.reset_view,
            Qt.Key_W: lambda: self.look(0, look),
            Qt.Key_S: lambda: self.look(0, -look),
            Qt.Key_A: lambda: self.look(-look, 0),
            Qt.Key_D: lambda: self.look(look, 0),
            Qt.Key_Plus: lambda: self._wheel(-120),
            Qt.Key_Equal: lambda: self._wheel(-120),
            Qt.Key_Minus: lambda: self._wheel(120),
            Qt.Key_I: lambda: self.mpv and self.mpv.command("script-binding", "stats/display-stats-toggle"),
            Qt.Key_T: self.mark_scene,
        }
        if Qt.Key_0 <= key <= Qt.Key_5 and self.video is not None:
            self.rate_requested.emit(self.video["id"], key - Qt.Key_0)
            return
        action = actions.get(key)
        if action is None:
            super().keyPressEvent(event)
            return
        action()
        self._reveal_controls()

    def _escape(self) -> None:
        if self.fullscreen:
            self.fullscreen_requested.emit()
        else:
            self.back_requested.emit()
