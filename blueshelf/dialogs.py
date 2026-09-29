"""Welcome page, settings, trash and help dialogs."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from blueshelf import paths, trash
from blueshelf.db import Database
from blueshelf.parser import DEFAULT_TAG_RULES, bad_tag_rule_lines
from blueshelf.player import AUTO_DEVICE, audio_devices
from blueshelf.scanner import remove_thumb
from blueshelf.theme import MODES, make_icon
from blueshelf.util import VR_CROP, VR_FLAT, VR_OFF, fmt_date, fmt_size

VR_MODE_CHOICES = [("Flat view (drag to look around)", VR_FLAT), ("One eye, no reprojection", VR_CROP),
                   ("The whole frame, both eyes", VR_OFF)]


def library_overlap(db: Database, folder: str) -> str | None:
    """Explain why a folder can't be added, or None when it's fine."""
    norm = os.path.normcase(os.path.abspath(folder)).rstrip("\\/")
    for lib in db.libraries():
        other = os.path.normcase(os.path.abspath(lib["path"])).rstrip("\\/")
        if norm == other:
            return f"{folder} is already in your library."
        if norm.startswith(other + os.sep):
            return f"{folder} is inside {lib['path']}, which BlueShelf already scans."
        if other.startswith(norm + os.sep):
            return (f"{lib['path']} is already in your library and is inside {folder}. "
                    "Remove it in Settings first, then add the larger folder.")
    return None


class WelcomePage(QWidget):
    add_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.addStretch()
        icon = QLabel()
        icon.setPixmap(make_icon(112))
        title = QLabel("Welcome to BlueShelf", objectName="heading")
        text = QLabel("Choose the folder where your videos are, on this PC or on a network drive. BlueShelf reads file "
                      "and folder names to sort videos by performer, studio and category, makes thumbnails and "
                      "finds duplicates. It never changes your files unless you delete something.")
        text.setObjectName("muted")
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignCenter)
        text.setMaximumWidth(540)
        button = QPushButton("Add a video folder…", objectName="primary")
        button.setMinimumWidth(220)
        button.clicked.connect(self.add_clicked)
        note = QLabel(f"Your library data and thumbnails stay on this PC, in {paths.data_dir()}")
        note.setObjectName("muted")
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignCenter)
        for widget in (icon, title, text, button, note):
            outer.addWidget(widget, 0, Qt.AlignHCenter)
            outer.addSpacing(10)
        outer.addStretch()


class SettingsDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.libraries_added = False
        self.libraries_removed = False
        self.reparse_requested = False
        self.setWindowTitle("Settings")
        self.resize(680, 560)
        tabs = QTabWidget(self)

        folders = QWidget()
        fl = QVBoxLayout(folders)
        fl.addWidget(QLabel("BlueShelf looks for videos in these folders. Changes here take effect straight away."))
        self.lib_list = QListWidget()
        fl.addWidget(self.lib_list, 1)
        row = QHBoxLayout()
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add_library)
        change = QPushButton("Change folder…")
        change.setToolTip("Point this entry at a new location, keeping ratings, performers and categories.\n"
                          "Use this when a drive letter changes instead of removing and adding the folder.")
        change.clicked.connect(self._relocate_library)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_library)
        row.addWidget(add)
        row.addWidget(change)
        row.addWidget(remove)
        row.addStretch()
        fl.addLayout(row)
        self._load_libraries()
        tabs.addTab(folders, "Folders")

        general = QWidget()
        gf = QFormLayout(general)
        self.scan_check = QCheckBox("Look for new and changed videos when BlueShelf starts")
        self.scan_check.setChecked(db.get_setting("scan_on_start", True))
        self.auto_scan_spin = QSpinBox()
        self.auto_scan_spin.setRange(0, 720)
        self.auto_scan_spin.setSuffix(" minutes")
        self.auto_scan_spin.setSpecialValueText("Never")
        self.auto_scan_spin.setValue(int(db.get_setting("auto_scan_minutes", 10) or 0))
        self.auto_scan_spin.setToolTip("New downloads appear by themselves. The check is quiet and only reads "
                                       "the folders, so it costs little even on a network drive.")
        self.hide_check = QCheckBox("Hide mainstream movies and TV episodes (worked out from release names)")
        self.hide_check.setChecked(db.get_setting("hide_non_adult", True))
        self.delete_combo = QComboBox()
        self.delete_combo.addItems(["Move to the trash folder (can be restored)", "Delete permanently"])
        self.delete_combo.setCurrentIndex(1 if db.get_setting("delete_mode", "trash") == "permanent" else 0)
        self.thumb_spin = QSpinBox()
        self.thumb_spin.setRange(5, 95)
        self.thumb_spin.setSuffix(" % into the video")
        self.thumb_spin.setValue(int(db.get_setting("thumb_position", 0.3) * 100))
        self.theme_combo = QComboBox()
        for key, label in MODES.items():
            self.theme_combo.addItem(label, key)
        saved_theme = self.theme_combo.findData(db.get_setting("theme", "system"))
        self.theme_combo.setCurrentIndex(saved_theme if saved_theme >= 0 else 0)
        gf.addRow("Appearance", self.theme_combo)
        gf.addRow(self.scan_check)
        gf.addRow("Check for new videos every", self.auto_scan_spin)
        gf.addRow(self.hide_check)
        gf.addRow("When deleting", self.delete_combo)
        gf.addRow("Thumbnails taken", self.thumb_spin)
        tabs.addTab(general, "General")

        playback = QWidget()
        pf = QFormLayout(playback)
        self.hw_check = QCheckBox("Use hardware decoding (turn off if videos show green or broken pictures)")
        self.hw_check.setChecked(db.get_setting("hwdec", True))
        self.vr_combo = QComboBox()
        for label, value in VR_MODE_CHOICES:
            self.vr_combo.addItem(label, value)
        self.vr_combo.setCurrentIndex(max(0, self.vr_combo.findData(int(db.get_setting("vr_mode", VR_FLAT)))))
        self.eye_combo = QComboBox()
        self.eye_combo.addItems(["Left eye", "Right eye"])
        self.eye_combo.setCurrentIndex(int(db.get_setting("vr_eye", 0)))
        self.audio_combo = QComboBox()
        self.audio_combo.addItem("System default", AUTO_DEVICE)
        try:
            for name, description in audio_devices():
                self.audio_combo.addItem(description, name)
        except Exception:
            pass
        saved = self.audio_combo.findData(db.get_setting("audio_device", AUTO_DEVICE))
        self.audio_combo.setCurrentIndex(saved if saved >= 0 else 0)
        self.channels_combo = QComboBox()
        self.channels_combo.addItem("As in the file", "auto-safe")
        self.channels_combo.addItem("Stereo (mix surround down)", "stereo")
        channels = self.channels_combo.findData(db.get_setting("audio_channels", "auto-safe"))
        self.channels_combo.setCurrentIndex(channels if channels >= 0 else 0)
        self.resume_check = QCheckBox("Remember where I stopped watching and pick up there")
        self.resume_check.setChecked(db.get_setting("remember_position", True))
        self.autoplay_check = QCheckBox("Play the next video in the list when one finishes")
        self.autoplay_check.setChecked(db.get_setting("autoplay_next", False))
        self.fullscreen_check = QCheckBox("Start videos in full screen")
        self.fullscreen_check.setChecked(db.get_setting("start_fullscreen", False))
        self.seek_spin = QSpinBox()
        self.seek_spin.setRange(1, 120)
        self.seek_spin.setSuffix(" seconds")
        self.seek_spin.setValue(int(db.get_setting("seek_step", 5)))
        help_text = QLabel("In flat view, drag with the mouse to look around and scroll to zoom. "
                           "R resets the view, E swaps eyes, V switches VR mode.")
        help_text.setObjectName("muted")
        help_text.setWordWrap(True)
        pf.addRow("Audio output", self.audio_combo)
        pf.addRow("Audio channels", self.channels_combo)
        pf.addRow("Arrow keys jump", self.seek_spin)
        pf.addRow(self.resume_check)
        pf.addRow(self.autoplay_check)
        pf.addRow(self.fullscreen_check)
        pf.addRow(self.hw_check)
        pf.addRow("VR videos open in", self.vr_combo)
        pf.addRow("VR eye", self.eye_combo)
        pf.addRow(help_text)
        tabs.addTab(playback, "Playback")

        categories = QWidget()
        cl = QVBoxLayout(categories)
        info = QLabel("One rule per line: <b>Category = pattern</b>. A video gets the category when the pattern "
                      "(a regular expression, not case-sensitive) matches its file or folder name. "
                      "Categories you add by hand are never removed.")
        info.setWordWrap(True)
        cl.addWidget(info)
        self.rules_edit = QPlainTextEdit(db.get_setting("tag_rules", DEFAULT_TAG_RULES))
        self.rules_edit.setFont(QFont("Consolas", 10))
        cl.addWidget(self.rules_edit, 1)
        row = QHBoxLayout()
        reset = QPushButton("Restore defaults")
        reset.clicked.connect(lambda: self.rules_edit.setPlainText(DEFAULT_TAG_RULES))
        self.apply_check = QCheckBox("Apply the rules to every video when saving")
        row.addWidget(reset)
        row.addStretch()
        row.addWidget(self.apply_check)
        cl.addLayout(row)
        tabs.addTab(categories, "Categories")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs, 1)
        layout.addWidget(buttons)
        self._hide_before = self.hide_check.isChecked()
        self._theme_before = self.theme_combo.currentData()
        self.theme_changed = False

    def _load_libraries(self) -> None:
        self.lib_list.clear()
        for lib in self.db.libraries():
            item = QListWidgetItem(lib["path"])
            item.setData(Qt.UserRole, lib["id"])
            self.lib_list.addItem(item)

    def _add_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder with videos")
        if not folder:
            return
        folder = os.path.normpath(folder)
        problem = library_overlap(self.db, folder)
        if problem:
            QMessageBox.warning(self, "Folder already covered", problem)
            return
        self.db.add_library(folder)
        self.libraries_added = True
        self._load_libraries()

    def _relocate_library(self) -> None:
        item = self.lib_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Pick a folder", "Choose one of the folders in the list first.")
            return
        library_id = item.data(Qt.UserRole)
        folder = QFileDialog.getExistingDirectory(self, "Where is this folder now?", item.text())
        if not folder:
            return
        folder = os.path.normpath(folder)
        for lib in self.db.libraries():
            if lib["id"] != library_id and os.path.normcase(lib["path"]) == os.path.normcase(folder):
                QMessageBox.warning(self, "Already in the list", f"{folder} is already one of your folders.")
                return
        moved = self.db.relocate_library(library_id, folder)
        self.libraries_added = True  # make BlueShelf look at the new location
        self._load_libraries()
        QMessageBox.information(
            self, "Folder changed",
            f"{moved} videos now point at {folder}.\n\nTheir ratings, performers and categories are unchanged. "
            "BlueShelf will check the folder again for anything new or missing.")

    def _remove_library(self) -> None:
        item = self.lib_list.currentItem()
        if item is None:
            return
        answer = QMessageBox.question(
            self, "Remove folder",
            f"Stop using {item.text()}?\n\nYour video files are not touched, but BlueShelf forgets the videos in this "
            "folder along with their ratings, performers and categories.\n\nIf the folder has only moved, use "
            "Change folder instead, which keeps all of that.")
        if answer != QMessageBox.Yes:
            return
        self.db.remove_library(item.data(Qt.UserRole))
        self.libraries_removed = True
        self._load_libraries()

    def accept(self) -> None:
        rules = self.rules_edit.toPlainText()
        bad = bad_tag_rule_lines(rules)
        if bad:
            QMessageBox.warning(self, "Check the category rules",
                                "These lines couldn't be read:\n\n" + "\n".join(bad[:10]))
            return
        db = self.db
        db.set_setting("scan_on_start", self.scan_check.isChecked())
        db.set_setting("auto_scan_minutes", self.auto_scan_spin.value())
        db.set_setting("hide_non_adult", self.hide_check.isChecked())
        db.set_setting("delete_mode", "permanent" if self.delete_combo.currentIndex() == 1 else "trash")
        db.set_setting("thumb_position", self.thumb_spin.value() / 100)
        db.set_setting("theme", self.theme_combo.currentData())
        self.theme_changed = self.theme_combo.currentData() != self._theme_before
        db.set_setting("hwdec", self.hw_check.isChecked())
        db.set_setting("audio_device", self.audio_combo.currentData())
        db.set_setting("audio_channels", self.channels_combo.currentData())
        db.set_setting("seek_step", self.seek_spin.value())
        db.set_setting("remember_position", self.resume_check.isChecked())
        db.set_setting("autoplay_next", self.autoplay_check.isChecked())
        db.set_setting("start_fullscreen", self.fullscreen_check.isChecked())
        db.set_setting("vr_mode", self.vr_combo.currentData())
        db.set_setting("vr_eye", self.eye_combo.currentIndex())
        db.set_setting("tag_rules", rules)
        self.reparse_requested = self.apply_check.isChecked() or self.hide_check.isChecked() != self._hide_before
        super().accept()


class TrashDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.changed = False
        self.setWindowTitle("Trash")
        self.resize(900, 500)
        self.summary = QLabel(objectName="muted")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Was in", "Size", "Deleted"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        restore = QPushButton("Restore")
        restore.clicked.connect(self._restore)
        delete = QPushButton("Delete permanently", objectName="danger")
        delete.clicked.connect(lambda: self._delete(self._selected()))
        empty = QPushButton("Empty trash", objectName="danger")
        empty.clicked.connect(lambda: self._delete(self.entries))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(restore)
        row.addWidget(delete)
        row.addStretch()
        row.addWidget(empty)
        row.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Deleted videos wait here until you empty the trash. They still use space on the "
                                "drive until then."))
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summary)
        layout.addLayout(row)
        self.entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        entries: dict[str, dict] = {}
        for r in self.db.trashed():
            entry = entries.setdefault(r["trash_item"], {
                "item": r["trash_item"], "origin": r["trash_origin"],
                "title": r["title"] or os.path.basename(r["path"]), "size": 0, "when": r["trashed_at"], "ids": []})
            entry["size"] += r["size"]
            entry["ids"].append(r["id"])
        for lib in self.db.libraries():
            for path in trash.trash_contents(lib["path"]):
                if path not in entries:
                    try:
                        when = os.path.getmtime(path)
                    except OSError:
                        when = None
                    entries[path] = {"item": path, "origin": None, "title": os.path.basename(path),
                                     "size": trash.path_size(path), "when": when, "ids": []}
        self.entries = list(entries.values())
        self.table.setRowCount(len(self.entries))
        for row, e in enumerate(self.entries):
            where = os.path.dirname(e["origin"]) if e["origin"] else "(not known to BlueShelf)"
            for col, text in enumerate([e["title"], where, fmt_size(e["size"]), fmt_date(e["when"])]):
                self.table.setItem(row, col, QTableWidgetItem(text))
        total = sum(e["size"] for e in self.entries)
        self.summary.setText(f"{len(self.entries)} items · {fmt_size(total)}")

    def _selected(self) -> list[dict]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.entries[r] for r in rows]

    def _restore(self) -> None:
        errors = []
        for e in self._selected():
            if not e["origin"]:
                errors.append(f"{e['title']}: BlueShelf doesn't know where this came from. "
                              f"Move it back yourself from {os.path.dirname(e['item'])}.")
                continue
            try:
                trash.restore(e["item"], e["origin"])
                self.db.restore(e["item"])
                self.changed = True
            except OSError as exc:
                errors.append(f"{e['title']}: {exc.strerror or exc}")
        self._finish(errors)

    def _delete(self, entries: list[dict]) -> None:
        if not entries:
            return
        size = fmt_size(sum(e["size"] for e in entries))
        answer = QMessageBox.question(self, "Delete permanently",
                                      f"Permanently delete {len(entries)} item(s) ({size})? This can't be undone.")
        if answer != QMessageBox.Yes:
            return
        errors = []
        for e in entries:
            try:
                trash.remove_path(e["item"])
            except OSError as exc:
                errors.append(f"{e['title']}: {exc.strerror or exc}")
                continue
            for vid in self.db.purge(e["item"]):
                remove_thumb(vid)
            self.changed = True
        self._finish(errors)

    def _finish(self, errors: list[str]) -> None:
        if errors:
            QMessageBox.warning(self, "Some items couldn't be changed", "\n".join(errors[:15]))
        self._load()


SHORTCUTS = [
    ("Library", [("Double-click / Enter", "Play"), ("Delete", "Delete selected videos"), ("Ctrl+F", "Search"),
                 ("F5", "Look for new videos"), ("Ctrl+D", "Find duplicates"), ("Ctrl+R", "Review names"),
                 ("Ctrl+E", "Cleanup mode: keep or delete one by one"), ("Ctrl+1 / Ctrl+2", "Grid / list"),
                 ("F11", "Full screen")]),
    ("Player", [("Space / click", "Play or pause"), ("← / →", "Back / forward 5 s (Shift 1 s, Ctrl 30 s)"),
                ("↑ / ↓ or wheel", "Volume"), ("F / Enter / double-click", "Full screen"),
                ("Esc", "Leave full screen, then back to library"),
                ("Alt+← / Alt+→", "Previous / next video (same as Page Up / Page Down)"),
                ("Home", "Start over"), ("[ / ] / Backspace", "Slower / faster / normal speed"),
                (", / .", "Frame back / forward"), ("0–5", "Rate the video"), ("M", "Mute"),
                ("Right-click the speaker", "Choose the audio device"),
                ("Delete", "Delete this video (asks first) and play the next"),
                ("Backspace", "Back to normal speed"), ("I", "Playback statistics")]),
    ("VR", [("V", "Switch VR mode"), ("Drag / W A S D", "Look around"), ("Wheel / + / −", "Zoom"),
            ("E", "Swap eye"), ("R", "Reset view")]),
]


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        html = ""
        for section, rows in SHORTCUTS:
            html += f"<h3>{section}</h3><table cellspacing=0>"
            html += "".join(f"<tr><td style='padding:2px 18px 2px 0'><b>{k}</b></td><td>{v}</td></tr>"
                            for k, v in rows)
            html += "</table>"
        label = QLabel(html)
        label.setTextFormat(Qt.RichText)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(buttons)
