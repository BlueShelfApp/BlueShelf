"""Build a folder tree of shortcuts to the library, so it can be browsed outside BlueShelf."""
from __future__ import annotations

import os

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QProgressBar, QPushButton, QVBoxLayout)

from blueshelf import paths
from blueshelf.db import Database
from blueshelf.organize import (NO_PERFORMER, STRUCTURES, ShortcutPlan, check_root, clean_tree, plan_tree, read_manifest,
                            write_tree)
from blueshelf.parser import VIDEO_EXTS


def videos_for_plan(db: Database, include_hidden: bool) -> list[dict]:
    rows = db.list_videos(show_hidden=include_hidden, sort="title", descending=False)
    for row in rows:
        row["performers"] = [p.strip() for p in (row.get("performer_names") or "").split(",") if p.strip()]
    return rows


class _BuildThread(QThread):
    finished_build = Signal(int, list, str)

    def __init__(self, plan: ShortcutPlan, root: str, structure: str, parent=None):
        super().__init__(parent)
        self.plan, self.root, self.structure = plan, root, structure

    def run(self) -> None:
        try:
            made, errors = write_tree(self.plan, self.root, self.structure, str(paths.data_dir()))
        except Exception as exc:
            self.finished_build.emit(0, [], str(exc))
            return
        self.finished_build.emit(made, errors, "")


class OrganizeDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.thread: _BuildThread | None = None
        self.setWindowTitle("Build shortcut folders")
        self.resize(720, 420)

        intro = QLabel(
            "This builds a folder tree you can browse in Explorer, where every video is a shortcut to the real "
            "file. <b>Nothing is moved or renamed</b>, so downloads that are still seeding keep working. A scene "
            "with two performers gets a shortcut in both folders. Delete the tree whenever you like, or press "
            "Build again to refresh it.")
        intro.setWordWrap(True)

        self.folder_edit = QLineEdit(db.get_setting("shortcut_root", ""))
        self.folder_edit.setPlaceholderText("Choose an empty folder for the shortcuts")
        self.folder_edit.textChanged.connect(self._update_preview)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Folder"))
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)

        self.structure_combo = QComboBox()
        for key, label in STRUCTURES.items():
            self.structure_combo.addItem(label, key)
        saved = self.structure_combo.findData(db.get_setting("shortcut_structure", "performer"))
        self.structure_combo.setCurrentIndex(saved if saved >= 0 else 0)
        self.structure_combo.currentIndexChanged.connect(self._update_preview)
        self.hidden_check = QCheckBox("Include hidden videos (movies and TV shows)")
        self.hidden_check.setChecked(bool(db.get_setting("shortcut_hidden", False)))
        self.hidden_check.toggled.connect(self._update_preview)
        structure_row = QHBoxLayout()
        structure_row.addWidget(QLabel("Folders"))
        structure_row.addWidget(self.structure_combo, 1)

        self.preview = QLabel(objectName="muted")
        self.preview.setWordWrap(True)
        self.progress = QProgressBar(textVisible=False)
        self.progress.hide()
        self.status = QLabel(objectName="muted")
        self.status.setWordWrap(True)

        self.build_btn = QPushButton("Build", objectName="primary")
        self.build_btn.clicked.connect(self._build)
        self.open_btn = QPushButton("Open folder")
        self.open_btn.clicked.connect(self._open_folder)
        self.open_btn.setEnabled(bool(self.folder_edit.text()))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.build_btn)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addSpacing(6)
        layout.addLayout(folder_row)
        layout.addLayout(structure_row)
        layout.addWidget(self.hidden_check)
        layout.addSpacing(6)
        layout.addWidget(self.preview)
        layout.addWidget(self.progress)
        layout.addWidget(self.status, 1)
        layout.addLayout(buttons)
        self._update_preview()

    # ------------------------------------------------------------ helpers

    def _structure(self) -> str:
        return self.structure_combo.currentData()

    def _plan(self) -> ShortcutPlan:
        return plan_tree(videos_for_plan(self.db, self.hidden_check.isChecked()), self._structure())

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder for the shortcuts",
                                                  self.folder_edit.text() or "")
        if folder:
            self.folder_edit.setText(os.path.normpath(folder))

    def _open_folder(self) -> None:
        folder = self.folder_edit.text()
        if os.path.isdir(folder):
            os.startfile(folder)
        else:
            QMessageBox.information(self, "Not built yet", "That folder doesn't exist yet. Press Build first.")

    def _update_preview(self) -> None:
        plan = self._plan()
        parts = [f"{plan.count} shortcuts in {len(plan.folders)} folders"]
        if plan.unsorted:
            parts.append(f"{plan.unsorted} videos have no performer yet and go into “{NO_PERFORMER}”")
        existing = read_manifest(self.folder_edit.text()) if self.folder_edit.text() else {}
        if existing:
            parts.append(f"the {len(existing.get('links', []))} shortcuts built here last time will be replaced")
        self.preview.setText(". ".join(parts) + ".")
        self.open_btn.setEnabled(os.path.isdir(self.folder_edit.text()))

    # ------------------------------------------------------------ building

    def _build(self) -> None:
        root = os.path.normpath(self.folder_edit.text().strip()) if self.folder_edit.text().strip() else ""
        problem = check_root(root, VIDEO_EXTS)
        if problem:
            QMessageBox.warning(self, "Choose another folder", problem)
            return
        plan = self._plan()
        if not plan.count:
            QMessageBox.information(self, "Nothing to do", "There are no videos to make shortcuts for.")
            return
        self.db.set_setting("shortcut_root", root)
        self.db.set_setting("shortcut_structure", self._structure())
        self.db.set_setting("shortcut_hidden", self.hidden_check.isChecked())
        removed = clean_tree(root) if read_manifest(root) else 0
        self.status.setText(f"Removed {removed} old shortcuts…" if removed else "Building…")
        self.build_btn.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.thread = _BuildThread(plan, root, self._structure(), self)
        self.thread.finished_build.connect(self._done)
        self.thread.start()

    def _done(self, made: int, errors: list[str], failure: str) -> None:
        self.progress.hide()
        self.build_btn.setEnabled(True)
        self._update_preview()
        if failure:
            self.status.setText("")
            QMessageBox.warning(self, "Could not build the shortcuts", failure)
            return
        message = f"Made {made} shortcuts in {self.folder_edit.text()}."
        if errors:
            message += f" {len(errors)} could not be created:\n" + "\n".join(errors[:5])
        self.status.setText(message)
        self.open_btn.setEnabled(True)

    def closeEvent(self, event) -> None:
        if self.thread is not None and self.thread.isRunning():
            self.thread.wait(20000)
        super().closeEvent(event)
