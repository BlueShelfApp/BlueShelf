"""Window for finding and cleaning up duplicate videos."""
from __future__ import annotations

import html
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from blueshelf.db import Database
from blueshelf.dupes import DupeScanThread
from blueshelf.library import display_title
from blueshelf.theme import ACCENT, COLORS
from blueshelf.util import breakable_html, fmt_bitrate, fmt_size, fmt_time, res_label


class DupesDialog(QDialog):
    def __init__(self, db: Database, cache, delete_fn, play_fn, parent=None):
        super().__init__(parent)
        self.db = db
        self.cache = cache
        self.delete_fn = delete_fn
        self.play_fn = play_fn
        self.thread: DupeScanThread | None = None
        self.rows: list[dict] = []
        self.cards: list[QFrame] = []
        self.setWindowTitle("Find duplicates")
        self.resize(1060, 780)

        intro = QLabel(
            "BlueShelf looks for copies of the same video. <b>Identical files</b> are matched by their contents. With "
            "frame matching on, it also compares pictures from across each video to catch <b>the same scene saved "
            "at a different resolution or quality</b>. In each group the best-quality copy is kept and the others "
            "are ticked for deletion. Check the ticks before you delete.")
        intro.setWordWrap(True)
        self.visual_check = QCheckBox("Match re-encoded copies by comparing frames (slow the first time, "
                                      "remembered afterwards)")
        self.visual_check.setChecked(True)
        self.start_btn = QPushButton("Find duplicates", objectName="primary")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.hide()
        self.progress = QProgressBar(textVisible=False)
        self.progress.hide()
        self.status = QLabel(objectName="muted")

        controls = QHBoxLayout()
        controls.addWidget(self.visual_check, 1)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)

        self.results = QWidget()
        self.results_layout = QVBoxLayout(self.results)
        self.results_layout.setSpacing(10)
        self.results_layout.addStretch()
        scroll = QScrollArea(widgetResizable=True)
        scroll.setWidget(self.results)

        self.summary = QLabel(objectName="muted")
        self.delete_btn = QPushButton("Delete ticked copies", objectName="danger")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self.delete_checked)
        bottom = QHBoxLayout()
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.delete_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(controls)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addWidget(scroll, 1)
        layout.addLayout(bottom)

    def start(self) -> None:
        self._clear()
        self.thread = DupeScanThread(self.visual_check.isChecked(), self)
        self.thread.progress.connect(self._progress)
        self.thread.finished_groups.connect(self._finished)
        self.start_btn.hide()
        self.stop_btn.show()
        self.progress.show()
        self.progress.setRange(0, 0)
        self.status.setText("Starting…")
        self.thread.start()

    def _progress(self, label: str, done: int, total: int) -> None:
        self.status.setText(f"{label}… {done} of {total}")
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)

    def _stop(self) -> None:
        if self.thread is not None:
            self.thread.stop()
            self.status.setText("Stopping…")

    def _finished(self, groups: list[dict]) -> None:
        self.start_btn.show()
        self.stop_btn.hide()
        self.progress.hide()
        self.show_groups(groups)

    def _clear(self) -> None:
        for card in self.cards:
            card.deleteLater()
        self.cards = []
        self.rows = []
        self._update_summary()

    def show_groups(self, groups: list[dict]) -> None:
        self._clear()
        dpr = self.devicePixelRatioF()
        for group in groups:
            videos = [v for v in (self.db.get_video(i) for i in group["ids"]) if v and not v["trashed"]]
            if len(videos) < 2:
                continue
            card = QFrame(objectName="card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 10, 14, 10)
            kind = "Identical files" if group["kind"] == "identical" else "Same video, different versions"
            cl.addWidget(QLabel(f"<b>{kind}</b> · {len(videos)} copies · "
                                f"{fmt_size(sum(v['size'] for v in videos))}"))
            for i, v in enumerate(videos):
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 4, 0, 4)
                check = QCheckBox()
                check.setChecked(i > 0)
                check.toggled.connect(self._update_summary)
                thumb = QLabel()
                thumb.setFixedSize(160, 90)
                pix = self.cache.scaled(v, 160, 90, dpr)
                if pix is not None:
                    thumb.setPixmap(pix)
                else:
                    thumb.setStyleSheet(f"background: {COLORS['thumb_bg']}; border-radius: 4px;")
                quality = res_label(v["width"], v["height"], bool(v["is_vr"]))
                dims = f"{v['width']}×{v['height']}" if v["width"] else "size unknown"
                facts = [f"{quality} ({dims})" if quality else dims, fmt_bitrate(v["bitrate"]),
                         fmt_time(v["duration"]) if v["duration"] else "", fmt_size(v["size"])]
                keep = f" &nbsp;<span style='color:{ACCENT}'>best quality</span>" if i == 0 else ""
                info = QLabel(f"<b>{html.escape(display_title(v))}</b>{keep}<br>"
                              f"<span style='color:#a0a2ab'>{' · '.join(f for f in facts if f)}</span><br>"
                              f"<span style='color:#6c6e77'>{breakable_html(v['path'])}</span>")
                info.setTextFormat(Qt.RichText)
                info.setWordWrap(True)
                play = QPushButton("Play")
                play.clicked.connect(lambda _=False, vid=v["id"]: self.play_fn(vid))
                rl.addWidget(check)
                rl.addWidget(thumb)
                rl.addWidget(info, 1)
                rl.addWidget(play)
                cl.addWidget(row)
                self.rows.append({"widget": row, "check": check, "video": v, "card": card, "deleted": False})
            self.results_layout.insertWidget(self.results_layout.count() - 1, card)
            self.cards.append(card)
        if self.cards:
            self.status.setText(f"Found {len(self.cards)} groups of duplicates.")
        else:
            self.status.setText("No duplicates found.")
        self._update_summary()

    def _live_rows(self) -> list[dict]:
        return [r for r in self.rows if not r["deleted"]]

    def _update_summary(self, *_args) -> None:
        ticked = [r for r in self._live_rows() if r["check"].isChecked()]
        size = sum(r["video"]["size"] for r in ticked)
        self.delete_btn.setEnabled(bool(ticked))
        self.delete_btn.setText(f"Delete {len(ticked)} ticked copies ({fmt_size(size)})" if ticked
                                else "Delete ticked copies")
        whole = 0
        for card in self.cards:
            live = [r for r in self._live_rows() if r["card"] is card]
            if live and all(r["check"].isChecked() for r in live):
                whole += 1
        self.summary.setText(f"Careful: every copy is ticked in {whole} group(s)." if whole else "")

    def delete_checked(self) -> None:
        ticked = [r for r in self._live_rows() if r["check"].isChecked()]
        if not ticked or not self.delete_fn([r["video"]["id"] for r in ticked]):
            return
        for r in ticked:
            current = self.db.get_video(r["video"]["id"])
            if current is None or current["trashed"]:
                r["deleted"] = True
                r["widget"].hide()
        for card in self.cards:
            if len([r for r in self._live_rows() if r["card"] is card]) < 2:
                card.hide()
        self._update_summary()

    def closeEvent(self, event) -> None:
        if self.thread is not None and self.thread.isRunning():
            self.thread.stop()
            self.thread.wait(15000)
        super().closeEvent(event)
