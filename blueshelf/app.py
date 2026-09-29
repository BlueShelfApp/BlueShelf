"""Main window: library browser, player page and background work."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
import uuid

from PySide6.QtCore import QByteArray, QItemSelection, QItemSelectionModel, QObject, QProcess, QRunnable, Qt, \
    QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView, QMainWindow, QMenu,
                               QMessageBox, QProgressBar, QScrollArea, QSlider, QSplitter, QStackedWidget, QTableView,
                               QToolButton, QVBoxLayout, QWidget)

from blueshelf import __version__, backup, paths, trash
from blueshelf.backup_dialog import BackupDialog
from blueshelf.db import Database
from blueshelf.details import DetailsPanel
from blueshelf.dialogs import SettingsDialog, ShortcutsDialog, TrashDialog, WelcomePage, library_overlap
from blueshelf.dlna import DEFAULT_PORT as DLNA_PORT, DlnaServer, start_saved
from blueshelf.dupes_dialog import DupesDialog
from blueshelf.library import (COLUMNS, DEFAULT_ASCENDING, SORT_FOR_COLUMN, CardDelegate, ThumbCache, VideoModel,
                           display_title)
from blueshelf.names_dialog import NameReviewDialog
from blueshelf.move_dialog import MoveDialog
from blueshelf.organize_dialog import OrganizeDialog
from blueshelf.parser import DEFAULT_TAG_RULES
from blueshelf.passthrough_dialog import PassthroughDialog
from blueshelf.player import PlayerWidget
from blueshelf.remote_dialog import RemoteDialog
from blueshelf.server import DEFAULT_PORT, RemoteServer, make_token
from blueshelf.scanner import ScanThread, regenerate_thumb, remove_thumb, reparse_videos
from blueshelf.sidebar import Sidebar
from blueshelf.theme import apply_theme, draw_icon, make_icon
from blueshelf.util import fmt_hours, fmt_size
from blueshelf.widgets import ask_text

SORT_CHOICES = [("added", "Date added"), ("title", "Title"), ("performers", "Performer"), ("studio", "Studio"),
                ("date", "Release date"), ("duration", "Length"), ("size", "File size"), ("resolution", "Quality"),
                ("rating", "Rating"), ("plays", "Most played"), ("last_played", "Recently played")]


class _TaskSignals(QObject):
    done = Signal(object)


class Task(QRunnable):
    """Runs a function on the thread pool and hands the result (or exception) back to the GUI thread."""

    def __init__(self, fn):
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:
            result = exc
        self.signals.done.emit(result)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BlueShelf")
        self.resize(1440, 900)
        self.db = Database(str(paths.db_path()))
        if self.db.get_setting("tag_rules") is None:
            self.db.set_setting("tag_rules", DEFAULT_TAG_RULES)
        self.theme_mode = self.db.get_setting("theme", "system")
        apply_theme(QApplication.instance(), self.theme_mode)
        self.cache = ThumbCache()
        self.scan_thread: ScanThread | None = None
        self.dupes_dialog: DupesDialog | None = None
        self.remote = RemoteServer(token=self.db.get_setting("remote_token", "") or make_token(),
                                   port=int(self.db.get_setting("remote_port", DEFAULT_PORT)))
        self.db.set_setting("remote_token", self.remote.token)
        # Players remember a DLNA server by this id, so it stays the same from one run to the next.
        if not self.db.get_setting("dlna_uuid", ""):
            self.db.set_setting("dlna_uuid", str(uuid.uuid4()))
        self.dlna = DlnaServer(name=self.db.get_setting("dlna_name", ""), udn=self.db.get_setting("dlna_uuid"),
                               port=int(self.db.get_setting("dlna_port", DLNA_PORT)))
        self.force_exit = False
        self.view_mode = "grid"
        self.filter_kind, self.filter_value = "view", "all"
        self.queue: list[int] = []
        self.cleanup_queue: list[int] = []
        self.cleanup_index = 0
        self.cleanup_stats = {"kept": 0, "deleted": 0}
        self._cleanup_undo_item: tuple[int, str, str] | None = None
        self.show_hidden = bool(self.db.get_setting("show_hidden", False))
        self._rescan_pending = False
        self._rescan_reparse = False
        self._tag_passthrough: dict[str, int] = {}
        self.passthrough_window = None
        self._tasks: set[Task] = set()
        self._ready = False
        self._was_maximized = False
        self._restarting = False

        self._refresh_timer = QTimer(self, singleShot=True, interval=500)
        self._refresh_timer.timeout.connect(self.refresh_all)
        self._search_timer = QTimer(self, singleShot=True, interval=200)
        self._search_timer.timeout.connect(self.refresh_library)

        self._build_ui()
        self._build_menus()
        self._restore_state()
        self._ready = True

        if self.db.libraries():
            self.stack.setCurrentWidget(self.library_page)
            self.refresh_all()
            if self.db.get_setting("scan_on_start", True):
                QTimer.singleShot(800, self.scan)
        else:
            self.stack.setCurrentWidget(self.welcome)

        if self.db.get_setting("remote_enabled", False):
            try:
                self.remote.start()
            except OSError as exc:
                self.status_label.setText(f"The headset page could not start: {exc.strerror or exc}")
        if self.db.get_setting("dlna_enabled", False):
            problem = start_saved(self.dlna, self.db)
            if problem:
                self.status_label.setText(f"DLNA sharing could not start: {problem}")
        # New downloads should simply appear, rather than waiting to be asked for.
        self._last_scan_at = time.time()
        self._auto_scan_timer = QTimer(self, interval=60_000)
        self._auto_scan_timer.timeout.connect(self._auto_scan)
        self._auto_scan_timer.start()
        # Deleting or rating from a headset changes the same library, so keep the desktop in step.
        self._remote_timer = QTimer(self, interval=3000)
        self._remote_timer.timeout.connect(self._check_remote)
        self._remote_timer.start()
        if self.db.get_setting("auto_backup", True):
            QTimer.singleShot(4000, lambda: self.run_task(lambda: backup.run_automatic(7), self._backup_done))

    def _backup_done(self, summary) -> None:
        if summary:
            self.status_label.setText(f"Library data backed up ({summary.get('videos', 0)} videos)")

    def apply_appearance(self) -> None:
        self.theme_mode = self.db.get_setting("theme", "system")
        apply_theme(QApplication.instance(), self.theme_mode)
        self.player.refresh_icons()
        self.details.apply_colors()
        for view in (self.grid, self.table):
            # Videos can be dragged out to the passthrough window. Drag only - nothing is
            # ever dropped back into the library this way.
            view.setDragEnabled(True)
            view.setDragDropMode(QAbstractItemView.DragOnly)
            view.setDefaultDropAction(Qt.CopyAction)
            view.viewport().update()

    def _check_remote(self) -> None:
        if self.remote.changed:
            self.refresh_all()

    # ------------------------------------------------------------ construction

    def _build_ui(self) -> None:
        self.stack = QStackedWidget(self)
        self.setCentralWidget(self.stack)

        self.welcome = WelcomePage()
        self.welcome.add_clicked.connect(self.add_library)
        self.stack.addWidget(self.welcome)

        self.sidebar = Sidebar(self.db)
        self.sidebar.filter_changed.connect(self._on_filter)
        self.sidebar.data_changed.connect(self.refresh_all)
        self.sidebar.move_performer.connect(self.move_performer)

        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(14, 10, 14, 0)
        cl.setSpacing(8)

        top = QHBoxLayout()
        self.heading = QLabel("All videos", objectName="heading")
        self.count_label = QLabel(objectName="muted")
        self.search = QLineEdit(placeholderText="Search titles, performers, studios, categories",
                                clearButtonEnabled=True)
        self.search.setMinimumWidth(320)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        top.addWidget(self.heading)
        top.addSpacing(10)
        top.addWidget(self.count_label)
        top.addStretch()
        self.mute_btn = QToolButton(checkable=True)
        self.mute_btn.setToolTip("Mute playback. Set it before pressing play; it is remembered.")
        self.mute_btn.setChecked(bool(self.db.get_setting("muted", False)))
        self.mute_btn.toggled.connect(self._mute_toggled)
        self._paint_mute()
        top.addWidget(self.mute_btn)
        top.addSpacing(6)
        top.addWidget(self.search)
        cl.addLayout(top)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Sort by", objectName="muted"))
        self.sort_combo = QComboBox()
        for key, label in SORT_CHOICES:
            self.sort_combo.addItem(label, key)
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)
        bar.addWidget(self.sort_combo)
        self.sort_dir = QToolButton(checkable=True, checked=True)
        self.sort_dir.toggled.connect(self._sort_dir_toggled)
        bar.addWidget(self.sort_dir)
        bar.addStretch()
        self.hidden_check = QCheckBox("Show hidden")
        self.hidden_check.setChecked(self.show_hidden)
        self.hidden_check.toggled.connect(self.set_show_hidden)
        bar.addWidget(self.hidden_check)
        bar.addSpacing(14)
        bar.addWidget(QLabel("Size", objectName="muted"))
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(180, 480)
        self.size_slider.setFixedWidth(120)
        self.size_slider.valueChanged.connect(self._set_card_width)
        bar.addWidget(self.size_slider)
        self.grid_btn = QToolButton(text="Grid", checkable=True)
        self.list_btn = QToolButton(text="List", checkable=True)
        buttons = QButtonGroup(self)
        buttons.setExclusive(True)
        buttons.addButton(self.grid_btn)
        buttons.addButton(self.list_btn)
        self.grid_btn.clicked.connect(lambda: self.set_view_mode("grid"))
        self.list_btn.clicked.connect(lambda: self.set_view_mode("list"))
        bar.addWidget(self.grid_btn)
        bar.addWidget(self.list_btn)
        cl.addLayout(bar)

        self.model = VideoModel(self)
        self.grid = QListView(objectName="grid")
        self.grid.setModel(self.model)
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.setMouseTracking(True)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.grid.verticalScrollBar().setSingleStep(40)
        self.delegate = CardDelegate(self.cache, self.grid)
        self.grid.setItemDelegate(self.delegate)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionModel(self.grid.selectionModel())
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(28)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.sectionClicked.connect(self._header_clicked)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 220), (2, 140), (3, 95), (4, 70), (5, 80), (6, 80), (7, 70), (8, 50), (9, 90)):
            self.table.setColumnWidth(col, width)

        for view in (self.grid, self.table):
            view.setContextMenuPolicy(Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, v=view: self._context_menu(v, pos))
            view.activated.connect(self._activated)
            shortcut = QShortcut(QKeySequence(QKeySequence.Delete), view)
            shortcut.setContext(Qt.WidgetShortcut)
            shortcut.activated.connect(lambda: self.delete_videos(self.selected_ids()))
        self.grid.selectionModel().selectionChanged.connect(self._selection_changed)

        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.grid)
        self.view_stack.addWidget(self.table)
        cl.addWidget(self.view_stack, 1)

        self.details = DetailsPanel(self.db, self.cache)
        self.details.play_requested.connect(lambda vid: self.play_video(vid))
        self.details.bulk_requested.connect(self.bulk)
        self.details.changed.connect(self._details_changed)
        self.details.folder_requested.connect(self.show_in_folder)
        details_scroll = QScrollArea(widgetResizable=True)
        details_scroll.setWidget(self.details)
        details_scroll.setMinimumWidth(320)

        self.splitter = QSplitter()
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(center)
        self.splitter.addWidget(details_scroll)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([250, 860, 350])
        self.library_page = self.splitter
        self.stack.addWidget(self.library_page)

        self.player = PlayerWidget(self.db)
        self.player.back_requested.connect(self.back_to_library)
        self.player.next_requested.connect(lambda: self._step(1))
        self.player.prev_requested.connect(lambda: self._step(-1))
        self.player.delete_requested.connect(self._delete_from_player)
        self.player.rate_requested.connect(self.rate)
        self.player.favorite_requested.connect(self.set_favorite)
        self.player.mute_changed.connect(self._player_mute_changed)
        self.player.cleanup_keep.connect(self._cleanup_keep)
        self.player.cleanup_delete.connect(self._cleanup_delete)
        self.player.cleanup_undo.connect(self._cleanup_undo)
        self.player.cleanup_finish.connect(self._cleanup_finish)
        self.player.fullscreen_requested.connect(self.toggle_fullscreen)
        self.stack.addWidget(self.player)

        self.status_label = QLabel()
        self.progress = QProgressBar(textVisible=False)
        self.progress.setMaximumWidth(220)
        self.progress.hide()
        self.totals_label = QLabel()
        status = self.statusBar()
        status.addWidget(self.status_label, 1)
        status.addPermanentWidget(self.progress)
        status.addPermanentWidget(self.totals_label)

    def _action(self, menu: QMenu, text: str, slot, shortcut: str | None = None,
                checkable: bool = False) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setCheckable(checkable)
        action.triggered.connect(slot)
        menu.addAction(action)
        self.addAction(action)  # keeps the shortcut working while the menu bar is hidden in full screen
        return action

    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        self._action(file_menu, "Add video folder…", lambda: self.add_library(), "Ctrl+O")
        self._action(file_menu, "Look for new videos", lambda: self.scan(), "F5")
        self._action(file_menu, "Re-read all file names…", lambda: self._reparse_all())
        file_menu.addSeparator()
        self._action(file_menu, "Bring in a library…", lambda: self.bring_in_library())
        self._action(file_menu, "Write my library beside the films…", lambda: self.write_sidecars())
        file_menu.addSeparator()
        self._action(file_menu, "Settings…", lambda: self.open_settings(), "Ctrl+,")
        file_menu.addSeparator()
        self._action(file_menu, "Quit", lambda: self.close(), "Ctrl+Q")

        tools = bar.addMenu("&Tools")
        self._action(tools, "Find duplicates…", lambda: self.find_duplicates(), "Ctrl+D")
        self._action(tools, "Review names…", lambda: self.review_names(), "Ctrl+R")
        self._action(tools, "Build shortcut folders…", lambda: self.build_shortcuts())
        self._action(tools, "Share with a headset, phone or TV…", lambda: self.watch_remotely())
        self._action(tools, "Make passthrough copies…", lambda: self.make_passthrough())
        self._action(tools, "Help another computer…", lambda: self.help_another_computer())
        self._action(tools, "Trash…", lambda: self.open_trash())
        self._action(tools, "Backup…", lambda: self.open_backup())
        tools.addSeparator()
        self._action(tools, "Cleanup mode…", lambda: self.start_cleanup(), "Ctrl+E")

        view = bar.addMenu("&View")
        self._action(view, "Grid", lambda: self.set_view_mode("grid"), "Ctrl+1")
        self._action(view, "List", lambda: self.set_view_mode("list"), "Ctrl+2")
        self.hidden_action = self._action(view, "Show hidden videos", self.set_show_hidden, checkable=True)
        self.hidden_action.setChecked(self.show_hidden)
        view.addSeparator()
        self._action(view, "Full screen", lambda: self.toggle_fullscreen(), "F11")
        view.addSeparator()
        self.passthrough_action = self._action(
            view, "Passthrough window", self._toggle_passthrough, "Ctrl+P", checkable=True)

        help_menu = bar.addMenu("&Help")
        self._action(help_menu, "Keyboard shortcuts", lambda: ShortcutsDialog(self).exec(), "F1")
        self._action(help_menu, "Open data folder", lambda: os.startfile(str(paths.data_dir())))
        self._action(help_menu, "About BlueShelf", lambda: self._about())

        search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self.library_page)
        search_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        search_shortcut.activated.connect(lambda: (self.search.setFocus(), self.search.selectAll()))

    def _restore_state(self) -> None:
        geometry = self.db.get_setting("window_geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))
        sizes = self.db.get_setting("splitter_sizes")
        if sizes and len(sizes) == 3:
            self.splitter.setSizes(sizes)
        self.size_slider.setValue(int(self.db.get_setting("card_width", 260)))
        self._set_card_width(self.size_slider.value())
        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(self.db.get_setting("sort", "added"))))
        self.sort_combo.blockSignals(False)
        self.sort_dir.blockSignals(True)
        self.sort_dir.setChecked(bool(self.db.get_setting("sort_descending", True)))
        self.sort_dir.blockSignals(False)
        self._update_sort_dir_text()
        self.set_view_mode(self.db.get_setting("view_mode", "grid"))

    def _save_state(self) -> None:
        self.db.set_setting("window_geometry", bytes(self.saveGeometry().toBase64()).decode())
        self.db.set_setting("splitter_sizes", self.splitter.sizes())
        self.db.set_setting("card_width", self.size_slider.value())
        self.db.set_setting("sort", self.sort_combo.currentData())
        self.db.set_setting("sort_descending", self.sort_dir.isChecked())
        self.db.set_setting("view_mode", self.view_mode)

    # ------------------------------------------------------------ library view

    def selected_videos(self) -> list[dict]:
        rows = sorted({index.row() for index in self.grid.selectionModel().selectedIndexes()})
        return [self.model.video(r) for r in rows if r < len(self.model.videos)]

    def selected_ids(self) -> list[int]:
        return [v["id"] for v in self.selected_videos()]

    def refresh_all(self) -> None:
        if not self._ready:
            return
        self.sidebar.refresh(self.show_hidden)
        self.details.update_completions()
        self.refresh_library()

    def refresh_library(self) -> None:
        if not self._ready:
            return
        selected = self.selected_ids()
        current = self.grid.currentIndex()
        current_id = self.model.video(current.row())["id"] if current.isValid() else None
        filters = dict(search=self.search.text().strip(), show_hidden=self.show_hidden,
                       sort=self.sort_combo.currentData(), descending=self.sort_dir.isChecked())
        if self.filter_kind == "view":
            filters["view"] = self.filter_value
        elif self.filter_kind == "performer":
            filters["performer_id"] = self.filter_value
        elif self.filter_kind == "library":
            filters["library_id"] = self.filter_value
        elif self.filter_kind == "studio":
            filters["studio"] = self.filter_value
        elif self.filter_kind == "tag":
            filters["tag_id"] = self.filter_value
        videos = self.db.list_videos(**filters)

        grid_scroll = self.grid.verticalScrollBar().value()
        table_scroll = self.table.verticalScrollBar().value()
        selection_model = self.grid.selectionModel()
        selection_model.blockSignals(True)
        self.model.set_videos(videos)
        selection = QItemSelection()
        for vid in selected:
            row = self.model.row_of(vid)
            if row is not None:
                selection.select(self.model.index(row, 0), self.model.index(row, len(COLUMNS) - 1))
        selection_model.select(selection, QItemSelectionModel.ClearAndSelect)
        if current_id is not None and self.model.row_of(current_id) is not None:
            selection_model.setCurrentIndex(self.model.index(self.model.row_of(current_id), 0),
                                            QItemSelectionModel.NoUpdate)
        selection_model.blockSignals(False)
        self.grid.verticalScrollBar().setValue(grid_scroll)
        self.table.verticalScrollBar().setValue(table_scroll)
        self.grid.viewport().update()
        self.table.viewport().update()

        size = sum(v["size"] for v in videos)
        duration = sum(v["duration"] or 0 for v in videos)
        self.count_label.setText(f"{len(videos)} videos · {fmt_size(size)} · {fmt_hours(duration)}")
        count, total_size, _ = self.db.totals(self.show_hidden)
        self.totals_label.setText(f"Library: {count} videos · {fmt_size(total_size)}")
        self._selection_changed()

    def _refresh_row(self, video_id: int) -> None:
        rows = self.db.list_videos(video_id=video_id, show_hidden=True)
        if rows:
            self.model.update_video(rows[0])

    def _paint_mute(self) -> None:
        muted = self.mute_btn.isChecked()
        self.mute_btn.setIcon(draw_icon("muted" if muted else "volume", 20))
        self.mute_btn.setText("")

    def _mute_toggled(self, muted: bool) -> None:
        self.player.set_muted(muted)
        self._paint_mute()

    def _player_mute_changed(self, muted: bool) -> None:
        """The player's own mute button was used, so the one up here matches it."""
        if self.mute_btn.isChecked() != muted:
            self.mute_btn.blockSignals(True)
            self.mute_btn.setChecked(muted)
            self.mute_btn.blockSignals(False)
        self._paint_mute()

    def _on_filter(self, kind: str, value, label: str) -> None:
        self.filter_kind, self.filter_value = kind, value
        self.heading.setText(label)
        self.refresh_library()

    def _selection_changed(self, *_args) -> None:
        self.details.show_videos(self.selected_videos())

    def _details_changed(self, ids: list[int]) -> None:
        for vid in ids:
            self._refresh_row(vid)
        self.sidebar.refresh(self.show_hidden)
        self.details.update_completions()

    def _activated(self, index) -> None:
        if index.isValid():
            self.play_video(self.model.video(index.row())["id"])

    def _update_sort_dir_text(self) -> None:
        descending = self.sort_dir.isChecked()
        self.sort_dir.setText("↓" if descending else "↑")
        self.sort_dir.setToolTip("Descending, click to reverse" if descending else "Ascending, click to reverse")

    def _sort_changed(self) -> None:
        self.sort_dir.blockSignals(True)
        self.sort_dir.setChecked(self.sort_combo.currentData() not in DEFAULT_ASCENDING)
        self.sort_dir.blockSignals(False)
        self._update_sort_dir_text()
        self.refresh_library()

    def _sort_dir_toggled(self) -> None:
        self._update_sort_dir_text()
        self.refresh_library()

    def _header_clicked(self, section: int) -> None:
        key = SORT_FOR_COLUMN[COLUMNS[section][0]]
        if key == self.sort_combo.currentData():
            self.sort_dir.toggle()
        else:
            self.sort_combo.setCurrentIndex(self.sort_combo.findData(key))

    def set_view_mode(self, mode: str) -> None:
        self.view_mode = "list" if mode == "list" else "grid"
        self.view_stack.setCurrentIndex(1 if self.view_mode == "list" else 0)
        (self.list_btn if self.view_mode == "list" else self.grid_btn).setChecked(True)

    def _set_card_width(self, width: int) -> None:
        self.delegate.card_width = width
        self.model.layoutAboutToBeChanged.emit()
        self.model.layoutChanged.emit()

    def set_show_hidden(self, on: bool) -> None:
        on = bool(on)
        for widget in (self.hidden_check, self.hidden_action):
            widget.blockSignals(True)
            widget.setChecked(on)
            widget.blockSignals(False)
        if on == self.show_hidden:
            return
        self.show_hidden = on
        self.db.set_setting("show_hidden", on)
        self.refresh_all()

    def _context_menu(self, view, pos) -> None:
        index = view.indexAt(pos)
        if index.isValid() and self.model.video(index.row())["id"] not in self.selected_ids():
            view.selectionModel().select(index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        videos = self.selected_videos()
        if not videos:
            return
        ids = [v["id"] for v in videos]
        menu = QMenu(self)
        menu.addAction("Play").triggered.connect(lambda: self.bulk("play", ids))
        if len(videos) == 1:
            menu.addAction("Show in folder").triggered.connect(lambda: self.show_in_folder(videos[0]["path"]))
        menu.addSeparator()
        menu.addAction("Add performer…").triggered.connect(lambda: self.bulk("add_performer", ids))
        menu.addAction("Add category…").triggered.connect(lambda: self.bulk("add_tag", ids))
        menu.addAction("Set studio…").triggered.connect(lambda: self.bulk("set_studio", ids))
        menu.addSeparator()
        favorite = all(v["favorite"] for v in videos)
        menu.addAction("Remove from favorites" if favorite else "Add to favorites").triggered.connect(
            lambda: self.bulk("unfavorite" if favorite else "favorite", ids))
        vr = all(v["is_vr"] for v in videos)
        menu.addAction("Mark as not VR" if vr else "Mark as VR").triggered.connect(
            lambda: self.bulk("not_vr" if vr else "vr", ids))
        hidden = all(v["hidden"] for v in videos)
        menu.addAction("Unhide" if hidden else "Hide from library").triggered.connect(
            lambda: self.bulk("unhide" if hidden else "hide", ids))
        menu.addSeparator()
        menu.addAction("Make passthrough copies…").triggered.connect(
            lambda: self.make_passthrough(videos))
        menu.addAction("Make new thumbnail").triggered.connect(lambda: self.bulk("thumb", ids))
        menu.addAction("Re-read from file name").triggered.connect(lambda: self.bulk("reparse", ids))
        menu.addSeparator()
        label = "Delete video…" if len(ids) == 1 else f"Delete {len(ids)} videos…"
        menu.addAction(label).triggered.connect(lambda: self.delete_videos(ids))
        menu.exec(view.viewport().mapToGlobal(pos))

    def bulk(self, action: str, ids: list[int]) -> None:
        if not ids:
            return
        db = self.db
        if action == "play":
            self.play_video(ids[0], queue=ids if len(ids) > 1 else None)
            return
        if action == "delete":
            self.delete_videos(ids)
            return
        if action == "thumb":
            self.regenerate_thumbs(ids)
            return
        if action in ("add_performer", "add_tag", "set_studio"):
            count = f"{len(ids)} video{'s' if len(ids) > 1 else ''}"
            prompts = {
                "add_performer": ("Add performer", f"Performers to add to {count}. Separate names with commas.",
                                  db.performer_names(), True),
                "add_tag": ("Add category", f"Categories to add to {count}. Separate them with commas.",
                            db.tag_names(), True),
                "set_studio": ("Set studio", f"Studio for {count}:", db.studio_names(), False),
            }
            title, label, words, multi = prompts[action]
            text = ask_text(self, title, label, words, multi=multi)
            if not text:
                return
            if action == "set_studio":
                for vid in ids:
                    db.set_fields(vid, studio=text, meta_locked=1)
            else:
                add = db.add_performer_to if action == "add_performer" else db.add_tag_to
                for name in (n.strip() for n in text.split(",")):
                    if name:
                        add(ids, name)
        elif action in ("favorite", "unfavorite"):
            for vid in ids:
                db.set_fields(vid, favorite=int(action == "favorite"))
        elif action in ("hide", "unhide"):
            for vid in ids:
                db.set_fields(vid, hidden=int(action == "hide"), hidden_locked=1)
        elif action in ("vr", "not_vr"):
            for vid in ids:
                db.set_fields(vid, is_vr=int(action == "vr"), vr_locked=1)
            self.regenerate_thumbs(ids)
        elif action == "reparse":
            reparse_videos(db, ids)
        self.refresh_all()

    def regenerate_thumbs(self, ids: list[int]) -> None:
        self.status_label.setText(f"Making {len(ids)} thumbnail{'s' if len(ids) > 1 else ''}…")

        def work():
            db = Database(str(paths.db_path()))
            try:
                return [(vid, regenerate_thumb(db, vid)) for vid in ids]
            finally:
                db.close()

        self.run_task(work, self._thumbs_done)

    def _thumbs_done(self, results) -> None:
        for vid, _ok in results:
            self.cache.invalidate(vid)
            self._refresh_row(vid)
            self.details.refresh_thumb(vid)
        self.status_label.setText("Thumbnails updated")

    def run_task(self, fn, on_done) -> None:
        task = Task(fn)
        self._tasks.add(task)

        def finished(result, task=task):
            self._tasks.discard(task)
            if isinstance(result, Exception):
                QMessageBox.warning(self, "Something went wrong", str(result))
            else:
                on_done(result)

        task.signals.done.connect(finished)
        QThreadPool.globalInstance().start(task)

    # ------------------------------------------------------------ playback

    def play_video(self, video_id: int, queue: list[int] | None = None) -> None:
        video = self.db.get_video(video_id)
        if video is None:
            return
        if not os.path.exists(video["path"]):
            QMessageBox.warning(self, "File not found",
                                f"BlueShelf can't find this file:\n{video['path']}\n\nIf it's on a network drive, check "
                                "the drive is connected. Otherwise use File → Look for new videos.")
            return
        if queue is not None:
            self.queue = list(queue)
        elif video_id not in self.queue or self.stack.currentWidget() is not self.player:
            self.queue = [v["id"] for v in self.model.videos]
        self.stack.setCurrentWidget(self.player)
        self.player.play(video)
        self.setWindowTitle(f"{display_title(video)} - BlueShelf")
        if video_id in self.queue:
            self.player.set_queue_position(self.queue.index(video_id) + 1, len(self.queue))
        else:
            self.player.set_queue_position(0, 0)
        if self.db.get_setting("start_fullscreen", False) and not self.isFullScreen():
            self.toggle_fullscreen()

    def _step(self, delta: int) -> None:
        if self.player.video is None:
            return
        vid = self.player.video["id"]
        if vid not in self.queue:
            return
        i = self.queue.index(vid) + delta
        if 0 <= i < len(self.queue):
            self.play_video(self.queue[i], queue=self.queue)
        elif self.player.mpv is not None:
            self.player.mpv.show_text("End of the list" if delta > 0 else "Start of the list")

    def back_to_library(self) -> None:
        if self.isFullScreen():
            self.toggle_fullscreen()
        last = self.player.video["id"] if self.player.video else None
        self.player.stop()
        self.stack.setCurrentWidget(self.library_page if self.db.libraries() else self.welcome)
        self.setWindowTitle("BlueShelf")
        self.refresh_library()
        row = self.model.row_of(last) if last is not None else None
        if row is not None:
            index = self.model.index(row, 0)
            self.grid.selectionModel().setCurrentIndex(index, QItemSelectionModel.ClearAndSelect
                                                       | QItemSelectionModel.Rows)
            self.grid.scrollTo(index)
            self.table.scrollTo(index)
        (self.table if self.view_mode == "list" else self.grid).setFocus()

    def toggle_fullscreen(self) -> None:
        going_full = not self.isFullScreen()
        if going_full:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        elif self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.menuBar().setVisible(not going_full)
        self.statusBar().setVisible(not going_full)
        self.player.set_fullscreen(going_full and self.stack.currentWidget() is self.player)

    def rate(self, video_id: int, stars: int) -> None:
        self.db.set_fields(video_id, rating=stars)
        if self.player.mpv is not None:
            self.player.mpv.show_text("★" * stars if stars else "Rating cleared")
        self._refresh_row(video_id)

    # ------------------------------------------------------------ cleanup mode

    def start_cleanup(self) -> None:
        """Go through the current list one video at a time, keeping or deleting each."""
        videos = [v["id"] for v in self.model.videos]
        if not videos:
            QMessageBox.information(self, "Nothing to go through", "This view is empty.")
            return
        permanent = self.db.get_setting("delete_mode", "trash") == "permanent"
        fate = "deleted permanently" if permanent else "moved to the trash, where you can get it back"
        answer = QMessageBox.question(
            self, "Cleanup mode",
            f"Work through {len(videos)} videos one at a time.\n\n"
            f"K keeps it and moves on, D deletes it and moves on, U undoes the last delete, Esc finishes.\n\n"
            f"Deleting does not ask each time: the video is {fate}.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        self.cleanup_queue = videos
        self.cleanup_index = 0
        self.cleanup_stats = {"kept": 0, "deleted": 0}
        self._cleanup_undo_item = None
        self.player.set_cleanup(True)
        self._cleanup_show()

    def _cleanup_show(self) -> None:
        if self.cleanup_index >= len(self.cleanup_queue):
            self._cleanup_finish()
            return
        self.play_video(self.cleanup_queue[self.cleanup_index], queue=self.cleanup_queue)
        self.player.set_cleanup(True)
        self.player.set_cleanup_progress(
            f"{self.cleanup_index + 1} of {len(self.cleanup_queue)} · kept {self.cleanup_stats['kept']} · "
            f"deleted {self.cleanup_stats['deleted']}", self._cleanup_undo_item is not None)

    def _cleanup_keep(self) -> None:
        if not self.cleanup_queue:
            return
        self.cleanup_stats["kept"] += 1
        self.cleanup_index += 1
        self._cleanup_show()

    def _cleanup_delete(self) -> None:
        if not self.cleanup_queue or self.cleanup_index >= len(self.cleanup_queue):
            return
        video_id = self.cleanup_queue[self.cleanup_index]
        if not self.delete_videos([video_id], confirm=False):
            return
        row = self.db.get_video(video_id)
        self._cleanup_undo_item = ((video_id, row["trash_item"], row["trash_origin"])
                                   if row and row["trash_item"] else None)
        self.cleanup_stats["deleted"] += 1
        self.cleanup_queue.pop(self.cleanup_index)
        self._cleanup_show()

    def _cleanup_undo(self) -> None:
        if self._cleanup_undo_item is None:
            return
        video_id, item, origin = self._cleanup_undo_item
        try:
            trash.restore(item, origin)
        except OSError as exc:
            QMessageBox.warning(self, "Could not undo", str(exc))
            return
        self.db.restore(item)
        self._cleanup_undo_item = None
        self.cleanup_stats["deleted"] = max(0, self.cleanup_stats["deleted"] - 1)
        self.cleanup_queue.insert(self.cleanup_index, video_id)
        self.refresh_all()
        self._cleanup_show()

    def _cleanup_finish(self) -> None:
        kept, deleted = self.cleanup_stats["kept"], self.cleanup_stats["deleted"]
        self.player.set_cleanup(False)
        self.cleanup_queue = []
        self.back_to_library()
        self.status_label.setText(f"Cleanup finished: kept {kept}, deleted {deleted}")

    def set_favorite(self, video_id: int, favorite: bool) -> None:
        self.db.set_fields(video_id, favorite=int(favorite))
        self._refresh_row(video_id)
        self.sidebar.refresh(self.show_hidden)

    def _delete_from_player(self, video_id: int) -> None:
        queue = list(self.queue)
        following = None
        if video_id in queue:
            i = queue.index(video_id)
            following = queue[i + 1] if i + 1 < len(queue) else (queue[i - 1] if i > 0 else None)
        if not self.delete_videos([video_id]):
            self.player.setFocus()
            return
        self.queue = [q for q in queue if q != video_id]
        if following is not None:
            self.play_video(following, queue=self.queue)
        else:
            self.back_to_library()

    # ------------------------------------------------------------ deleting

    def delete_videos(self, ids: list[int], confirm: bool = True) -> bool:
        videos = [v for v in (self.db.get_video(i) for i in ids) if v]
        if not videos:
            return False
        permanent = self.db.get_setting("delete_mode", "trash") == "permanent"
        count = len(videos)
        noun = "video" if count == 1 else "videos"
        size = fmt_size(sum(v["size"] for v in videos))
        names = "\n".join("• " + display_title(v) for v in videos[:8])
        if count > 8:
            names += f"\n…and {count - 8} more"
        if permanent:
            question = f"Permanently delete {count} {noun} ({size})?\n\n{names}\n\nThis can't be undone."
        else:
            question = (f"Move {count} {noun} ({size}) to the trash?\n\n{names}\n\nThey stay on the drive in a "
                        "hidden .blueshelf-trash folder until you empty the trash (Tools → Trash).")
        if confirm:
            answer = QMessageBox.question(self, "Delete", question, QMessageBox.Yes | QMessageBox.Cancel,
                                          QMessageBox.Cancel)
            if answer != QMessageBox.Yes:
                return False
        if self.player.video is not None and self.player.video["id"] in ids:
            self.player.stop()
        roots = {lib["id"]: lib["path"] for lib in self.db.libraries()}
        errors = []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for v in videos:
                root = roots.get(v["library_id"], os.path.dirname(v["path"]))
                try:
                    if permanent:
                        trash.delete_permanently(v["path"], root)
                        self.db.delete_videos([v["id"]])
                        remove_thumb(v["id"])
                    else:
                        item, origin = trash.move_to_trash(v["path"], root)
                        self.db.mark_trashed([v["id"]], item, origin)
                except OSError as exc:
                    errors.append(f"{os.path.basename(v['path'])}: {exc.strerror or exc}")
            self.db.cleanup_orphans()
        finally:
            QApplication.restoreOverrideCursor()
        if errors:
            QMessageBox.warning(self, "Some videos couldn't be deleted",
                                "\n".join(errors[:15]) + "\n\nIf a video is open in another program, close it "
                                                         "and try again.")
        deleted = count - len(errors)
        if deleted:
            self.status_label.setText(f"{'Deleted' if permanent else 'Moved to trash:'} {deleted} {noun}")
        self.refresh_all()
        return deleted > 0

    # ------------------------------------------------------------ scanning

    def scan(self, reparse: bool = False, quiet: bool = False) -> None:
        """Look for new and changed videos. Quiet scans are the ones nobody asked for."""
        if not self.db.libraries():
            return
        if self.scan_thread is not None and self.scan_thread.isRunning():
            self._rescan_pending = True
            self._rescan_reparse = self._rescan_reparse or reparse
            return
        self._last_scan_at = time.time()
        self.scan_thread = ScanThread(reparse=reparse, parent=self)
        self.scan_thread.progress.connect(self._scan_progress)
        self.scan_thread.library_changed.connect(lambda: self._refresh_timer.start())
        self.scan_thread.thumb_ready.connect(self._thumb_ready)
        self.scan_thread.done.connect(self._scan_done)
        if not quiet:
            self.progress.setRange(0, 0)
            self.progress.show()
        self.scan_thread.start()

    def _auto_scan(self) -> None:
        """A quiet look for new videos, every so often, when nothing else is going on."""
        minutes = int(self.db.get_setting("auto_scan_minutes", 10) or 0)
        if minutes <= 0 or not self.db.libraries():
            return
        if self.scan_thread is not None and self.scan_thread.isRunning():
            return
        if time.time() - self._last_scan_at < minutes * 60:
            return
        self.scan(quiet=True)

    def _scan_progress(self, message: str, done: int, total: int) -> None:
        self.status_label.setText(f"{message}… {done} of {total}" if total else f"{message}…")
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            self.progress.setRange(0, 0)

    def _thumb_ready(self, video_id: int, name: str) -> None:
        self.cache.invalidate(video_id)
        self.model.set_thumb(video_id, name)
        self.details.refresh_thumb(video_id)

    def _scan_done(self, summary: str) -> None:
        self.progress.hide()
        if self._tag_passthrough:
            self._mark_passthroughs()
        self.status_label.setText(summary)
        self.refresh_all()
        if self._rescan_pending:
            reparse = self._rescan_reparse
            self._rescan_pending = self._rescan_reparse = False
            self.scan(reparse=reparse)

    def _reparse_all(self) -> None:
        answer = QMessageBox.question(self, "Re-read file names",
                                      "Work out titles, performers, studios and categories again for every video? "
                                      "Anything you edited by hand is kept.")
        if answer == QMessageBox.Yes:
            self.scan(reparse=True)

    # ------------------------------------------------------------ other windows

    def add_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose the folder with your videos")
        if not folder:
            return
        folder = os.path.normpath(folder)
        problem = library_overlap(self.db, folder)
        if problem:
            QMessageBox.warning(self, "Folder already covered", problem)
            return
        self.db.add_library(folder)
        self.stack.setCurrentWidget(self.library_page)
        self.refresh_all()
        self.scan()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.db, self)
        accepted = bool(dialog.exec())
        if not self.db.libraries():
            self.stack.setCurrentWidget(self.welcome)
        elif self.stack.currentWidget() is self.welcome:
            self.stack.setCurrentWidget(self.library_page)
        if accepted:
            self.player.reload_settings()
            if dialog.theme_changed:
                self.apply_appearance()
        if accepted or dialog.libraries_added or dialog.libraries_removed:
            self.refresh_all()
        reparse = accepted and dialog.reparse_requested
        if dialog.libraries_added or reparse:
            self.scan(reparse=reparse)

    def review_names(self) -> None:
        dialog = NameReviewDialog(self.db, self)
        dialog.exec()
        if dialog.changed:
            self.refresh_all()

    def move_performer(self, performer_id: int, name: str) -> None:
        """Put one performer's scenes in a folder of their own - real files, with an undo."""
        dialog = MoveDialog(self.db, performer_id, name, self)
        dialog.changed.connect(self.refresh_all)
        dialog.exec()

    def make_passthrough(self, videos: list[dict] | None = None) -> None:
        """Cut the performer out of a scene so it plays against the room in a headset.

        Opened beside the library rather than over it, and kept for as long as BlueShelf is
        running: the list of videos, the scans and any work under way all survive closing the
        window, and closing it does not stop a job that is running.

        No parent is given on purpose. A window owned by the main one is pinned above it, and
        the whole point is to be able to watch a scene while deciding what to do with it.
        """
        if self.passthrough_window is None:
            self.passthrough_window = PassthroughDialog(self.db, videos or [])
            self.passthrough_window.made.connect(self._passthrough_made)
            self.passthrough_window.shown.connect(self._passthrough_shown)
            self.passthrough_window.wants_library.connect(self._show_library)
        elif videos:
            self.passthrough_window.add_videos(videos)
        self.passthrough_window.show()
        self.passthrough_window.raise_()
        self.passthrough_window.activateWindow()
        self._passthrough_shown(True)

    def help_another_computer(self) -> None:
        """Open straight onto the helping tab, for a machine that is only lending its card."""
        self.make_passthrough()
        self.passthrough_window.tabs.setCurrentIndex(1)

    def _toggle_passthrough(self, wanted: bool) -> None:
        if wanted:
            self.make_passthrough()
        elif self.passthrough_window is not None:
            self.passthrough_window.hide()

    def _passthrough_shown(self, showing: bool) -> None:
        if hasattr(self, "passthrough_action"):
            self.passthrough_action.setChecked(showing)

    def _passthrough_made(self, path: str) -> None:
        """A finished copy: find it, then mark it, once the scan has caught up with the file."""
        self._tag_passthrough[path] = 0
        self.scan(quiet=True)

    def _show_library(self, wanted: bool) -> None:
        """The passthrough window asking for the library to be shown or put away."""
        if wanted:
            self.show()
            self.raise_()
            self.activateWindow()
        else:
            self.hide()

    def _mark_passthroughs(self) -> None:
        """Tag whatever the last scan found. A copy still being written gets another chance."""
        for path, tries in list(self._tag_passthrough.items()):
            video_id = self.db.video_id_for_path(path)
            if video_id:
                self.db.add_tag_to([video_id], "Passthrough")
                del self._tag_passthrough[path]
            elif tries >= 3:
                del self._tag_passthrough[path]   # it is not coming: outside every library
            else:
                self._tag_passthrough[path] = tries + 1

    def build_shortcuts(self) -> None:
        OrganizeDialog(self.db, self).exec()

    def watch_remotely(self) -> None:
        RemoteDialog(self.db, self.remote, self, media_server=self.dlna).exec()
        self._check_remote()

    def open_backup(self) -> None:
        BackupDialog(self.db, self.restore_backup, self).exec()

    def restore_backup(self, path: str) -> None:
        """Swap in a backup, which means letting go of the database and starting again."""
        self.remote.stop()
        self.dlna.stop()
        self.player.shutdown()
        if self.scan_thread is not None and self.scan_thread.isRunning():
            self.scan_thread.stop()
            self.scan_thread.wait(8000)
        self.db.close()
        try:
            backup.restore(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not restore", f"{exc}\n\nBlueShelf will reopen with what it had.")
        self._restarting = True
        QProcess.startDetached(sys.executable, [] if getattr(sys, "frozen", False) else sys.argv)
        QApplication.quit()

    def open_trash(self) -> None:
        dialog = TrashDialog(self.db, self)
        dialog.exec()
        if dialog.changed:
            self.refresh_all()

    def bring_in_library(self) -> None:
        """Somebody arriving from Stash, XBVR, Kodi or a pile of folders, keeping their work."""
        from blueshelf.import_dialog import ImportDialog
        dialog = ImportDialog(self.db, self)
        dialog.changed.connect(self.refresh_all)
        dialog.exec()

    def write_sidecars(self) -> None:
        """A copy of what BlueShelf knows, in a file beside each film, so it is never trapped here."""
        from blueshelf import sidecars
        many = self.db.conn.execute("SELECT COUNT(*) FROM videos WHERE trashed = 0").fetchone()[0]
        keep_up = sidecars.wanted(self.db)
        answer = QMessageBox(self)
        answer.setWindowTitle("Write my library beside the films")
        answer.setText(
            f"Write a small .nfo file next to each of your {many} films, holding its performers, "
            f"tags, rating, marked scenes and how far through you were.\n\n"
            f"Kodi, Jellyfin, Emby and Plex read these, and BlueShelf reads them back - so nothing "
            f"you organise here is trapped here. Your videos themselves are not touched.")
        write = answer.addButton("Write them now", QMessageBox.AcceptRole)
        keep = answer.addButton("Stop keeping them up to date" if keep_up else "Write them and keep them up to date",
                                QMessageBox.ActionRole)
        remove = answer.addButton("Remove the ones BlueShelf wrote", QMessageBox.DestructiveRole)
        answer.addButton("Cancel", QMessageBox.RejectRole)
        answer.exec()
        pressed = answer.clickedButton()
        if pressed is remove:
            gone = sidecars.remove_all(self.db)
            self.db.set_setting(sidecars.WRITE_SETTING, False)
            QMessageBox.information(self, "Done", f"Removed {gone}.")
            return
        if pressed is keep:
            self.db.set_setting(sidecars.WRITE_SETTING, not keep_up)
            if keep_up:
                QMessageBox.information(self, "Done", "They will no longer be kept up to date.")
                return
        elif pressed is not write:
            return
        counted = sidecars.write_all(self.db)
        QMessageBox.information(self, "Done", f"Written beside {counted['written']} films."
                                + (f"\n\n{counted['could not']} could not be written - a disc that "
                                   f"cannot be written to, or a film that has gone."
                                   if counted["could not"] else ""))

    def find_duplicates(self) -> None:
        if self.dupes_dialog is None:
            self.dupes_dialog = DupesDialog(self.db, self.cache, self.delete_videos, self.play_video, self)
            self.dupes_dialog.finished.connect(self._dupes_closed)
        self.dupes_dialog.show()
        self.dupes_dialog.raise_()
        self.dupes_dialog.activateWindow()

    def _dupes_closed(self) -> None:
        if self.dupes_dialog is not None:
            self.dupes_dialog.deleteLater()
            self.dupes_dialog = None

    def show_in_folder(self, path: str) -> None:
        if not os.path.exists(path):
            QMessageBox.warning(self, "File not found", f"BlueShelf can't find this file:\n{path}")
            return
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')

    def _about(self) -> None:
        QMessageBox.about(self, "About BlueShelf",
                          f"<b>BlueShelf {__version__}</b><br>A private library and player for your video collection."
                          f"<br><br>Playback by mpv (libmpv). Video analysis by FFmpeg.<br>"
                          f"Library data: {paths.data_dir()}")

    def closeEvent(self, event) -> None:
        if self.isFullScreen():
            self.toggle_fullscreen()
        self._save_state()
        self.remote.stop()
        self.dlna.stop()  # says goodbye, so players drop it at once
        self.player.shutdown()
        if self.dupes_dialog is not None:
            self.dupes_dialog.close()
        if self.passthrough_window is not None:
            # It ignores an ordinary close so a job can carry on without the window open.
            # This is the one that means it: stop the work, then let it go.
            self.passthrough_window.shut_down()
            self.passthrough_window.deleteLater()
            self.passthrough_window = None
        if self.scan_thread is not None and self.scan_thread.isRunning():
            self.scan_thread.stop()
            if not self.scan_thread.wait(8000):
                self.force_exit = True
        QThreadPool.globalInstance().waitForDone(3000)
        super().closeEvent(event)


_last_error_box = 0.0


def _install_excepthook() -> None:
    log_path = paths.data_dir() / "blueshelf.log"

    def hook(exc_type, exc, tb):
        global _last_error_box
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}")
        except OSError:
            pass
        if sys.__stderr__ is not None:
            sys.__stderr__.write(text)
        if QApplication.instance() is not None and time.time() - _last_error_box > 10:
            _last_error_box = time.time()
            QMessageBox.critical(None, "BlueShelf hit a problem", f"{exc}\n\nThe details were saved to {log_path}.")

    sys.excepthook = hook


def main() -> None:
    _install_excepthook()
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BlueShelf.VideoLibrary")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("BlueShelf")
    apply_theme(app)
    app.setWindowIcon(QIcon(make_icon(256)))
    window = MainWindow()
    # A second machine lending its graphics card has no use for the library: no scanning,
    # no browsing, nothing to pick. Started this way it goes straight to the helping tab
    # and the library stays out of the way.
    if "--helper" in sys.argv[1:]:
        window.help_another_computer()
    elif window.db.get_setting("window_geometry"):
        window.show()
    else:
        window.showMaximized()
    code = app.exec()
    if window.force_exit:
        os._exit(code)
    sys.exit(code)
