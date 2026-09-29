"""Right-hand panel: details and editing for the selected video or videos."""
from __future__ import annotations

import os
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QCompleter, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from blueshelf.db import Database
from blueshelf.parser import LAYOUT_MONO, LAYOUT_SBS, LAYOUT_TB, PROJ_EQUI180, PROJ_EQUI360, PROJ_FISHEYE, performer_key
from blueshelf.theme import ACCENT, COLORS
from blueshelf.util import breakable_html, fmt_bitrate, fmt_date, fmt_size, fmt_time
from blueshelf.widgets import StarRating, TokenLineEdit

LAYOUTS = [("Side by side", LAYOUT_SBS), ("Top / bottom", LAYOUT_TB), ("Mono", LAYOUT_MONO)]
PROJECTIONS = [("180°", PROJ_EQUI180, 190.0), ("360°", PROJ_EQUI360, 190.0), ("Fisheye 190°", PROJ_FISHEYE, 190.0),
               ("Fisheye 200°", PROJ_FISHEYE, 200.0), ("Fisheye 220°", PROJ_FISHEYE, 220.0)]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MULTI_ACTIONS = [
    ("▶  Play selected", "play"), ("Add performer…", "add_performer"), ("Add category…", "add_tag"),
    ("Set studio…", "set_studio"), ("Add to favorites", "favorite"), ("Remove from favorites", "unfavorite"),
    ("Mark as VR", "vr"), ("Mark as not VR", "not_vr"), ("Hide from library", "hide"), ("Unhide", "unhide"),
    ("Re-read from file names", "reparse"), ("Make new thumbnails", "thumb"),
]


# What to call each place a video can be played from, in the details panel.
PLACES = {"deovr": "DeoVR", "page": "the headset page", "dlna": "a DLNA player"}


