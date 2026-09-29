"""Bringing a library in from another program, with a look at what will happen first.

Nothing is changed until the person presses the last button, and even then only BlueShelf's
own library is written to - never their films.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from blueshelf import importing, sidecars
from blueshelf.db import Database
from blueshelf.importing import Match, apply_plan

SOURCES = [
    ("stash", "Stash", "Its library file, usually stash-go.sqlite", "file"),
    ("xbvr", "XBVR", "Its library file, usually main.db", "file"),
    ("nfo", "NFO files beside the films", "The folder holding them - Kodi, Jellyfin, Emby and Plex write these", "folder"),
    ("folders", "Folder and file names only", "For a library kept in no program at all", "folder"),
]


class _Work(QThread):
    """Reading and matching, off the window's own thread: both can take a while on a big library."""

    said = Signal(str)
    ready = Signal(object, object)          # the plan, and the matcher that made it
    failed = Signal(str)

    def __init__(self, db: Database, kind: str, where: str, roots, by_content: bool, parent=None):
        super().__init__(parent)
        self.db, self.kind, self.where, self.roots, self.by_content = db, kind, where, roots, by_content

    def run(self) -> None:
        try:
            say = self.said.emit
            if self.kind == "stash":
                incoming = importing.read_stash(self.where, say)
            elif self.kind == "xbvr":
                incoming = importing.read_xbvr(self.where, say)
            elif self.kind == "nfo":
                incoming = importing.read_nfos([self.where], say)
            else:
                incoming = importing.read_folders([self.where], say)
            if not incoming:
                self.failed.emit("Nothing was found to bring in.")
                return
            match = Match(self.db)
            for theirs, ours in self.roots:
                match.remap(theirs, ours)
            self.ready.emit(match.plan(incoming, self.by_content, say), match)
        except Exception as exc:  # noqa: BLE001 - shown to the person
            self.failed.emit(str(exc))


class _Bringing(QThread):
    said = Signal(str)
    done = Signal(dict)

    def __init__(self, db: Database, plan, overwrite: bool, parent=None):
        super().__init__(parent)
        self.db, self.plan, self.overwrite = db, plan, overwrite

    def run(self) -> None:
        self.done.emit(apply_plan(self.db, self.plan, self.overwrite, self.said.emit))


