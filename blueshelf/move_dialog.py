"""Moving one performer's scenes into a folder - an existing one, or a new one beside it."""
from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout)

from blueshelf import mover, paths
from blueshelf.db import Database


class _Worker(QThread):
    """Moving files can take a while on a network drive, so it happens off the window's thread."""

    step = Signal(int, int, str)
    done = Signal(object)

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job

    def run(self) -> None:
        db = Database(str(paths.db_path()))  # this thread's own connection
        try:
            self.done.emit(self.job(db, lambda at, total, name: self.step.emit(at, total, name)))
        finally:
            db.close()


class MoveDialog(QDialog):
    changed = Signal()

    def __init__(self, db: Database, performer_id: int, performer: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.performer_id = performer_id
        self.performer = performer
        self.worker: _Worker | None = None
        self.last_log = ""
        self.setWindowTitle(f"Move {performer}'s videos into a folder")
        self.resize(760, 620)

        intro = QLabel(
            f"This moves the files themselves into one folder, and the library follows them - "
            f"ratings, performers and everything else stay attached. Subtitles and artwork beside a "
            f"video travel with it.<br><br>"
            f"<b>If these files are still seeding in a torrent client, moving them will break that.</b> "
            f"Every move is written down, so the whole batch can be put back with one button.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)

        self.folder_box = QComboBox()
        self.folder_box.setEditable(True)
        self.folder_box.setMinimumWidth(460)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Move into"))
        folder_row.addWidget(self.folder_box, 1)
        folder_row.addWidget(browse)

        self.skip_check = QCheckBox("Leave scenes that also have other performers where they are")
        self.skip_check.toggled.connect(self._refresh)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.list = QListWidget()
        self.progress = QProgressBar()
        self.progress.hide()

        self.move_btn = QPushButton(f"Move into this folder")
        self.move_btn.clicked.connect(self._move)
        self.undo_btn = QPushButton("Undo the last move")
        self.undo_btn.clicked.connect(self._undo)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.move_btn)
        buttons.addWidget(self.undo_btn)
        buttons.addStretch()
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addSpacing(6)
        layout.addLayout(folder_row)
        layout.addWidget(self.skip_check)
        layout.addWidget(self.summary)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.progress)
        layout.addLayout(buttons)

        self._fill_folders()
        self.folder_box.currentTextChanged.connect(self._refresh)
        self._refresh()

    # ------------------------------------------------------------ setting up

    def _fill_folders(self) -> None:
        """Folders already made for this performer come first; a new one is only the fallback."""
        found = mover.matching_folders(self.db, self.performer)
        for folder in found:
            self.folder_box.addItem(folder)
        libraries = [row["path"] for row in self.db.libraries()]
        for root in libraries:
            fresh = mover.folder_for(root, self.performer)
            if fresh not in found:
                self.folder_box.addItem(fresh)
        if found:
            self.folder_box.setCurrentIndex(0)

    def _browse(self) -> None:
        start = self.folder_box.currentText() or (self.db.libraries()[0]["path"] if self.db.libraries() else "")
        chosen = QFileDialog.getExistingDirectory(self, "Choose the folder to move into", start)
        if chosen:
            chosen = chosen.replace("/", "\\")
            if self.folder_box.findText(chosen) < 0:
                self.folder_box.insertItem(0, chosen)
            self.folder_box.setCurrentIndex(self.folder_box.findText(chosen))

    # ------------------------------------------------------------ what would happen

    def moves(self) -> list[mover.Move]:
        folder = self.folder_box.currentText().strip()
        if not folder:
            return []
        return mover.plan(self.db, self.performer_id, folder, skip_shared=self.skip_check.isChecked())

    def _refresh(self) -> None:
        folder = self.folder_box.currentText().strip()
        moves = self.moves()
        self.list.clear()
        for move in moves:
            label = move.name
            if move.shared:
                label += f"    — also with {', '.join(move.others)}"
            item = QListWidgetItem(label)
            item.setToolTip(move.source)
            self.list.addItem(item)
        shared = sum(1 for move in moves if move.shared)
        if not folder:
            self.summary.setText("Choose a folder to move into.")
        elif not moves:
            self.summary.setText("Nothing to move: every one of these videos is already in that folder.")
        else:
            exists = "" if mover.library_root(self.db, folder) else \
                "  <b>That folder is outside your library folders</b>, so the videos will leave the library."
            note = f"  {shared} of them also have other performers and can only live in one folder." if shared else ""
            self.summary.setText(f"{len(moves)} videos would move into <b>{folder}</b>.{note}{exists}")
        self.move_btn.setEnabled(bool(moves))
        self.undo_btn.setEnabled(bool(self.last_log or mover.recent_logs(1)))

    # ------------------------------------------------------------ doing it

    def _busy(self, busy: bool) -> None:
        self.move_btn.setEnabled(not busy)
        self.undo_btn.setEnabled(not busy)
        self.folder_box.setEnabled(not busy)
        self.progress.setVisible(busy)

    def _move(self) -> None:
        moves = self.moves()
        if not moves:
            return
        folder = self.folder_box.currentText().strip()
        question = (f"Move {len(moves)} videos into:\n{folder}\n\n"
                    "The files themselves are moved. Anything still seeding from them will stop.")
        if QMessageBox.question(self, "Move these videos?", question,
                                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self._busy(True)
        self.progress.setRange(0, len(moves))
        self.worker = _Worker(lambda db, report: mover.apply(db, moves, report), self)
        self.worker.step.connect(self._step)
        self.worker.done.connect(self._moved)
        self.worker.start()

    def _step(self, at: int, total: int, name: str) -> None:
        self.progress.setValue(at)
        self.summary.setText(f"Moving {at} of {total}: {name}")

    def _moved(self, result: mover.Result) -> None:
        self._busy(False)
        self.last_log = result.log
        if result.failed:
            trouble = "\n".join(f"{move.name}: {why}" for move, why in result.failed[:8])
            QMessageBox.warning(self, "Some did not move",
                                f"{len(result.moved)} moved, {len(result.failed)} did not:\n\n{trouble}")
        self.changed.emit()
        self._refresh()
        if result.moved and not result.failed:
            self.summary.setText(f"Moved {len(result.moved)} videos into "
                                 f"<b>{self.folder_box.currentText().strip()}</b>. Undo is below if that was wrong.")

    def _undo(self) -> None:
        log = self.last_log or (mover.recent_logs(1) or [""])[0]
        if not log:
            return
        self._busy(True)
        self.progress.setRange(0, 0)
        self.worker = _Worker(lambda db, _report: mover.undo(db, log), self)
        self.worker.done.connect(self._undone)
        self.worker.start()

    def _undone(self, outcome) -> None:
        back, problems = outcome
        self._busy(False)
        self.last_log = ""
        self.changed.emit()
        self._refresh()
        if problems:
            QMessageBox.warning(self, "Not everything went back",
                                f"{back} put back.\n\n" + "\n".join(problems[:8]))
        else:
            self.summary.setText(f"Put {back} videos back where they were.")

    def closeEvent(self, event) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.wait(30000)
        super().closeEvent(event)