class DetailsPanel(QWidget):
    play_requested = Signal(int)
    bulk_requested = Signal(str, list)
    changed = Signal(list)
    folder_requested = Signal(str)

    def __init__(self, db: Database, cache, parent=None):
        super().__init__(parent)
        self.db = db
        self.cache = cache
        self.video: dict | None = None
        self.selection: list[dict] = []
        self._loading = False
        self.setMinimumWidth(300)
        self.stack = QStackedWidget(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.addWidget(self.stack)
        empty = QLabel("Select a video to see and edit its details.\n\nDouble-click a video to play it.")
        empty.setObjectName("muted")
        empty.setAlignment(Qt.AlignCenter)
        empty.setWordWrap(True)
        self.stack.addWidget(empty)
        self.stack.addWidget(self._build_single())
        self.stack.addWidget(self._build_multi())

    # ------------------------------------------------------------ construction

    def _build_single(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self.thumb = QLabel()
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet(f"background: {COLORS['thumb_bg']}; border-radius: 8px; "
                                 f"color: {COLORS['placeholder']};")
        lay.addWidget(self.thumb)

        buttons = QHBoxLayout()
        play = QPushButton("▶  Play", objectName="primary")
        play.clicked.connect(lambda: self.video and self.play_requested.emit(self.video["id"]))
        folder = QPushButton("Show in folder")
        folder.clicked.connect(lambda: self.video and self.folder_requested.emit(self.video["path"]))
        delete = QPushButton("Delete…", objectName="danger")
        delete.clicked.connect(lambda: self.video and self.bulk_requested.emit("delete", [self.video["id"]]))
        buttons.addWidget(play, 1)
        buttons.addWidget(folder)
        buttons.addWidget(delete)
        lay.addLayout(buttons)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.title_edit = QLineEdit()
        self.title_edit.editingFinished.connect(self._commit_title)
        self.performers_edit = TokenLineEdit()
        self.performers_edit.setPlaceholderText("Name, Name")
        self.performers_edit.editingFinished.connect(self._commit_performers)
        self.studio_edit = QLineEdit()
        self.studio_completer = QCompleter([], self.studio_edit)
        self.studio_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.studio_completer.setFilterMode(Qt.MatchContains)
        self.studio_edit.setCompleter(self.studio_completer)
        self.studio_edit.editingFinished.connect(self._commit_studio)
        self.date_edit = QLineEdit(placeholderText="YYYY-MM-DD")
        self.date_edit.editingFinished.connect(self._commit_date)
        self.tags_edit = TokenLineEdit()
        self.tags_edit.setPlaceholderText("Category, Category")
        self.tags_edit.editingFinished.connect(self._commit_tags)
        form.addRow("Title", self.title_edit)
        form.addRow("Performers", self.performers_edit)
        form.addRow("Studio", self.studio_edit)
        form.addRow("Released", self.date_edit)
        form.addRow("Categories", self.tags_edit)

        rating_row = QHBoxLayout()
        self.stars = StarRating()
        self.stars.changed.connect(self._commit_rating)
        self.fav_btn = QToolButton(checkable=True, toolTip="Favorite")
        self.fav_btn.setStyleSheet(f"QToolButton {{ font-size: 18px; color: {ACCENT}; }}")
        self.fav_btn.toggled.connect(self._commit_favorite)
        rating_row.addWidget(self.stars)
        rating_row.addStretch()
        rating_row.addWidget(self.fav_btn)
        form.addRow("Rating", rating_row)
        lay.addLayout(form)

        self.hidden_check = QCheckBox("Hide from library")
        self.hidden_check.toggled.connect(self._commit_hidden)
        lay.addWidget(self.hidden_check)
        vr_row = QHBoxLayout()
        self.vr_check = QCheckBox("VR video")
        self.vr_check.toggled.connect(self._commit_vr)
        self.layout_combo = QComboBox()
        for label, _ in LAYOUTS:
            self.layout_combo.addItem(label)
        self.layout_combo.currentIndexChanged.connect(self._commit_vr)
        self.proj_combo = QComboBox()
        for label, _, _ in PROJECTIONS:
            self.proj_combo.addItem(label)
        self.proj_combo.currentIndexChanged.connect(self._commit_vr)
        vr_row.addWidget(self.vr_check)
        vr_row.addWidget(self.layout_combo, 1)
        vr_row.addWidget(self.proj_combo, 1)
        lay.addLayout(vr_row)

        self.info = QLabel()
        self.info.setTextFormat(Qt.RichText)
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.info)

        more = QHBoxLayout()
        reread = QPushButton("Re-read file name")
        reread.setToolTip("Discard edits and work out title, performers and categories from the file name again")
        reread.clicked.connect(lambda: self.video and self.bulk_requested.emit("reparse", [self.video["id"]]))
        new_thumb = QPushButton("New thumbnail")
        new_thumb.clicked.connect(lambda: self.video and self.bulk_requested.emit("thumb", [self.video["id"]]))
        more.addWidget(reread)
        more.addWidget(new_thumb)
        lay.addLayout(more)
        lay.addStretch()
        return page

    def _build_multi(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.multi_label = QLabel(objectName="heading")
        self.multi_sub = QLabel(objectName="muted")
        lay.addWidget(self.multi_label)
        lay.addWidget(self.multi_sub)
        lay.addSpacing(6)
        for label, action in MULTI_ACTIONS:
            button = QPushButton(label)
            if action == "play":
                button.setObjectName("primary")
            button.clicked.connect(lambda _=False, a=action: self.bulk_requested.emit(a, self._ids()))
            lay.addWidget(button)
        lay.addSpacing(6)
        self.multi_delete = QPushButton(objectName="danger")
        self.multi_delete.clicked.connect(lambda: self.bulk_requested.emit("delete", self._ids()))
        lay.addWidget(self.multi_delete)
        lay.addStretch()
        return page

    def _ids(self) -> list[int]:
        return [v["id"] for v in self.selection]

    # ------------------------------------------------------------ loading

    def apply_colors(self) -> None:
        """Repaint the parts that carry their own colours after a theme change."""
        self.thumb.setStyleSheet(f"background: {COLORS['thumb_bg']}; border-radius: 8px; "
                                 f"color: {COLORS['placeholder']};")
        self.fav_btn.setStyleSheet(f"QToolButton {{ font-size: 18px; color: {ACCENT}; }}")

    def update_completions(self) -> None:
        self.performers_edit.set_words(self.db.performer_names())
        self.tags_edit.set_words(self.db.tag_names())
        self.studio_completer.model().setStringList(self.db.studio_names())

    def _editing(self) -> bool:
        return any(w.hasFocus() for w in (self.title_edit, self.performers_edit, self.studio_edit, self.date_edit,
                                          self.tags_edit))

    def show_videos(self, videos: list[dict]) -> None:
        self.selection = videos
        if not videos:
            self.video = None
            self.stack.setCurrentIndex(0)
        elif len(videos) == 1:
            if self.video and self.video["id"] == videos[0]["id"] and self._editing():
                return
            self.load(videos[0]["id"])
        else:
            self.video = None
            self.multi_label.setText(f"{len(videos)} videos selected")
            total_size = sum(v["size"] for v in videos)
            total_time = sum(v["duration"] or 0 for v in videos)
            self.multi_sub.setText(f"{fmt_size(total_size)} · {fmt_time(total_time)}")
            self.multi_delete.setText(f"Delete {len(videos)} videos…")
            self.stack.setCurrentIndex(2)

    def load(self, video_id: int) -> None:
        video = self.db.get_video(video_id)
        if video is None:
            self.show_videos([])
            return
        self.video = video
        self._loading = True
        self.title_edit.setText(video["title"])
        self.title_edit.setCursorPosition(0)
        self.performers_edit.setText(", ".join(video["performers"]))
        self.performers_edit.setCursorPosition(0)
        self.studio_edit.setText(video["studio"])
        self.date_edit.setText(video["release_date"])
        self.date_edit.setStyleSheet("")
        self.tags_edit.setText(", ".join(video["tags"]))
        self.tags_edit.setCursorPosition(0)
        self.stars.set_value(video["rating"] or 0)
        self.fav_btn.setChecked(bool(video["favorite"]))
        self.fav_btn.setText("♥" if video["favorite"] else "♡")
        self.hidden_check.setChecked(bool(video["hidden"]))
        self.vr_check.setChecked(bool(video["is_vr"]))
        self.layout_combo.setCurrentIndex(next((i for i, (_, v) in enumerate(LAYOUTS) if v == video["vr_layout"]), 0))
        self.proj_combo.setCurrentIndex(next(
            (i for i, (_, proj, lens) in enumerate(PROJECTIONS)
             if proj == video["vr_proj"] and (proj != PROJ_FISHEYE or lens == video["vr_lens_fov"])), 0))
        self.layout_combo.setEnabled(bool(video["is_vr"]))
        self.proj_combo.setEnabled(bool(video["is_vr"]))
        self.info.setText(self._info_html(video))
        self._loading = False
        self._set_thumb()
        self.stack.setCurrentIndex(1)

    def _info_html(self, v: dict) -> str:
        video_bits = [f"{v['width']}×{v['height']}" if v["width"] else "", (v["vcodec"] or "").upper(),
                      fmt_bitrate(v["bitrate"]), f"{v['fps']:g} fps" if v["fps"] else ""]
        played = "Never"
        if v["play_count"]:
            played = f"{v['play_count']}×" + (f", last {fmt_date(v['last_played'])}" if v["last_played"] else "")
            # Where they happened, when it was anywhere but this PC.
            elsewhere = {source: n for source, n in self.db.play_counts(v["id"]).items() if source != "desktop"}
            if elsewhere:
                played += " (" + ", ".join(f"{n} in {PLACES.get(source, source)}"
                                           for source, n in sorted(elsewhere.items())) + ")"
        rows = [
            ("File", breakable_html(os.path.basename(v["path"]))),
            ("Folder", breakable_html(os.path.dirname(v["path"]))),
            ("Size", fmt_size(v["size"])),
            ("Length", fmt_time(v["duration"]) if v["duration"] else "—"),
            ("Video", " · ".join(b for b in video_bits if b) or "—"),
            ("Added", fmt_date(v["added_at"])),
            ("Played", played),
        ]
        cells = "".join(f"<tr><td style='color:#8d8f98; padding:2px 10px 2px 0'>{k}</td><td>{val}</td></tr>"
                        for k, val in rows)
        return f"<table cellspacing=0>{cells}</table>"

    def _set_thumb(self) -> None:
        if not self.video:
            return
        width = max(200, self.stack.width())
        height = round(width * 9 / 16)
        self.thumb.setFixedHeight(height)
        pix = self.cache.scaled(self.video, width, height, self.devicePixelRatioF())
        if pix is not None:
            self.thumb.setPixmap(pix)
        else:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("No preview yet")

    def refresh_thumb(self, video_id: int) -> None:
        if self.video and self.video["id"] == video_id:
            self.video["thumb"] = self.db.get_video(video_id)["thumb"]
            self._set_thumb()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.video:
            self._set_thumb()

    # ------------------------------------------------------------ saving edits

    def _save(self, **fields) -> None:
        self.db.set_fields(self.video["id"], **fields)
        self.video.update(fields)
        self.changed.emit([self.video["id"]])

    def _commit_title(self) -> None:
        if self._loading or not self.video:
            return
        text = self.title_edit.text().strip()
        if text != self.video["title"]:
            self._save(title=text, meta_locked=1)

    def _commit_studio(self) -> None:
        if self._loading or not self.video:
            return
        text = self.studio_edit.text().strip()
        if text != self.video["studio"]:
            self._save(studio=text, meta_locked=1)

    def _commit_date(self) -> None:
        if self._loading or not self.video:
            return
        text = self.date_edit.text().strip()
        if text and not DATE_RE.match(text):
            self.date_edit.setStyleSheet(f"border-color: {ACCENT};")
            return
        self.date_edit.setStyleSheet("")
        if text != self.video["release_date"]:
            self._save(release_date=text, meta_locked=1)

    def _commit_performers(self) -> None:
        if self._loading or not self.video:
            return
        names = [n.strip() for n in self.performers_edit.text().split(",") if n.strip()]
        if sorted(map(performer_key, names)) == sorted(map(performer_key, self.video["performers"])):
            return
        self.db.set_performers(self.video["id"], names)
        self.video["performers"] = self.db.video_performer_names(self.video["id"])
        self._loading = True
        self.performers_edit.setText(", ".join(self.video["performers"]))
        self._loading = False
        self.changed.emit([self.video["id"]])

    def _commit_tags(self) -> None:
        if self._loading or not self.video:
            return
        names = [n.strip() for n in self.tags_edit.text().split(",") if n.strip()]
        if sorted(n.lower() for n in names) == sorted(n.lower() for n in self.video["tags"]):
            return
        self.db.set_tags(self.video["id"], names)
        self.video["tags"] = self.db.get_video(self.video["id"])["tags"]
        self._loading = True
        self.tags_edit.setText(", ".join(self.video["tags"]))
        self._loading = False
        self.changed.emit([self.video["id"]])

    def _commit_rating(self, value: int) -> None:
        if not self._loading and self.video:
            self._save(rating=value)

    def _commit_favorite(self, on: bool) -> None:
        self.fav_btn.setText("♥" if on else "♡")
        if not self._loading and self.video:
            self._save(favorite=int(on))

    def _commit_hidden(self, on: bool) -> None:
        if not self._loading and self.video:
            self._save(hidden=int(on), hidden_locked=1)

    def _commit_vr(self, *_args) -> None:
        if self._loading or not self.video:
            return
        is_vr = self.vr_check.isChecked()
        self.layout_combo.setEnabled(is_vr)
        self.proj_combo.setEnabled(is_vr)
        _, proj, lens = PROJECTIONS[self.proj_combo.currentIndex()]
        layout = LAYOUTS[self.layout_combo.currentIndex()][1]
        self._save(is_vr=int(is_vr), vr_layout=layout, vr_proj=proj, vr_lens_fov=lens, vr_locked=1)
        self.bulk_requested.emit("thumb", [self.video["id"]])
