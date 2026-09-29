"""Picking videos out of the library the same way the sidebar does.

Browsing to a file works when you know where it is. Choosing "everything by this performer" or
"everything added this month" is how the library is actually thought about, so anything that
takes a list of videos should be able to ask for them that way too.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout)

from blueshelf.db import Database
from blueshelf.util import fmt_time, res_label

# The same groupings as the sidebar, in the same words, so there is nothing new to learn.
VIEWS = [("all", "All videos"), ("recent", "Recently added"), ("favorites", "Favorites"),
         ("unwatched", "Unwatched"), ("vr", "VR"), ("flat", "Not VR"),
         ("continue", "Continue watching")]
BY_PERFORMER, BY_STUDIO, BY_TAG, BY_FOLDER = "performer", "studio", "tag", "library"


class PickDialog(QDialog):
    """Choose videos by performer, studio, category, folder or one of the usual views."""

    def __init__(self, db: Database, already: set[str] | None = None, parent=None,
                 title: str = "Choose videos"):
        super().__init__(parent)
        self.db = db
        self.already = {p.lower() for p in (already or set())}
        self.setWindowTitle(title)
        self.resize(760, 620)

        self.kind = QComboBox()
        for view, label in VIEWS:
            self.kind.addItem(label, ("view", view))
        self.kind.insertSeparator(self.kind.count())
        self.kind.addItem("By performer…", (BY_PERFORMER, None))
        self.kind.addItem("By studio…", (BY_STUDIO, None))
        self.kind.addItem("By category…", (BY_TAG, None))
        self.kind.addItem("By folder…", (BY_FOLDER, None))
        self.kind.currentIndexChanged.connect(self._kind_changed)

        self.which = QComboBox()
        self.which.setMinimumWidth(240)
        self.which.currentIndexChanged.connect(lambda _i: self._fill())
        self.which.hide()

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search titles, performers, categories…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._fill())

        top = QHBoxLayout()
        top.addWidget(QLabel("Show"))
        top.addWidget(self.kind)
        top.addWidget(self.which, 1)
        top.addStretch()

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._count)
        self.list.itemDoubleClicked.connect(lambda _i: self.accept())

        select_all = QPushButton("Select all")
        select_all.clicked.connect(self.list.selectAll)
        select_none = QPushButton("Select none")
        select_none.clicked.connect(self.list.clearSelection)
        self.summary = QLabel()
        row = QHBoxLayout()
        row.addWidget(select_all)
        row.addWidget(select_none)
        row.addStretch()
        row.addWidget(self.summary)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Add these")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.search)
        layout.addWidget(self.list, 1)
        layout.addLayout(row)
        layout.addWidget(buttons)

        self._fill()

    # ------------------------------------------------------------ the two dropdowns

    def _kind_changed(self) -> None:
        picked = self.kind.currentData()
        if not picked:      # the separator
            return
        kind, _value = picked
        self.which.blockSignals(True)
        self.which.clear()
        if kind == BY_PERFORMER:
            for row in self.db.performer_counts(False):
                self.which.addItem(f"{row['name']} ({row['n']})", row["id"])
        elif kind == BY_STUDIO:
            for name, count, key in self.db.studio_counts(False):
                self.which.addItem(f"{name} ({count})", key)
        elif kind == BY_TAG:
            for row in self.db.tag_counts(False):
                self.which.addItem(f"{row['name']} ({row['n']})", row["id"])
        elif kind == BY_FOLDER:
            for row in self.db.library_counts(False):
                self.which.addItem(f"{Path(row['path']).name or row['path']} ({row['n']})",
                                   row["id"])
        self.which.blockSignals(False)
        self.which.setVisible(kind != "view")
        self._fill()

    # ------------------------------------------------------------ the list

    def _query(self) -> list[dict]:
        picked = self.kind.currentData()
        if not picked:
            return []
        kind, value = picked
        chosen = self.which.currentData()
        search = self.search.text().strip()
        if kind == "view":
            return self.db.list_videos(view=value, search=search, sort="added", descending=True)
        if chosen is None:
            return []
        named = {BY_PERFORMER: "performer_id", BY_STUDIO: "studio",
                 BY_TAG: "tag_id", BY_FOLDER: "library_id"}[kind]
        return self.db.list_videos(search=search, sort="title", descending=False,
                                   **{named: chosen})

    def _fill(self) -> None:
        self.list.clear()
        for video in self._query():
            path = video["path"]
            label = video["title"] or Path(path).stem
            extra = " · ".join(bit for bit in (
                res_label(video["width"], video["height"], bool(video["is_vr"])),
                fmt_time(video["duration"])) if bit)
            item = QListWidgetItem(f"{label}    {extra}" if extra else label)
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            if path.lower() in self.already:
                # Already on the list it would be added to - shown, but not offered twice.
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsEnabled)
                item.setText(item.text() + "    (already added)")
            self.list.addItem(item)
        self._count()

    def _count(self) -> None:
        chosen = len(self.list.selectedItems())
        self.summary.setText(f"{chosen} of {self.list.count()} chosen" if self.list.count()
                             else "Nothing here")

    def chosen(self) -> list[str]:
        return [item.data(Qt.UserRole) for item in self.list.selectedItems()]
