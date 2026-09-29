"""Backing up and restoring the work built on top of your videos."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QVBoxLayout)

from blueshelf import backup
from blueshelf.db import Database
from blueshelf.util import fmt_size


class BackupDialog(QDialog):
    def __init__(self, db: Database, restore_callback, parent=None):
        super().__init__(parent)
        self.db = db
        self.restore_callback = restore_callback
        self.setWindowTitle("Backup")
        self.resize(720, 460)

        intro = QLabel(
            "A backup holds everything BlueShelf knows <b>about</b> your videos: performers, categories, studios, "
            "ratings, favourites, where you stopped watching, and the thumbnails. The videos themselves are not "
            "copied, so a backup is small.<br><br>"
            "Keep one somewhere other than this PC, and your work survives a reinstall, a new machine, or a "
            "mistake.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)

        self.auto_check = QCheckBox("Back up automatically once a week")
        self.auto_check.setChecked(bool(db.get_setting("auto_backup", True)))
        self.auto_check.toggled.connect(lambda on: db.set_setting("auto_backup", on))

        self.status = QLabel(objectName="muted")
        self.status.setWordWrap(True)

        backup_btn = QPushButton("Back up now…", objectName="primary")
        backup_btn.clicked.connect(self._backup)
        restore_btn = QPushButton("Restore from a backup…")
        restore_btn.clicked.connect(self._restore)
        folder_btn = QPushButton("Open backup folder")
        folder_btn.clicked.connect(lambda: os.startfile(backup.automatic_dir()))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(backup_btn)
        buttons.addWidget(restore_btn)
        buttons.addWidget(folder_btn)
        buttons.addStretch()
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addSpacing(8)
        layout.addWidget(self.auto_check)
        layout.addWidget(self.status)
        layout.addStretch()
        layout.addLayout(buttons)
        self._refresh()

    def _refresh(self) -> None:
        folder = backup.automatic_dir()
        saved = sorted((os.path.join(folder, name) for name in os.listdir(folder) if name.endswith(backup.SUFFIX)),
                       key=os.path.getmtime, reverse=True)
        if not saved:
            self.status.setText("No automatic backup has been made yet.")
            return
        newest = saved[0]
        when = time.strftime("%d %B %Y at %H:%M", time.localtime(os.path.getmtime(newest)))
        self.status.setText(f"Newest automatic backup: {when} · {fmt_size(os.path.getsize(newest))} · "
                            f"{len(saved)} kept in {folder}")

    def _backup(self) -> None:
        suggested = os.path.join(os.path.expanduser("~"), "Documents", backup.default_name())
        path, _filter = QFileDialog.getSaveFileName(self, "Save a backup", suggested,
                                                    f"BlueShelf backup (*{backup.SUFFIX})")
        if not path:
            return
        try:
            summary = backup.create(path)
        except OSError as exc:
            QMessageBox.warning(self, "Could not write the backup", str(exc))
            return
        QMessageBox.information(
            self, "Backed up",
            f"Saved {fmt_size(summary['size'])} to:\n{path}\n\n"
            f"{summary.get('videos', 0)} videos, {summary.get('performers', 0)} performers, "
            f"{summary.get('thumbs', 0)} thumbnails.")
        self._refresh()

    def _restore(self) -> None:
        start = backup.automatic_dir()
        path, _filter = QFileDialog.getOpenFileName(self, "Choose a backup", start,
                                                    f"BlueShelf backup (*{backup.SUFFIX});;All files (*)")
        if not path:
            return
        details = backup.inspect(path)
        if details is None:
            QMessageBox.warning(self, "Not a backup", "That file is not a BlueShelf backup.")
            return
        when = time.strftime("%d %B %Y at %H:%M", time.localtime(details.get("created", 0)))
        answer = QMessageBox.question(
            self, "Restore this backup?",
            f"Backup from {when}\n\n{details.get('videos', 0)} videos, {details.get('performers', 0)} performers, "
            f"{details.get('thumbs', 0)} thumbnails.\n\n"
            "Your current library data will be replaced. The data being replaced is kept in a folder beside it, "
            "so nothing is lost. BlueShelf will close and reopen.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return
        self.accept()
        self.restore_callback(path)
