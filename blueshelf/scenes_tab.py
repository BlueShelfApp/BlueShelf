"""Making a passthrough copy of some scenes of a film, in an order of your choosing.

Scenes are marked while watching - in the headset or the player here. On this tab they are
chosen, put in order and trimmed, then cut out of the film into one clip, which goes on the
Make copies list and is made into a passthrough copy like any other video.
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget)

from blueshelf import passthrough
from blueshelf.db import Database
from blueshelf.util import fmt_time

CLOCK = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*$|^\s*(\d+(?:\.\d+)?)\s*$")


def read_clock(text: str) -> float | None:
    """"1:02:03", "12:30", "12:30.5" or plain seconds; None for anything else."""
    found = CLOCK.match(text or "")
    if not found:
        return None
    if found.group(4) is not None:
        return float(found.group(4))
    hours = int(found.group(1) or 0)
    return hours * 3600 + int(found.group(2)) * 60 + float(found.group(3))


def default_name(numbers: list[int]) -> str:
    return "scene " + str(numbers[0]) if len(numbers) == 1 else "scenes " + "+".join(map(str, numbers))


class _Cutting(QThread):
    step = Signal(str, float, str)
    finished_clip = Signal(str)
    failed = Signal(str)

    def __init__(self, film: str, ranges: list[tuple[float, float]], into: Path, parent=None):
        super().__init__(parent)
        self.film, self.ranges, self.into = film, ranges, into

    def run(self) -> None:
        try:
            made = passthrough.cut_scenes(self.film, self.ranges, self.into,
                                          lambda s, f, d: self.step.emit(s, f, d))
            self.finished_clip.emit(str(made))
        except Exception as exc:  # noqa: BLE001 - shown to the person, not raised into Qt
            self.failed.emit(str(exc))


class ScenesTab(QWidget):
    """Choose a film's scenes, put them in order, and send them to be made."""

    ready = Signal(str)          # a clip cut and ready to be made into a passthrough copy

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.cutting: _Cutting | None = None
        self._named = False                  # the person typed a name; stop suggesting one

        intro = QLabel(
            "Scenes marked while watching - press <b>Mark scene</b> in the headset, or <b>T</b> in "
            "the player here. Choose them, put them in the order you want, and they are cut out of the "
            "film into one clip, which goes on the <b>Make copies</b> list to be made into a full "
            "passthrough copy.<br><br>The cut copies the film rather than re-encoding it, so it can "
            "only start on a key frame: each scene starts up to a second or two before where it was "
            "marked, never after. Its end is exact.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)

        self.films = QComboBox()
        self.films.currentIndexChanged.connect(self._show_scenes)
        again = QPushButton("Look again")
        again.setToolTip("Scenes marked since this window opened - in the headset, say - appear after this.")
        again.clicked.connect(self.reload)
        film_row = QHBoxLayout()
        film_row.addWidget(QLabel("Film"))
        film_row.addWidget(self.films, 1)
        film_row.addWidget(again)

        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setMinimumHeight(160)
        self.list.itemChanged.connect(self._chosen_changed)
        self.list.model().rowsMoved.connect(lambda *_: self._chosen_changed())
        self.list.currentItemChanged.connect(self._picked)

        up, down = QPushButton("Move up"), QPushButton("Move down")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        every, none = QPushButton("Choose all"), QPushButton("Choose none")
        every.clicked.connect(lambda: self._choose_all(True))
        none.clicked.connect(lambda: self._choose_all(False))
        order_row = QHBoxLayout()
        for button in (up, down, every, none):
            order_row.addWidget(button)
        order_row.addStretch(1)

        # The scene picked in the list, to trim or rename.
        self.start_box, self.end_box, self.label_box = QLineEdit(), QLineEdit(), QLineEdit()
        for box, hint in ((self.start_box, "12:30"), (self.end_box, "18:45"), (self.label_box, "Scene 1")):
            box.setPlaceholderText(hint)
        self.start_box.setMaximumWidth(110)
        self.end_box.setMaximumWidth(110)
        save = QPushButton("Save changes")
        save.clicked.connect(self._save_scene)
        remove = QPushButton("Remove scene")
        remove.clicked.connect(self._remove_scene)
        edit_row = QHBoxLayout()
        edit_row.addWidget(QLabel("Start"))
        edit_row.addWidget(self.start_box)
        edit_row.addWidget(QLabel("End"))
        edit_row.addWidget(self.end_box)
        edit_row.addWidget(QLabel("Name"))
        edit_row.addWidget(self.label_box, 1)
        edit_row.addWidget(save)
        edit_row.addWidget(remove)

        self.total = QLabel()
        self.name_box = QLineEdit()
        self.name_box.textEdited.connect(lambda _t: setattr(self, "_named", True))
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Call it"))
        name_row.addWidget(self.name_box, 1)

        self.make_btn = QPushButton("Cut these scenes out and add them to the list")
        self.make_btn.setObjectName("primary")
        self.make_btn.clicked.connect(self._make)
        self.stage = QLabel()
        self.stage.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.hide()

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(film_row)
        layout.addWidget(QLabel("Scenes - ticked ones are made, in this order. Drag to reorder."))
        layout.addWidget(self.list, 1)
        layout.addLayout(order_row)
        layout.addLayout(edit_row)
        layout.addWidget(self.total)
        layout.addLayout(name_row)
        layout.addWidget(self.make_btn)
        layout.addWidget(self.stage)
        layout.addWidget(self.bar)
        self.reload()

    # ------------------------------------------------------------ the films and their scenes

    def reload(self) -> None:
        keep = self.films.currentData()
        self.films.blockSignals(True)
        self.films.clear()
        for film in self.db.videos_with_scenes():
            title = film["title"] or Path(film["path"]).stem
            self.films.addItem(f"{title}  -  {film['n']} scene{'s' if film['n'] != 1 else ''}", film["id"])
        self.films.blockSignals(False)
        at = self.films.findData(keep) if keep is not None else -1
        self.films.setCurrentIndex(at if at >= 0 else (0 if self.films.count() else -1))
        self._show_scenes()

    def show_film(self, video_id: int) -> None:
        self.reload()
        at = self.films.findData(video_id)
        if at >= 0:
            self.films.setCurrentIndex(at)

    def _film(self) -> dict | None:
        video_id = self.films.currentData()
        return self.db.get_video(video_id) if video_id is not None else None

    def _show_scenes(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        film = self._film()
        for number, scene in enumerate(self.db.scenes(film["id"]) if film else [], 1):
            item = QListWidgetItem()
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, {**scene, "number": number})
            item.setText(self._describe(scene))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._named = False
        if self.list.count():
            self.list.setCurrentRow(0)
        self._chosen_changed()
        if film is None:
            self.stage.setText("No scenes have been marked yet. Mark some while watching, then press Look again.")

    @staticmethod
    def _describe(scene: dict) -> str:
        length = scene["end"] - scene["start"]
        return f"{scene['label']}    {fmt_time(scene['start'])} - {fmt_time(scene['end'])}    ({fmt_time(length)})"

    def chosen(self) -> list[dict]:
        """The ticked scenes, in the order they are listed."""
        return [self.list.item(row).data(Qt.UserRole) for row in range(self.list.count())
                if self.list.item(row).checkState() == Qt.Checked]

    def _chosen_changed(self, *_args) -> None:
        picked = self.chosen()
        length = sum(s["end"] - s["start"] for s in picked)
        self.total.setText(f"{len(picked)} scene{'s' if len(picked) != 1 else ''} chosen, "
                           f"{fmt_time(length)} altogether." if picked else "No scenes chosen.")
        if not self._named:
            self.name_box.setText(default_name([s["number"] for s in picked]) if picked else "")
        self.make_btn.setEnabled(bool(picked) and self.cutting is None)

    def _choose_all(self, on: bool) -> None:
        for row in range(self.list.count()):
            self.list.item(row).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _move(self, by: int) -> None:
        row = self.list.currentRow()
        target = row + by
        if row < 0 or not 0 <= target < self.list.count():
            return
        self.list.blockSignals(True)
        item = self.list.takeItem(row)
        self.list.insertItem(target, item)
        self.list.blockSignals(False)
        self.list.setCurrentRow(target)
        self._chosen_changed()

    # ------------------------------------------------------------ trimming one

    def _picked(self, item, _previous=None) -> None:
        scene = item.data(Qt.UserRole) if item else None
        self.start_box.setText(fmt_time(scene["start"]) if scene else "")
        self.end_box.setText(fmt_time(scene["end"]) if scene else "")
        self.label_box.setText(scene["label"] if scene and scene.get("named") else "")

    def _save_scene(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        scene = item.data(Qt.UserRole)
        start, end = read_clock(self.start_box.text()), read_clock(self.end_box.text())
        if start is None or end is None:
            QMessageBox.information(self, "Scenes", "Times look like 12:30, 1:02:03 or a number of seconds.")
            return
        try:
            self.db.update_scene(scene["id"], start=start, end=end, label=self.label_box.text())
        except ValueError as exc:
            QMessageBox.information(self, "Scenes", str(exc).capitalize() + ".")
            return
        self._refresh_keeping_order()

    def _remove_scene(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        scene = item.data(Qt.UserRole)
        if QMessageBox.question(self, "Scenes", f"Remove {scene['label']}? The film itself is not touched.",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.db.delete_scene(scene["id"])
        self._refresh_keeping_order()

    def _refresh_keeping_order(self) -> None:
        """After a change, the scenes as the database now has them, in the order chosen here."""
        order = [self.list.item(row).data(Qt.UserRole)["id"] for row in range(self.list.count())]
        ticked = {s["id"] for s in self.chosen()}
        current = self.list.currentRow()
        film = self._film()
        fresh = {s["id"]: (n, s) for n, s in enumerate(self.db.scenes(film["id"]) if film else [], 1)}
        self.list.blockSignals(True)
        self.list.clear()
        for scene_id in [i for i in order if i in fresh] + [i for i in fresh if i not in order]:
            number, scene = fresh[scene_id]
            item = QListWidgetItem(self._describe(scene))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            item.setCheckState(Qt.Checked if scene_id in ticked or scene_id not in order else Qt.Unchecked)
            item.setData(Qt.UserRole, {**scene, "number": number})
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.list.setCurrentRow(min(max(current, 0), self.list.count() - 1))
        self._chosen_changed()
        if not fresh:
            self.reload()

    # ------------------------------------------------------------ cutting

    def _make(self) -> None:
        film = self._film()
        picked = self.chosen()
        if film is None or not picked or self.cutting is not None:
            return
        into = passthrough.scene_clip_path(film["path"], self.name_box.text().strip())
        n = 2
        while into.exists():                  # never over a clip that a copy may be made from
            into = into.with_name(f"{into.stem.rsplit(' (', 1)[0]} ({n}).mp4")
            n += 1
        self.make_btn.setEnabled(False)
        self.bar.setValue(0)
        self.bar.show()
        self.cutting = _Cutting(film["path"], [(s["start"], s["end"]) for s in picked], into, self)
        self.cutting.step.connect(self._cut_step)
        self.cutting.finished_clip.connect(self._cut_done)
        self.cutting.failed.connect(self._cut_failed)
        self.cutting.finished.connect(self._cut_over)
        self.cutting.start()

    def _cut_step(self, stage: str, share: float, detail: str) -> None:
        self.stage.setText(f"{stage} - {detail}" if detail else stage)
        self.bar.setValue(int(share * 1000))

    def _cut_done(self, clip: str) -> None:
        self.stage.setText(f"Cut: {Path(clip).name}. It is on the Make copies list - choose how to make it "
                           f"there and press Start.")
        self.ready.emit(clip)

    def _cut_failed(self, why: str) -> None:
        self.stage.setText(f"Could not cut the scenes: {why}")

    def _cut_over(self) -> None:
        self.cutting = None
        self.bar.hide()
        self._chosen_changed()

    def shut_down(self) -> None:
        if self.cutting is not None:
            self.cutting.wait(60000)