class ImportDialog(QDialog):
    """Point it at another program's library and it does the rest."""

    changed = Signal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.plan = None
        self.match = None
        self.roots: list[tuple[str, str]] = []
        self.worker: QThread | None = None
        self.setWindowTitle("Bring in a library")
        self.resize(760, 620)

        note = QLabel(
            "Bring in what you organised somewhere else - performers, studios, tags, titles, "
            "ratings, how far through you were, and marked scenes. <b>Your videos are never "
            "touched</b>: nothing is moved, renamed or written to, and this only adds to "
            "BlueShelf's own library. You can look at what will happen before anything does, and "
            "you can run it again safely.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)

        self.source = QComboBox()
        for key, label, _hint, _kind in SOURCES:
            self.source.addItem(label, key)
        self.source.currentIndexChanged.connect(self._source_changed)
        self.where = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        where_row = QHBoxLayout()
        where_row.addWidget(QLabel("Where"))
        where_row.addWidget(self.where, 1)
        where_row.addWidget(browse)
        self.hint = QLabel()
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)

        self.by_content = QCheckBox("Find films that were moved or renamed, by looking at the films themselves")
        self.by_content.setChecked(True)
        self.by_content.setToolTip("Slower the first time - it reads a little of each film - but it "
                                   "finds films whose paths have changed since the other program saw them.")
        self.look_btn = QPushButton("Look at it")
        self.look_btn.setObjectName("primary")
        self.look_btn.clicked.connect(self._look)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.folders = QListWidget()
        self.folders.setMinimumHeight(90)
        self.folders.setToolTip("Where their folders look like yours. Tick the ones that are right "
                                "and look again.")
        self.missing = QListWidget()
        self.missing.setMinimumHeight(90)

        self.overwrite = QCheckBox("Replace what I have typed here as well")
        self.overwrite.setToolTip("Off, anything you typed in BlueShelf is kept and only gaps are "
                                  "filled. On, the library being brought in wins. Either way, your "
                                  "performers and tags are added to, never removed.")
        self.bring_btn = QPushButton("Bring it in")
        self.bring_btn.setObjectName("primary")
        self.bring_btn.setEnabled(False)
        self.bring_btn.clicked.connect(self._bring)

        self.stage = QLabel()
        self.stage.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.hide()

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        row = QHBoxLayout()
        row.addWidget(QLabel("From"))
        row.addWidget(self.source, 1)
        layout.addLayout(row)
        layout.addLayout(where_row)
        layout.addWidget(self.hint)
        layout.addWidget(self.by_content)
        layout.addWidget(self.look_btn)
        layout.addWidget(self.summary)
        layout.addWidget(QLabel("Their folders and yours - tick any that are right, then look again:"))
        layout.addWidget(self.folders)
        layout.addWidget(QLabel("Films they had that are not here:"))
        layout.addWidget(self.missing, 1)
        layout.addWidget(self.overwrite)
        layout.addWidget(self.bring_btn)
        layout.addWidget(self.stage)
        layout.addWidget(self.bar)

        self._source_changed()
        found = importing.likely_places()
        if found:
            name, where = found[0]
            at = self.source.findData("stash" if name == "Stash" else "xbvr")
            if at >= 0:
                self.source.setCurrentIndex(at)
            self.where.setText(where)
            self.stage.setText(f"Found {name} on this computer. Press Look at it.")

    # ------------------------------------------------------------ choosing

    def _source_changed(self) -> None:
        kind = self.source.currentData()
        hint = next(h for key, _l, h, _k in SOURCES if key == kind)
        self.hint.setText(hint)
        self.where.clear()
        self._forget_plan()

    def _browse(self) -> None:
        kind = next(k for key, _l, _h, k in SOURCES if key == self.source.currentData())
        if kind == "file":
            where, _ = QFileDialog.getOpenFileName(self, "Find their library", "",
                                                   "Library files (*.sqlite *.db);;All files (*)")
        else:
            where = QFileDialog.getExistingDirectory(self, "Choose the folder their films are in")
        if where:
            self.where.setText(where.replace("/", "\\"))
            self._forget_plan()

    def _forget_plan(self) -> None:
        self.plan = self.match = None
        self.bring_btn.setEnabled(False)
        self.summary.clear()
        self.folders.clear()
        self.missing.clear()

    # ------------------------------------------------------------ looking

    def _look(self) -> None:
        where = self.where.text().strip()
        if not where:
            self.stage.setText("Choose where their library is first.")
            return
        self.roots = [(item.data(Qt.UserRole)[0], item.data(Qt.UserRole)[1])
                      for row in range(self.folders.count())
                      for item in [self.folders.item(row)]
                      if item.checkState() == Qt.Checked and item.data(Qt.UserRole)]
        self._forget_plan()
        self._busy(True)
        self.worker = _Work(self.db, self.source.currentData(), where, self.roots,
                            self.by_content.isChecked(), self)
        self.worker.said.connect(self.stage.setText)
        self.worker.ready.connect(self._looked)
        self.worker.failed.connect(self._went_wrong)
        self.worker.finished.connect(lambda: self._busy(False))
        self.worker.start()

    def _looked(self, plan, match) -> None:
        self.plan, self.match = plan, match
        self.summary.setText(plan.summary())
        self.bring_btn.setEnabled(bool(plan.matched))
        self.folders.clear()
        for theirs, ours, count in match.suggest_roots(plan.missing):
            item = QListWidgetItem(f"{theirs}   →   {ours}      ({count} films)")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, (theirs, ours))
            self.folders.addItem(item)
        if self.folders.count():
            self.folders.addItem("Tick these and press Look at it again to match those films.")
        self.missing.clear()
        for one in plan.missing[:400]:
            self.missing.addItem(f"{one.title or Path(one.path).name}    {one.path}")
        if len(plan.missing) > 400:
            self.missing.addItem(f"…and {len(plan.missing) - 400} more")

    def _went_wrong(self, why: str) -> None:
        self.stage.setText(why)
        QMessageBox.warning(self, "Bring in a library", why)

    # ------------------------------------------------------------ doing it

    def _bring(self) -> None:
        if not self.plan:
            return
        many = len(self.plan.matched)
        if QMessageBox.question(
                self, "Bring in a library",
                f"Add what they organised to {many} film{'s' if many != 1 else ''}?\n\n"
                f"Your videos are not touched. This can be run again.",
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self._busy(True)
        self.bring_btn.setEnabled(False)
        self.worker = _Bringing(self.db, self.plan, self.overwrite.isChecked(), self)
        self.worker.said.connect(self.stage.setText)
        self.worker.done.connect(self._brought)
        self.worker.finished.connect(lambda: self._busy(False))
        self.worker.start()

    def _brought(self, counted: dict) -> None:
        self.stage.setText(
            f"Done. {counted['films']} films, {counted['performers']} performers, "
            f"{counted['tags']} tags, {counted['scenes']} scenes, {counted['ratings']} ratings."
            + ("  Their sidecar files can be written beside your films from Settings."
               if not sidecars.wanted(self.db) else ""))
        self.changed.emit()

    def _busy(self, on: bool) -> None:
        self.bar.setVisible(on)
        self.look_btn.setEnabled(not on)
        self.source.setEnabled(not on)
        self.where.setEnabled(not on)
        if not on and self.plan:
            self.bring_btn.setEnabled(bool(self.plan.matched))

    def shut_down(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.wait(30000)

    def closeEvent(self, event) -> None:
        self.shut_down()
        super().closeEvent(event)
