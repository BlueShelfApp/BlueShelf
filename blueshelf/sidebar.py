"""Left-hand navigation: library views, performers, studios and categories."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QHeaderView, QLineEdit, QMenu, QMessageBox, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from blueshelf.db import Database, studio_key
from blueshelf.widgets import ask_text

VIEWS = [
    ("all", "All videos"), ("recent", "Recently added"), ("continue", "Continue watching"),
    ("favorites", "Favorites"), ("unwatched", "Unwatched"), ("vr", "VR"), ("flat", "Not VR"),
    ("no_performers", "Missing performers"), ("hidden", "Hidden"),
]
GROUPS = [("view", "LIBRARY"), ("library", "FOLDERS"), ("performer", "PERFORMERS"),
          ("studio", "STUDIOS"), ("tag", "CATEGORIES")]


class Sidebar(QWidget):
    filter_changed = Signal(str, object, str)  # kind, value, label
    data_changed = Signal()
    move_performer = Signal(int, str)  # "put this performer's videos in a folder"

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.show_hidden = False
        self.current: tuple[str, object] = ("view", "all")

        self.search = QLineEdit(placeholderText="Filter performers, studios, categories", clearButtonEnabled=True)
        self.search.textChanged.connect(self._apply_filter)
        self.tree = QTreeWidget(objectName="sidebar")
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(10)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self._on_select)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.search)
        layout.addWidget(self.tree, 1)
        self.setMinimumWidth(200)

        bold = QFont()
        bold.setBold(True)
        bold.setPointSizeF(8.5)
        self.groups: dict[str, QTreeWidgetItem] = {}
        for kind, label in GROUPS:
            item = QTreeWidgetItem(self.tree, [label, ""])
            item.setFlags(Qt.ItemIsEnabled)
            item.setFont(0, bold)
            item.setForeground(0, QColor("#7d7f88"))
            item.setForeground(1, QColor("#5d5f68"))
            item.setExpanded(True)
            self.groups[kind] = item

    @staticmethod
    def _encode(kind: str, value) -> str:
        return f"{kind}|{value}"

    @staticmethod
    def _decode(data: str) -> tuple[str, object]:
        kind, _, raw = data.partition("|")
        return kind, int(raw) if kind in ("performer", "tag", "library") else raw

    def _add(self, parent: QTreeWidgetItem, label: str, count: int, kind: str, value) -> None:
        item = QTreeWidgetItem(parent, [label, str(count)])
        item.setData(0, Qt.UserRole, self._encode(kind, value))
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        item.setForeground(1, QColor("#6f717a"))

    def label_for(self, kind: str, value) -> str:
        item = self._find((kind, value))
        return item.text(0).removeprefix("★ ") if item else "All videos"

    def refresh(self, show_hidden: bool) -> None:
        self.show_hidden = show_hidden
        self.tree.blockSignals(True)
        for group in self.groups.values():
            group.takeChildren()
        counts = self.db.view_counts(show_hidden)
        for key, label in VIEWS:
            if key == "hidden" and not counts.get("hidden"):
                continue
            self._add(self.groups["view"], label, counts.get(key, 0), "view", key)
        folders = self.db.library_counts(show_hidden)
        for row in folders:
            name = os.path.basename(row["path"].rstrip("\\/")) or row["path"]
            self._add(self.groups["library"], name, row["n"], "library", row["id"])
            self.groups["library"].child(self.groups["library"].childCount() - 1).setToolTip(0, row["path"])
        # One folder is the library itself - there is nothing to choose between, so it stays hidden.
        self.groups["library"].setHidden(len(folders) < 2)
        for r in self.db.performer_counts(show_hidden):
            self._add(self.groups["performer"], ("★ " if r["favorite"] else "") + r["name"], r["n"], "performer",
                      r["id"])
        for name, n, key in self.db.studio_counts(show_hidden):
            self._add(self.groups["studio"], name, n, "studio", key)
        for r in self.db.tag_counts(show_hidden):
            self._add(self.groups["tag"], r["name"], r["n"], "tag", r["id"])
        for kind, group in self.groups.items():
            group.setText(1, "" if kind == "view" else str(group.childCount()))

        item = self._find(self.current)
        fell_back = item is None
        if fell_back:
            item = self._find(("view", "all"))
        self.tree.clearSelection()
        if item is not None:
            self.tree.setCurrentItem(item)
            item.setSelected(True)
        self.tree.blockSignals(False)
        self._apply_filter(self.search.text())
        if fell_back:
            self._on_select()

    def _find(self, target: tuple[str, object]) -> QTreeWidgetItem | None:
        group = self.groups.get(target[0])
        if group is None:
            return None
        wanted = self._encode(*target)
        for i in range(group.childCount()):
            if group.child(i).data(0, Qt.UserRole) == wanted:
                return group.child(i)
        return None

    def select(self, kind: str, value) -> None:
        item = self._find((kind, value))
        if item is not None:
            self.tree.setCurrentItem(item)

    def _on_select(self) -> None:
        items = self.tree.selectedItems()
        if not items or not items[0].data(0, Qt.UserRole):
            return
        self.current = self._decode(items[0].data(0, Qt.UserRole))
        self.filter_changed.emit(self.current[0], self.current[1], items[0].text(0).removeprefix("★ "))

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for kind, group in self.groups.items():
            if kind == "view":
                continue
            for i in range(group.childCount()):
                child = group.child(i)
                child.setHidden(bool(needle) and needle not in child.text(0).lower())
            if needle:
                group.setExpanded(True)

    def _menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None or not item.data(0, Qt.UserRole):
            return
        kind, value = self._decode(item.data(0, Qt.UserRole))
        name = item.text(0).removeprefix("★ ")
        menu = QMenu(self)
        if kind == "performer":
            menu.addAction("Rename or merge…").triggered.connect(lambda: self._rename(kind, value, name))
            favorite = item.text(0).startswith("★ ")
            menu.addAction("Remove from favorites" if favorite else "Add to favorite performers").triggered.connect(
                lambda: self._favorite(value, not favorite))
            menu.addSeparator()
            menu.addAction("Move these videos into a folder…").triggered.connect(
                lambda: self.move_performer.emit(int(value), name))
            menu.addSeparator()
            menu.addAction("Remove from all videos…").triggered.connect(lambda: self._delete(kind, value, name))
        elif kind == "studio":
            menu.addAction("Rename or merge…").triggered.connect(lambda: self._rename(kind, value, name))
        elif kind == "tag":
            menu.addAction("Rename or merge…").triggered.connect(lambda: self._rename(kind, value, name))
            menu.addSeparator()
            menu.addAction("Remove from all videos…").triggered.connect(lambda: self._delete(kind, value, name))
        else:
            return
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _rename(self, kind: str, value, name: str) -> None:
        words = {"performer": self.db.performer_names, "studio": self.db.studio_names,
                 "tag": self.db.tag_names}[kind]()
        text = ask_text(self, "Rename", "New name. Type an existing name to merge the two.", words, name)
        if not text or text == name:
            return
        if kind == "performer":
            self.db.rename_performer(value, text)
        elif kind == "studio":
            self.db.rename_studio(value, text)
            if self.current == (kind, value):
                self.current = (kind, studio_key(text))
        else:
            self.db.rename_tag(value, text)
        self.data_changed.emit()

    def _favorite(self, performer_id: int, favorite: bool) -> None:
        self.db.set_performer_favorite(performer_id, favorite)
        self.data_changed.emit()

    def _delete(self, kind: str, value, name: str) -> None:
        what = "performer" if kind == "performer" else "category"
        answer = QMessageBox.question(self, f"Remove {what}",
                                      f"Remove “{name}” from every video? The videos themselves are not deleted.")
        if answer != QMessageBox.Yes:
            return
        if kind == "performer":
            self.db.delete_performer(value)
        else:
            self.db.delete_tag(value)
        self.data_changed.emit()
