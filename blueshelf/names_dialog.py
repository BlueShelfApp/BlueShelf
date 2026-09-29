"""Review names: merge spelling variants and drop names that are really part of a title."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from blueshelf.db import Database
from blueshelf.suggestions import MergeSuggestion, StraySuggestion, merge_suggestions, stray_suggestions
from blueshelf.theme import ACCENT


class NameReviewDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.changed = False
        self.rows: list[dict] = []
        self.setWindowTitle("Review names")
        self.resize(940, 720)

        intro = QLabel(
            "BlueShelf only merges names automatically when it is sure, so close spellings of one person can end up "
            "as two. These are the names that look wrong. Nothing changes until you press Apply, and "
            "<b>Not the same</b> hides a suggestion for good.")
        intro.setWordWrap(True)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setSpacing(6)
        self.body_layout.addStretch()
        scroll = QScrollArea(widgetResizable=True)
        scroll.setWidget(self.body)

        self.summary = QLabel(objectName="muted")
        self.apply_btn = QPushButton("Apply selected", objectName="primary")
        self.apply_btn.clicked.connect(self._apply)
        select_all = QPushButton("Select all")
        select_all.clicked.connect(lambda: self._set_all(True))
        select_none = QPushButton("Select none")
        select_none.clicked.connect(lambda: self._set_all(False))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.summary, 1)
        buttons.addWidget(select_all)
        buttons.addWidget(select_none)
        buttons.addWidget(self.apply_btn)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(scroll, 1)
        layout.addLayout(buttons)
        self._load()

    # ------------------------------------------------------------ building the list

    def _ignored_pairs(self) -> set[tuple[str, ...]]:
        return {tuple(pair) for pair in self.db.get_setting("ignored_name_pairs", [])}

    def _ignored_strays(self) -> set[str]:
        return set(self.db.get_setting("ignored_stray_names", []))

    def _clear(self) -> None:
        while self.body_layout.count() > 1:
            item = self.body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.rows = []

    def _load(self) -> None:
        self._clear()
        ignored = self._ignored_pairs()
        performers = [(r["id"], r["name"], r["n"]) for r in self.db.performer_counts(True)]
        performer_merges = merge_suggestions(performers, ignored)
        studios = [(key, name, count) for name, count, key in self.db.studio_counts(True)]
        studio_merges = merge_suggestions(studios, ignored)
        titles = self.db.performer_video_titles()
        strays = stray_suggestions([(pid, name, count, titles.get(pid, [])) for pid, name, count in performers],
                                   self._ignored_strays())

        if performer_merges:
            self._heading("Performers that look like the same person")
            for suggestion in performer_merges:
                self._merge_row(suggestion, "performer")
        if strays:
            self._heading("Names that look like part of a title, not a person")
            for suggestion in strays:
                self._stray_row(suggestion)
        if studio_merges:
            self._heading("Studios that look like the same studio")
            for suggestion in studio_merges:
                self._merge_row(suggestion, "studio")
        if not self.rows:
            self._heading("Nothing looks wrong. Every name is either clearly different or already merged.")
        self._update_summary()

    def _heading(self, text: str) -> None:
        label = QLabel(text, objectName="heading")
        label.setWordWrap(True)
        label.setContentsMargins(0, 10, 0, 2)
        self.body_layout.insertWidget(self.body_layout.count() - 1, label)

    def _card(self) -> tuple[QFrame, QHBoxLayout]:
        card = QFrame(objectName="card")
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 8, 12, 8)
        self.body_layout.insertWidget(self.body_layout.count() - 1, card)
        return card, row

    def _merge_row(self, suggestion: MergeSuggestion, kind: str) -> None:
        card, row = self._card()
        check = QCheckBox()
        check.setChecked(suggestion.score >= 0.75)
        check.toggled.connect(self._update_summary)
        label = QLabel()
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        entry = {"kind": kind, "type": "merge", "suggestion": suggestion, "check": check, "label": label,
                 "card": card}
        self._set_merge_text(entry)
        swap = QPushButton("Other way round")
        swap.setToolTip("Keep the other spelling instead")
        swap.clicked.connect(lambda: self._swap(entry))
        ignore = QPushButton("Not the same")
        ignore.clicked.connect(lambda: self._ignore_pair(entry))
        row.addWidget(check)
        row.addWidget(label, 1)
        row.addWidget(swap)
        row.addWidget(ignore)
        self.rows.append(entry)

    def _set_merge_text(self, entry: dict) -> None:
        s: MergeSuggestion = entry["suggestion"]
        videos = f"{s.merge_count} video" + ("s" if s.merge_count != 1 else "")
        entry["label"].setText(
            f"Merge <b>{html.escape(s.merge_name)}</b> ({videos}) into "
            f"<b style='color:{ACCENT}'>{html.escape(s.keep_name)}</b> ({s.keep_count})"
            f"<br><span style='color:#8d8f98'>{s.reason}</span>")

    def _stray_row(self, suggestion: StraySuggestion) -> None:
        card, row = self._card()
        check = QCheckBox()
        check.setChecked(True)
        check.toggled.connect(self._update_summary)
        videos = f"{suggestion.count} video" + ("s" if suggestion.count != 1 else "")
        label = QLabel(f"Remove <b>{html.escape(suggestion.name)}</b> from {videos}"
                       f"<br><span style='color:#8d8f98'>{suggestion.reason}: "
                       f"“{html.escape(suggestion.example_title)}”</span>")
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        entry = {"kind": "performer", "type": "stray", "suggestion": suggestion, "check": check, "card": card}
        ignore = QPushButton("Keep it")
        ignore.clicked.connect(lambda: self._ignore_stray(entry))
        row.addWidget(check)
        row.addWidget(label, 1)
        row.addWidget(ignore)
        self.rows.append(entry)

    # ------------------------------------------------------------ actions

    def _swap(self, entry: dict) -> None:
        entry["suggestion"] = entry["suggestion"].swapped()
        self._set_merge_text(entry)

    def _ignore_pair(self, entry: dict) -> None:
        pairs = self.db.get_setting("ignored_name_pairs", [])
        pairs.append(entry["suggestion"].pair_key())
        self.db.set_setting("ignored_name_pairs", pairs)
        self._load()

    def _ignore_stray(self, entry: dict) -> None:
        from blueshelf.parser import performer_key
        names = self.db.get_setting("ignored_stray_names", [])
        names.append(performer_key(entry["suggestion"].name))
        self.db.set_setting("ignored_stray_names", names)
        self._load()

    def _set_all(self, checked: bool) -> None:
        for entry in self.rows:
            entry["check"].setChecked(checked)

    def _selected(self) -> list[dict]:
        return [entry for entry in self.rows if entry["check"].isChecked()]

    def _update_summary(self, *_args) -> None:
        count = len(self._selected())
        self.apply_btn.setEnabled(bool(count))
        self.summary.setText(f"{len(self.rows)} suggestions, {count} selected" if self.rows else "")

    def _apply(self) -> None:
        for entry in self._selected():
            suggestion = entry["suggestion"]
            if entry["type"] == "stray":
                self.db.delete_performer(suggestion.performer_id)
            elif entry["kind"] == "performer":
                self.db.rename_performer(suggestion.merge_id, suggestion.keep_name)
            else:
                self.db.rename_studio(suggestion.merge_id, suggestion.keep_name)
            self.changed = True
        self._load()
