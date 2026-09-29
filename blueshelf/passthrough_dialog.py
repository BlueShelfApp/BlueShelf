"""Making passthrough copies, on this computer or spread across several."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenuBar, QMessageBox,
                               QPlainTextEdit,
                               QApplication, QFrame, QProgressBar, QPushButton,
                               QScrollArea, QSlider, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from blueshelf import farm, passthrough, setup_matte
from blueshelf.db import Database
from blueshelf.passthrough import (DETAIL, DETAIL_WHY, Job, Slice, Stretch,
                                   Unavailable)
from blueshelf.library import VIDEO_MIME
from blueshelf.pick_dialog import PickDialog
from blueshelf.scenes_tab import ScenesTab
from blueshelf.timeline import Timeline
from blueshelf.widgets import no_stray_scrolling

MODELS = [("mobilenetv3", "Fast - good on hair and edges, and what most scenes want"),
          ("resnet50", "Slower and a little cleaner, for a scene the fast one struggles with"),
          ("matanyone2", "Best by far, and about three times slower. The others decide what "
                         "counts as a person every frame and lose her when she bends over or "
                         "is dressed; this one is shown who to follow and remembers")]
# The last one is an extra VRAutoMatte does not install by default, so it is only offered
# where it is actually present - otherwise it fails hours in with an import error.
NEEDS_EXTRA = {"matanyone2"}
VIDEO_FILTER = "Videos (*.mp4 *.mkv *.mov *.m4v *.webm *.avi *.wmv)"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".wmv"}


class _Thread(QThread):
    """Common shape for the long jobs: talk while working, say how each one ended."""

    line = Signal(str)
    step = Signal(str, float, str)          # stage, 0..1 through this video, detail
    overall = Signal(int, int, str)         # finished, total, the one being worked on
    one_done = Signal(str)                  # a finished file
    ended = Signal(str)                     # trouble, empty when all went well
    counts = Signal(str, dict)              # a video's name and where its shares have got to

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stop_wanted = False      # drop what is in hand
        self.finish_first = False     # stop, but let the chunk in hand finish

    def _assemble(self, job_id: str, job: Job, pieces: list[Slice]) -> str:
        """Join the mask, then share the tail out and join that.

        The tail is projecting and packing, and it carries no state between frames - so it
        goes back into the same queue as ranges any machine can take, instead of being done
        alone by whoever happened to start the job.
        """
        folder = passthrough.work_folder(job)
        matte = folder / "matte.mp4"
        # Asked before the chunks are, because a finished mask makes them beside the point -
        # including any that have gone missing since it was made.
        if passthrough.finished(matte, sum(p.kept for p in pieces)):
            self.line.emit(f"The mask for {Path(job.source).name} is already made.")
        else:
            if not self.share.segments(job_id):
                raise Unavailable("Some segments are missing from the shared folder.")
            self.line.emit(f"Putting {Path(job.source).name} back together.")
            matte = passthrough.build_matte(
                job, pieces, lambda piece: self.share.segment_of(job_id, piece),
                folder, lambda s, f, d: self.step.emit(s, f, d))
        if job.make == "matte":
            # Just the cut-out: once it is joined, the film is ready to watch in BlueShelf.
            # No packing, no copy of the film, nothing re-encoded.
            kept = passthrough.keep_cutout(job, matte, lambda s, f, d: self.step.emit(s, f, d))
            passthrough.tidy_after(job, folder)
            self.line.emit(f"Done: the cut-out for {Path(job.source).name} is ready to "
                           f"play with it in BlueShelf ({Path(kept).name}).")
            # The film itself is what gets watched now, so that is what the library marks.
            return str(job.source)
        return self._share_the_tail(job, matte, folder)

    def _share_the_tail(self, job: Job, matte, folder) -> str:
        """Hand the projecting and packing round the machines, then join what comes back."""
        frames = passthrough.packable_frames(matte, job.source)
        slice_size = job.pack_chunk or 2500
        pieces = passthrough.tail_pieces(frames, slice_size)
        job_id = self.share.post(job, pieces, stage="pack",
                                 extra={"matte": str(matte), "picture": job.source})
        self.line.emit(f"Sharing out the last stage: {len(pieces)} pieces of "
                       f"{slice_size:,} frames. Any machine can take any of them.")
        helper = farm.Helper(self.share, self.install, self.line.emit,
                             lambda s, f, d: self.step.emit(s, f, d),
                             should_stop=lambda: self.stop_wanted)
        told = None
        while not self.stop_wanted and not self.finish_first:
            state = self.share.state(job_id)
            if state != told:
                self.counts.emit(Path(job.source).name, state)
                told = state
            if state["complete"]:
                break
            # A piece that failed once is put back on its own after a rest, by whichever
            # machine is looking - this one included - so only a second failure ends it.
            if (state["failed"] and not state["waiting"] and not state["running"]
                    and not state["retrying"]):
                raise Unavailable("Some pieces of the last stage failed twice:\n"
                                  + "\n".join(state["failures"][:4]))
            if not helper.one():
                self.share.recover()
                for _tick in range(20):
                    if self.stop_wanted or self.finish_first:
                        break
                    time.sleep(0.5)
        if not self.share.state(job_id)["complete"]:
            raise Unavailable("Stopped before the last stage finished. What is done is kept.")

        made = [self.share.segment_of(job_id, p) for p in pieces]
        self.share.forget(job_id)
        self._get_it_joined(job, made)
        for part in made:
            try:
                part.unlink()
            except OSError:
                pass
        if job.keep_mask:
            self.line.emit("Keeping the cut-out as well, for BlueShelf's own player.")
            passthrough.keep_cutout(job, matte, lambda s, f, d: self.step.emit(s, f, d))
        else:
            try:
                matte.unlink()
            except OSError:
                pass
        if job.small_mask:      # a job filed before small masks leaves its chunks, as it always did
            passthrough.tidy_after(job, folder)
        self.line.emit(f"Done: {Path(job.output).name}")
        return str(job.output)

    def _get_it_joined(self, job: Job, made: list) -> None:
        """Have the pieces joined, by whichever machine is best placed to do it.

        Joining is nothing but moving bytes, so it belongs on the machine with the best
        link rather than on whoever started the job. It goes into the queue like any other
        work; this machine takes it if nobody is better placed, and takes it anyway if the
        better one does not turn up.
        """
        one = [Slice(0, 1, 1, 1)]
        job_id = self.share.post(job, one, stage="join",
                                 extra={"inputs": [str(f) for f in made],
                                        "output": job.output})
        best = self.share.best_for_the_tail()
        if best and best.get("machine") != self.share.machine:
            self.line.emit(f"Leaving the joining to {best['machine']} - it has the better "
                           f"link, and joining is all reading and writing.")
        helper = farm.Helper(self.share, self.install, self.line.emit,
                             lambda s, f, d: self.step.emit(s, f, d),
                             should_stop=lambda: self.stop_wanted)
        while not self.stop_wanted:
            if self.share.state(job_id)["complete"]:
                break
            if not helper.one():
                self.share.recover()
                for _tick in range(20):
                    if self.stop_wanted:
                        break
                    time.sleep(0.5)
        if not self.share.state(job_id)["complete"]:
            raise Unavailable("Stopped before the pieces were joined. They are all kept.")
        self.share.forget(job_id)

    def stop(self, after_this: bool = False) -> None:
        """Stop. ``after_this`` lets the chunk being matted finish first.

        Which one you want depends entirely on how far in it is - a chunk abandoned at
        frame 4,900 of 5,000 has thrown away nine minutes, and one abandoned at frame 50
        has thrown away nothing.
        """
        if after_this:
            self.finish_first = True
        else:
            self.stop_wanted = True


class _Probe(_Thread):
    """Ask an install what it is capable of, without freezing the window while torch loads."""

    answered = Signal(dict)

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = root

    def run(self) -> None:
        self.answered.emit(passthrough.describe(self.root))


class _Setting(_Thread):
    """Fetch and build VRAutoMatte, so nobody has to open a terminal to get started."""

    def __init__(self, home, parent=None):
        super().__init__(parent)
        self.home = home
        self.made = None

    def run(self) -> None:
        try:
            self.made = setup_matte.set_up(
                self.home,
                lambda stage, share, detail: self.step.emit(stage, share, detail),
                should_stop=lambda: self.stop_wanted)
            self.ended.emit("")
        except setup_matte.Stopped:
            self.ended.emit("Stopped. What was downloaded is kept, so starting again "
                            "carries on rather than begins from nothing.")
        except Exception as trouble:  # noqa: BLE001 - the window must always come back
            self.ended.emit(str(trouble))


class _Look(_Thread):
    """Read a video through cheaply to find where the action is."""

    reading = Signal(str, dict)

    def __init__(self, install, source: str, parent=None):
        super().__init__(parent)
        self.install, self.source = install, source

    def run(self) -> None:
        try:
            found = passthrough.look(self.install, self.source,
                                     lambda s, f, d: self.step.emit(s, f, d),
                                     should_stop=lambda: self.stop_wanted)
            self.reading.emit(self.source, found)
            self.ended.emit("")
        except InterruptedError:
            self.ended.emit("Stopped.")
        except Exception as exc:  # noqa: BLE001
            self.ended.emit(str(exc))


class _SoFar(_Thread):
    """Build a copy out of the chunks that are done, without matting anything more."""

    def __init__(self, install, job: Job, pieces: list[Slice], parent=None):
        super().__init__(parent)
        self.install, self.job, self.pieces = install, job, pieces

    def run(self) -> None:
        try:
            self.line.emit(f"Building what is done of {Path(self.job.source).name}.")
            made = passthrough.make_so_far(self.install, self.job, self.pieces,
                                           lambda s, f, d: self.step.emit(s, f, d))
            self.one_done.emit(str(made))
            self.line.emit(f"Made {Path(made).name}")
            self.ended.emit("")
        except Exception as exc:  # noqa: BLE001
            self.ended.emit(str(exc))


class _Bench(_Thread):
    """Measure this machine - its link to the shared folder, and how fast it mattes."""

    measured = Signal(dict)

    def __init__(self, install, share: farm.Share, job: Job | None, parent=None):
        super().__init__(parent)
        self.install, self.share, self.job = install, share, job

    def run(self) -> None:
        facts: dict = {}
        try:
            self.line.emit("Timing the link to the shared folder…")
            self.step.emit("Timing the link", 0.1, "writing and reading 256 MB")
            facts.update(passthrough.measure_link(self.share.root))
            self.line.emit(f"Link: {facts['write_mbs']:g} MB/s out, "
                           f"{facts['read_mbs']:g} MB/s back.")

            if self.install is not None:
                found = passthrough.describe(self.install)
                facts["gpu"] = found.get("gpu", "")
                facts["vram_gb"] = found.get("vram_gb", 0)
            if self.job is None:
                self.line.emit("No video to matte, so only the link was timed. Add one on "
                               "the first tab, or wait until there is work in the shared "
                               "folder, to time the graphics card as well.")
            elif self.install is None:
                self.line.emit("VRAutoMatte is not set up here, so the card was not timed.")
            else:
                self.line.emit("Matting a few seconds of the video to time the card…")
                facts["matte_fps"] = passthrough.measure_matting(
                    self.install, self.job,
                    report=lambda s, f, d: self.step.emit("Timing the card", 0.2 + 0.7 * f, d))
                self.line.emit(f"Matting: {facts['matte_fps']:g} frames a second at "
                               f"{self.job.detail} detail, on "
                               f"{Path(self.job.source).name}.")
            self.share.publish_machine(facts)
            self.measured.emit(facts)
            self.ended.emit("")
        except Exception as exc:  # noqa: BLE001
            self.ended.emit(str(exc))


class _Batch(_Thread):
    """Every video on the list, one after another, on this computer.

    One that goes wrong does not stop the rest: an overnight run of eight scenes should not be
    lost because the fourth had something odd about it.
    """

    def __init__(self, install, work: list[tuple[Job, list[Slice]]], parent=None):
        super().__init__(parent)
        self.install, self.work = install, work

    def run(self) -> None:
        trouble = []
        for at, (job, pieces) in enumerate(self.work):
            if self.stop_wanted or self.finish_first:
                break
            name = Path(job.source).name
            self.overall.emit(at, len(self.work), name)
            skipped = sum(p.kept for p in pieces if p.blank)
            note = (f" - skipping {skipped / max(1.0, job.fps) / 60:.0f} minutes with no action"
                    if skipped else "")
            self.line.emit(f"({at + 1} of {len(self.work)}) {name}{note}")
            try:
                if len(pieces) > 1 or any(p.blank for p in pieces):
                    made = passthrough.make_in_pieces(
                        self.install, job, pieces,
                        lambda s, f, d: self.step.emit(s, f, d),
                        should_stop=lambda: self.stop_wanted,
                        note=self.line.emit)
                else:
                    made = passthrough.make_here(
                        self.install, job,
                        lambda s, f, d: self.step.emit(s, f, d),
                        should_stop=lambda: self.stop_wanted)
                # A cut-out is watched with the film itself, so the film is what gets marked.
                self.one_done.emit(str(job.source) if job.make == "matte" else str(made))
                self.line.emit(f"Done: {Path(made).name}")
            except InterruptedError:
                break
            except Exception as exc:  # noqa: BLE001 - one bad scene must not end the night
                trouble.append(f"{name}: {exc}")
                self.line.emit(f"Failed: {name} - {exc}")
        self.overall.emit(len(self.work), len(self.work), "")
        self.ended.emit("\n".join(trouble))


class _Shared(_Thread):
    """Put the videos in the shared folder, take shares here, and put each one back together.

    Every video is filed up front rather than one at a time, so another machine joining in has
    plenty to pick up and is never left waiting on this one.

    This machine is a helper like any other while the work goes on. What makes it the one in
    charge is only that it notices when a video's last share arrives and does the joining,
    which needs no graphics card.
    """


    def __init__(self, install, share: farm.Share, work: list[tuple[Job, list[Slice]]],
                 parent=None):
        super().__init__(parent)
        self.install, self.share, self.work = install, share, work
        self.filed: dict[str, Job] = {}
        self.plans: dict[str, list[Slice]] = {}

    def run(self) -> None:
        trouble = []
        try:
            for job, pieces in self.work:
                # Anything this machine already matted on its own is taken over rather than
                # queued again - stopping alone and carrying on with help costs nothing.
                already = passthrough.work_folder(job)
                moved = passthrough.gather_older_work(job, pieces)
                if moved:
                    self.line.emit(f"Brought {moved} chunks over from a folder an older "
                                   f"version used.")
                rescued = self.share.reclaim_old_layout(job, pieces)
                if rescued:
                    self.line.emit(f"Brought back {rescued} chunks filed by an older version "
                                   f"inside the job folder.")
                had = self.share.ready_made(pieces, already)
                job_id = self.share.post(job, pieces)
                self.filed[job_id] = job
                self.plans[job_id] = pieces
                real = [p for p in pieces if not p.blank]
                skipped = sum(p.kept for p in pieces if p.blank)
                note = (f", skipping {skipped / max(1.0, job.fps) / 60:.0f} minutes with no "
                        f"action" if skipped else "")
                kept = (f" Took over {had} chunk{'s' if had != 1 else ''} already matted "
                        f"here." if had else "")
                self.line.emit(f"Filed {Path(job.source).name}: {len(real)} shares{note}."
                               + kept)
            self.line.emit("Other computers can pick these up from the same folder - open this "
                           "window there and use the second tab.")

            helper = farm.Helper(self.share, self.install, self.line.emit,
                                 lambda s, f, d: self.step.emit(s, f, d),
                                 should_stop=lambda: self.stop_wanted)
            self.share.release_mine()
            waiting = dict(self.filed)
            told: dict[str, dict] = {}
            while waiting and not self.stop_wanted and not self.finish_first:
                for job_id in list(waiting):
                    state = self.share.state(job_id)
                    if told.get(job_id) != state:
                        told[job_id] = state
                        self.counts.emit(Path(waiting[job_id].source).name, state)
                    if state["complete"]:
                        try:
                            self.one_done.emit(
                                self._assemble(job_id, waiting[job_id],
                                               self.plans[job_id]))
                        except Exception as exc:  # noqa: BLE001
                            trouble.append(f"{Path(waiting[job_id].source).name}: {exc}")
                            self.line.emit(f"Could not put it back together: {exc}")
                        del waiting[job_id]
                    elif state["failed"] and not state["waiting"] and not state["running"]:
                        # One more go before writing the video off. That is now given by
                        # whichever machine is looking, after a rest, and counted in the
                        # share itself - so it happens even with this machine switched off,
                        # and only once however many machines are watching.
                        if state["retrying"]:
                            continue
                        trouble.append(f"{Path(waiting[job_id].source).name}: shares "
                                       f"failed twice\n"
                                       + "\n".join(state["failures"][:4]))
                        del waiting[job_id]
                self.overall.emit(len(self.filed) - len(waiting), len(self.filed), "")
                if not waiting:
                    break
                if not helper.one():
                    # Nothing left to take: the other machines have the rest. Wait on them.
                    self.share.recover()
                    for _tick in range(20):
                        if self.stop_wanted:
                            break
                        time.sleep(0.5)
            if self.stop_wanted:
                trouble.append("Stopped. What was filed stays in the shared folder, so this can "
                               "be picked up again later.")
        except Exception as exc:  # noqa: BLE001
            trouble.append(str(exc))
        self.ended.emit("\n".join(trouble))

class _Helping(_Thread):
    """This computer, lending its card to whatever is in the shared folder."""

    def __init__(self, install, share: farm.Share, parent=None):
        super().__init__(parent)
        self.install, self.share = install, share

    def run(self) -> None:
        helper = farm.Helper(self.share, self.install, self.line.emit,
                             lambda s, f, d: self.step.emit(s, f, d),
                             should_stop=lambda: self.stop_wanted)
        self.line.emit(f"Watching {self.share.root} as {self.share.machine}.")
        # Anything this machine was holding when it last stopped is certainly not being
        # worked on now, and leaving it to time out keeps it from every other machine.
        mine = self.share.release_mine()
        if mine:
            self.line.emit(f"Handed back {mine} chunks this computer was still holding from "
                           f"an earlier run.")
        try:
            while not self.stop_wanted and not self.finish_first:
                if self._adopt_a_stranded_one():
                    continue
                if not helper.one():
                    for _tick in range(30):
                        if self.stop_wanted or self.finish_first:
                            break
                        time.sleep(0.5)
            helper.stop()
            self.ended.emit("")
        except Exception as exc:  # noqa: BLE001
            self.ended.emit(str(exc))

    def _adopt_a_stranded_one(self) -> bool:
        """Put together a finished job whose owner has gone, since nobody else will.

        Helping used to mean only taking shares, so a film could be matted right to the end
        and then sit there because the machine that started it had been closed. Everything
        was on disk and none of it was going anywhere.
        """
        for job_id in self.share.jobs():
            if self.stop_wanted or not self.share.stranded(job_id):
                continue
            if not self.share.take_over(job_id):
                continue          # another helper got there first
            try:
                job, pieces = self.share.read_job(job_id)
                self.line.emit(f"{Path(job.source).name} was matted right through and left "
                               f"behind. Putting it together here.")
                self.one_done.emit(self._assemble(job_id, job, pieces))
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the helping
                self.line.emit(f"Could not put it together: {exc}")
            return True
        return False


# --------------------------------------------------------------------------- the window

class PassthroughDialog(QDialog):
    """Pick videos, and either make the copies here or share the work out."""

    made = Signal(str)          # a finished copy, for the library to pick up and mark
    shown = Signal(bool)        # whether this window is on screen
    wants_library = Signal(bool)  # asking for the library window to be shown or put away

    def __init__(self, db: Database, videos: list[dict] | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.install: Path | None = None
        self.fov_touched = False
        self.jobs: dict[str, Job] = {}        # what each file would become, by path
        self.facts: dict[str, dict] = {}      # size and frame count, measured or from the library
        self.unreadable: set[str] = set()
        self.readings: dict[str, dict] = {}   # how much skin over time, by path, once scanned
        self.stretches: dict[str, list] = {}  # the parts worth matting, by path
        self.recalled: set[str] = set()       # scans that came back from an earlier visit
        self.stopping = ""
        self.worker: _Thread | None = None
        self.probe: _Probe | None = None
        self.setter: _Setting | None = None
        self.setWindowTitle("Make passthrough copies")
        # Opens at a size that fits a 1080p screen with room to spare, and can be dragged
        # much smaller than that - the pages scroll rather than refusing to shrink.
        self.resize(900, min(760, _screen_room()))
        self.setMinimumSize(620, 420)
        # Videos can be dragged in: out of the library behind, or off the desktop.
        self.setAcceptDrops(True)

        self.library_action = QAction("Library window", self)
        self.library_action.setCheckable(True)
        self.library_action.setChecked(True)
        self.library_action.setShortcut(QKeySequence("Ctrl+L"))
        self.library_action.toggled.connect(self.wants_library)
        self.addAction(self.library_action)
        bar = QMenuBar(self)
        looking = bar.addMenu("&View")
        looking.addAction(self.library_action)
        close_here = QAction("Close this window", self)
        close_here.setShortcut(QKeySequence("Ctrl+W"))
        close_here.triggered.connect(self.close)
        looking.addSeparator()
        looking.addAction(close_here)
        self.addAction(close_here)

        self.tabs = QTabWidget()
        # Both pages scroll. There is a lot on the first one, and a window that insists on
        # being sixteen hundred pixels tall simply does not fit on many screens - including
        # a monitor turned on its side.
        self.tabs.addTab(_scrolling(self._make_tab()), "Make copies")
        self.tabs.addTab(_scrolling(self._help_tab()), "Help another computer")
        # Last, so the tabs before it keep their places: the helping tab is looked for by index.
        self.scenes_tab = ScenesTab(self.db)
        self.scenes_tab.ready.connect(self._scene_clip_ready)
        self.tabs.addTab(_scrolling(self.scenes_tab), "From scenes")

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.RichText)
        layout = QVBoxLayout(self)
        layout.setMenuBar(bar)
        layout.addWidget(self._install_box())
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.status)

        self._load_settings()
        self._find_install()
        for video in videos or []:
            self._append_source(video["path"])
        self._refresh()

    # ------------------------------------------------------------ where VRAutoMatte is

    def _install_box(self) -> QGroupBox:
        box = QGroupBox("The matting program")
        self.install_line = QLineEdit()
        self.install_line.setPlaceholderText("the folder VRAutoMatte was unpacked into")
        self.install_line.editingFinished.connect(self._install_typed)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_install)
        self.check_btn = QPushButton("Check it")
        self.check_btn.clicked.connect(self._check_install)
        self.install_state = QLabel()
        self.install_state.setWordWrap(True)
        self.install_state.setTextFormat(Qt.RichText)

        self.setup_btn = QPushButton("Set it up for me")
        self.setup_btn.clicked.connect(self._set_it_up)
        self.setup_bar = QProgressBar()
        self.setup_bar.setRange(0, 1000)
        self.setup_bar.setVisible(False)
        self.setup_line = QLabel()
        self.setup_line.setWordWrap(True)
        self.setup_line.setVisible(False)

        row = QHBoxLayout()
        row.addWidget(self.install_line, 1)
        row.addWidget(browse)
        row.addWidget(self.check_btn)
        doing = QHBoxLayout()
        doing.addWidget(self.setup_btn)
        doing.addWidget(self.setup_bar, 1)
        inner = QVBoxLayout(box)
        note = QLabel(
            "Cutting the performer out of a scene is done by VRAutoMatte, which BlueShelf runs "
            "for you. It is a separate download because it carries PyTorch and a CUDA build "
            "with it - several gigabytes, and no use without an NVIDIA graphics card.<br><br>"
            "<b>Set it up for me</b> does the lot: it fetches uv, which brings its own Python, "
            "then VRAutoMatte, then builds it. Nothing needs installing first and there is no "
            "terminal to open. Expect a long wait on the last step.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)
        inner.addWidget(note)
        inner.addLayout(row)
        inner.addLayout(doing)
        inner.addWidget(self.setup_line)
        inner.addWidget(self.install_state)
        return box

    # ------------------------------------------------------------ setting it up

    def _set_it_up(self) -> None:
        if self.setter is not None:            # already running: this is the stop button
            self.setter.stop()
            self.setup_btn.setEnabled(False)
            self.setup_btn.setText("Stopping…")
            return
        typed = self.install_line.text().strip()
        home = Path(typed) if typed else setup_matte.default_home()
        if home.exists() and any(home.iterdir()) and not (home / "pyproject.toml").is_file():
            QMessageBox.warning(
                self, "That folder is not empty",
                f"{home} already has things in it that are not VRAutoMatte. Choose an empty "
                f"folder, or clear the box to use the usual place.")
            return
        settled = QMessageBox.question(
            self, "Set up VRAutoMatte?",
            f"This will download and build VRAutoMatte in:\n\n{home}\n\n"
            f"Several gigabytes, mostly PyTorch, and it can take a long while on a slow "
            f"connection. You can stop it and start again later without losing what came "
            f"down. Go ahead?")
        if settled != QMessageBox.Yes:
            return
        self.setup_bar.setValue(0)
        self.setup_bar.setVisible(True)
        self.setup_line.setVisible(True)
        self.setup_line.setText("Starting…")
        self.setup_btn.setText("Stop")
        self.install_state.setText("")
        self.setter = _Setting(home, self)
        self.setter.step.connect(self._setting_step)
        self.setter.ended.connect(self._setting_ended)
        self.setter.start()
        self._refresh()

    def _setting_step(self, stage: str, share: float, detail: str) -> None:
        self.setup_bar.setValue(int(max(0.0, min(1.0, share)) * 1000))
        self.setup_line.setText(f"{stage} — {detail}" if detail else stage)

    def _setting_ended(self, trouble: str) -> None:
        made = self.setter.made if self.setter is not None else None
        self.setter = None
        self.setup_btn.setEnabled(True)
        self.setup_btn.setText("Set it up for me")
        self.setup_bar.setVisible(False)
        if trouble:
            self.setup_line.setText(trouble)
            self._refresh()
            return
        self.setup_line.setVisible(False)
        if made is not None:
            self.install_line.setText(str(made).replace("/", "\\"))
            self._install_typed()
            self._check_install()

    def _browse_install(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Where is VRAutoMatte?",
                                                  self.install_line.text())
        if chosen:
            self.install_line.setText(chosen.replace("/", "\\"))
            self._install_typed()

    def _install_typed(self) -> None:
        text = self.install_line.text().strip()
        self.install = Path(text) if text and passthrough.looks_like_install(text) else None
        if text and self.install is None:
            self.install_state.setText(
                "<b>That folder does not look like VRAutoMatte.</b> It should contain "
                "src\\vrautomatte and a .venv folder. If the .venv is missing it has not been "
                "set up yet: install uv (<tt>winget install astral-sh.uv</tt>) and run "
                "<tt>uv sync</tt> in that folder, which downloads PyTorch and takes a while. "
                "Not its run.bat - that asks for an extra whose build is broken upstream.")
        elif self.install is not None:
            self.db.set_setting("vrautomatte_path", str(self.install))
            self.install_state.setText("Found it. Press <b>Check it</b> to see which graphics "
                                       "card it will use.")
        self._refresh()

    def _find_install(self) -> None:
        found = passthrough.find_install(self.db.get_setting("vrautomatte_path"))
        if found:
            self.install = found
            self.install_line.setText(str(found))
            self.install_state.setText("Found it. Press <b>Check it</b> to see which graphics "
                                       "card it will use.")
        else:
            self.install_state.setText(
                "<b>Not found.</b> Download VRAutoMatte, install uv "
                "(<tt>winget install astral-sh.uv</tt>), then run <tt>uv sync</tt> in its "
                "folder to build it. Point this at the folder afterwards.")

    def _check_install(self) -> None:
        if self.install is None:
            return
        self.check_btn.setEnabled(False)
        self.install_state.setText("Asking it… the first time takes a minute or two while it "
                                   "wakes the graphics card.")
        self.probe = _Probe(self.install, self)
        self.probe.answered.connect(self._probed)
        self.probe.start()

    def _probed(self, found: dict) -> None:
        self.check_btn.setEnabled(True)
        if found.get("ok"):
            self.install_state.setText(
                f"Ready. <b>{found['gpu']}</b>, {found['vram_gb']:g} GB, "
                f"PyTorch {found['torch']}.")
        else:
            self.install_state.setText(f"<b>Not usable yet.</b> {found.get('error', '')}")
        self._refresh()

    # ------------------------------------------------------------ tab one: making copies

    def _make_tab(self) -> QWidget:
        page = QWidget()
        self.sources = QListWidget()
        self.sources.setMinimumHeight(80)
        self.sources.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.sources.currentRowChanged.connect(self._source_changed)

        from_library = QPushButton("Add from library…")
        from_library.clicked.connect(self._add_from_library)
        from_disk = QPushButton("Browse for files…")
        from_disk.clicked.connect(self._add_from_disk)
        drop = QPushButton("Remove")
        drop.clicked.connect(self._drop_source)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_sources)
        picks = QHBoxLayout()
        picks.addWidget(from_library)
        picks.addWidget(from_disk)
        picks.addStretch()
        picks.addWidget(drop)
        picks.addWidget(clear)

        self.model_box = QComboBox()
        for name, why in MODELS:
            self.model_box.addItem(f"{name} — {why}", name)
        self.model_box.currentIndexChanged.connect(lambda _i: self._reinspect())
        self.proj_box = QComboBox()
        self.proj_box.addItem("Work it out from each file", "")
        self.proj_box.addItem("Equirectangular (flat 180 or 360)", "equirectangular")
        self.proj_box.addItem("Fisheye already", "fisheye")
        self.proj_box.currentIndexChanged.connect(lambda _i: self._reinspect())
        self.fov_box = QSpinBox()
        self.fov_box.setRange(140, 220)
        self.fov_box.setSingleStep(5)
        self.fov_box.setValue(180)
        self.fov_box.setSuffix("°")
        self.fov_box.valueChanged.connect(self._fov_changed)
        self.quality_box = QSpinBox()
        self.quality_box.setRange(10, 32)
        self.quality_box.setValue(18)
        self.quality_box.valueChanged.connect(lambda _v: self._reinspect())
        self.stride_box = QSpinBox()
        self.stride_box.setRange(1, 4)
        self.stride_box.setValue(1)
        self.stride_box.valueChanged.connect(lambda _v: self._reinspect())
        self.detail_box = QComboBox()
        for name in ("coarse", "normal", "fine", "finest"):
            self.detail_box.addItem(f"{name} — {DETAIL_WHY[name]}", name)
        self.detail_box.setCurrentIndex(2)
        self.detail_box.currentIndexChanged.connect(lambda _i: self._reinspect())
        self.chunk_box = QSpinBox()
        self.chunk_box.setRange(0, 200000)
        self.chunk_box.setSingleStep(1000)
        self.chunk_box.setValue(5000)
        self.chunk_box.setSpecialValueText("all in one go")
        self.chunk_box.setSuffix(" frames")
        self.chunk_box.setToolTip(
            "Matte this many frames at a time and join the results. A run that fails or is "
            "stopped loses only the chunk it was on - the finished ones are kept and picked "
            "up next time. Zero does the whole thing in one go.")
        self.chunk_box.valueChanged.connect(lambda _v: self._refresh())

        # What to end up with. The cut-out on its own is ready the moment matting is joined -
        # no packing, no copy of the film - but only BlueShelf's player can use it. A DeoVR
        # copy plays anywhere, and costs hours more and a second copy of the film.
        self.make_box = QComboBox()
        self.make_box.addItem("A full DeoVR copy — plays in any headset app", "deovr")
        self.make_box.addItem("Just the cut-out — plays in BlueShelf, done much sooner", "matte")
        self.make_box.currentIndexChanged.connect(lambda _i: self._make_changed())
        self.keep_box = QCheckBox("Also keep the cut-out, to play in BlueShelf")
        self.keep_box.setChecked(True)
        self.keep_box.toggled.connect(lambda _on: self._reinspect())
        self.keep_box.setToolTip(
            "The cut-out is made anyway on the way to the DeoVR copy. Keeping it costs about "
            "a gigabyte, and means the film can also be watched in BlueShelf with the "
            "original picture - and a better cut-out swapped in later without packing again.")
        make_row = QHBoxLayout()
        make_row.addWidget(self.make_box, 1)
        make_row.addWidget(self.keep_box)

        settings = QFormLayout()
        settings.addRow("Make", make_row)
        settings.addRow("Model", self.model_box)
        settings.addRow("The videos are", self.proj_box)
        settings.addRow("Lens angle", self.fov_box)
        settings.addRow("Detail", self.detail_box)
        settings.addRow("Quality (lower is better)", self.quality_box)
        settings.addRow("Matte every Nth frame", self.stride_box)
        self.roi_box = QCheckBox("Follow the performer around the frame")
        self.roi_box.setChecked(True)
        self.roi_box.setToolTip(
            "Mattes only a window around the performer. The model's memory is wiped every "
            "time that window moves, and it takes a few frames to recover, and each eye is "
            "followed separately. Measured on a real scene, turning this off was about "
            "twice as fast, used less graphics memory, and the mask was no worse.")
        settings.addRow("Follow the subject", self.roi_box)
        settings.addRow("Work in chunks of", self.chunk_box)

        self.spread = QCheckBox("Spread the work across several computers")
        self.spread.toggled.connect(self._refresh)
        self.parts_box = QSpinBox()
        self.parts_box.setRange(2, 8)
        self.parts_box.setValue(2)
        self.parts_box.valueChanged.connect(self._refresh)
        self.share_line = QLineEdit()
        self.share_line.setPlaceholderText(r"\\192.168.1.6\Transmission\.blueshelf-farm")
        share_browse = QPushButton("Browse…")
        share_browse.clicked.connect(lambda: self._browse_share(self.share_line))
        share_row = QHBoxLayout()
        share_row.addWidget(self.share_line, 1)
        share_row.addWidget(share_browse)
        self.parts_row = QWidget()
        parts_line = QHBoxLayout(self.parts_row)
        parts_line.setContentsMargins(0, 0, 0, 0)
        parts_line.addWidget(QLabel("Shares per video"))
        parts_line.addWidget(self.parts_box)
        parts_line.addStretch()

        self.farm_view = QListWidget()
        self.farm_view.setMaximumHeight(110)
        self.farm_view.setToolTip(
            "Every machine working out of the shared folder, and how long since each last "
            "said so. A machine that stops talking for twenty minutes has its share handed "
            "back to the queue.")

        spread_form = QVBoxLayout()
        spread_form.setContentsMargins(0, 0, 0, 0)
        spread_form.addWidget(self.parts_row)
        folder_line = QHBoxLayout()
        folder_line.addWidget(QLabel("Folder they all share"))
        folder_line.addLayout(share_row, 1)
        spread_form.addLayout(folder_line)
        self.bench_btn = QPushButton("Check this machine")
        self.bench_btn.setToolTip(
            "Time this machine's link to the shared folder and how fast it mattes, and tell "
            "the other machines. Matting hardly touches the network; projecting and packing "
            "move tens of gigabytes, so the two answer different questions.")
        self.bench_btn.clicked.connect(self._check_machine)
        self.retry_btn = QPushButton(RETRY_LABEL)
        self.retry_btn.setToolTip(
            "Put every chunk that failed back in the queue, for any machine to pick up. "
            "Shares usually fail for a reason shared by all of them, so once it is dealt "
            "with the rest of the job is still good.")
        self.retry_btn.clicked.connect(self._retry_failed)
        self.retry_btn.setEnabled(False)
        spread_form.addWidget(QLabel("Machines working out of that folder:"))
        spread_form.addWidget(self.farm_view)
        buttons_under = QHBoxLayout()
        buttons_under.addWidget(self.bench_btn)
        buttons_under.addWidget(self.retry_btn)
        spread_form.addLayout(buttons_under)
        self.spread_box = QGroupBox()
        self.spread_box.setFlat(True)
        self.spread_box.setLayout(spread_form)

        self.only_action = QCheckBox("Only matte the parts with action, and skip the talking")
        self.only_action.setToolTip(
            "Most scenes open with several minutes of clothed talking. Matting that is wasted "
            "time - it becomes an ordinary VR scene instead, and your room appears when the "
            "action starts.")
        self.only_action.toggled.connect(self._action_mode_changed)

        self.scan_btn = QPushButton("Scan this video")
        self.scan_btn.clicked.connect(self._scan)
        self.judge = QComboBox()
        self.judge.addItem("by what is exposed", "exposure")
        self.judge.addItem("by how much skin is on screen", "skin")
        self.judge.setToolTip(
            "Exposure asks a detector which parts of a body are on screen and whether they "
            "are covered. Skin just counts skin-coloured pixels, which is a poor guide in VR "
            "where the performer is close to the lens the whole time.")
        self.judge.currentIndexChanged.connect(lambda _i: self._judge_changed())
        self.threshold = QSlider(Qt.Horizontal)
        self.threshold.setRange(2, 90)          # 0.02 to 0.90, whichever measure is in use
        self.threshold.setValue(10)
        self.threshold.setToolTip("How much skin counts as action. Move it left to matte more.")
        self.threshold.valueChanged.connect(self._retime)
        self.pad_box = QSpinBox()
        self.pad_box.setRange(0, 60)
        self.pad_box.setValue(5)
        self.pad_box.setSuffix(" s")
        self.pad_box.setToolTip("Added to both ends of every stretch, so nothing is clipped.")
        self.pad_box.valueChanged.connect(lambda _v: self._retime())
        self.shortest_box = QSpinBox()
        self.shortest_box.setRange(0, 300)
        self.shortest_box.setValue(20)
        self.shortest_box.setSuffix(" s")
        self.shortest_box.setToolTip("Stretches shorter than this are noise, and dropped.")
        self.shortest_box.valueChanged.connect(lambda _v: self._retime())

        scan_row = QHBoxLayout()
        scan_row.addWidget(self.scan_btn)
        scan_row.addWidget(QLabel("Judge"))
        scan_row.addWidget(self.judge)
        scan_row.addWidget(QLabel("Sensitivity"))
        scan_row.addWidget(self.threshold, 1)
        scan_row.addWidget(QLabel("Pad"))
        scan_row.addWidget(self.pad_box)
        scan_row.addWidget(QLabel("Ignore under"))
        scan_row.addWidget(self.shortest_box)

        self.line_up = Timeline()
        self.line_up.edited.connect(self._stretches_edited)
        mark_action = QPushButton("Mark dragged as action")
        mark_action.clicked.connect(lambda: self.line_up.mark(True))
        mark_talk = QPushButton("Mark dragged as talk")
        mark_talk.clicked.connect(lambda: self.line_up.mark(False))
        reset_marks = QPushButton("Back to the scan")
        reset_marks.clicked.connect(self._retime)
        forget = QPushButton("Forget this scan")
        forget.setToolTip("Throw away what is remembered for this video and start again.")
        forget.clicked.connect(self._forget)
        mark_row = QHBoxLayout()
        mark_row.addWidget(mark_action)
        mark_row.addWidget(mark_talk)
        mark_row.addWidget(reset_marks)
        mark_row.addWidget(forget)
        mark_row.addStretch()

        self.scan_note = QLabel()
        self.scan_note.setWordWrap(True)
        scan_form = QVBoxLayout()
        scan_form.setContentsMargins(0, 0, 0, 0)
        scan_form.addLayout(scan_row)
        scan_form.addWidget(self.line_up)
        scan_form.addLayout(mark_row)
        scan_form.addWidget(self.scan_note)
        self.scan_box = QGroupBox()
        self.scan_box.setFlat(True)
        self.scan_box.setLayout(scan_form)
        self.scan_box.hide()

        self.estimate = QLabel()
        self.estimate.setWordWrap(True)
        self.estimate.setTextFormat(Qt.RichText)
        self.overall_bar = QProgressBar()
        self.overall_bar.setFormat("%v of %m done")
        self.overall_bar.hide()
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.stage = QLabel()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(90)
        self.log.setMaximumBlockCount(1000)

        self.start_btn = QPushButton("Make the copies")
        self.start_btn.clicked.connect(self._start)
        self.sofar_btn = QPushButton("Make one from what's done")
        self.sofar_btn.setToolTip(
            "Build a playable copy now, out of the chunks already matted. The parts not done "
            "yet play as the ordinary video. It changes nothing: the chunks stay where they "
            "are and the real job can carry on afterwards.")
        self.sofar_btn.clicked.connect(self._make_so_far)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.sofar_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addStretch()
        buttons.addWidget(close)

        # Nothing here changes because the window was scrolled past it.
        self._scroll_guard = no_stray_scrolling(
            self.model_box, self.proj_box, self.fov_box, self.quality_box, self.stride_box,
            self.chunk_box, self.detail_box, self.parts_box, self.threshold, self.pad_box,
            self.shortest_box, self.judge)

        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Videos to make passthrough copies of:"))
        layout.addWidget(self.sources, 1)
        layout.addLayout(picks)
        layout.addLayout(settings)
        layout.addWidget(self.only_action)
        layout.addWidget(self.scan_box)
        layout.addWidget(self.spread)
        layout.addWidget(self.spread_box)
        layout.addWidget(self.estimate)
        layout.addWidget(self.overall_bar)
        layout.addWidget(self.stage)
        layout.addWidget(self.bar)
        layout.addWidget(self.log, 1)
        layout.addLayout(buttons)
        return page

    # ------------------------------------------------------------ the list of videos

    def _source_changed(self, _row: int) -> None:
        self._recall(self._current_source())
        self._show_reading()
        self._refresh()

    def _stamp(self, source: str) -> tuple[int, float]:
        """Size and time of a file, used to tell whether a remembered scan still applies."""
        try:
            found = Path(source).stat()
        except OSError:
            return (0, 0.0)
        return (found.st_size, found.st_mtime)

    def _recall(self, source: str) -> bool:
        """Fetch a scan kept from an earlier visit, if the file is still the same one."""
        if not source or source in self.readings:
            return source in self.readings
        kept = self.db.get_scan(source, *self._stamp(source))
        if not kept:
            return False
        self.readings[source] = kept["reading"]
        if kept["stretches"]:
            self.stretches[source] = [Stretch(a, b) for a, b in kept["stretches"]]
        self.recalled.add(source)
        if not kept["reading"].get("exposure"):
            self.judge.blockSignals(True)
            self.judge.setCurrentIndex(1)
            self.judge.blockSignals(False)
        return True

    def _remember(self, source: str) -> None:
        """Write the scan and the current stretches back, so neither is lost on closing."""
        reading = self.readings.get(source)
        if not reading:
            return
        try:
            self.db.save_scan(source, *self._stamp(source), reading,
                              [(s.start, s.end) for s in self.stretches.get(source, [])])
        except Exception:  # noqa: BLE001 - failing to remember must not break the window
            pass

    def _current_source(self) -> str:
        item = self.sources.currentItem()
        if item is None and self.sources.count():
            item = self.sources.item(0)
        return item.data(Qt.UserRole) if item else ""

    def _paths(self) -> list[str]:
        return [self.sources.item(row).data(Qt.UserRole) for row in range(self.sources.count())]

    def _append_source(self, path: str) -> bool:
        if path.lower() in {p.lower() for p in self._paths()}:
            return False
        item = QListWidgetItem(Path(path).name)
        item.setData(Qt.UserRole, path)
        item.setToolTip(path)
        self.sources.addItem(item)
        self._recall(path)
        if self.sources.currentRow() < 0:
            self.sources.setCurrentRow(0)
        return True

    # ------------------------------------------------------------ dragged in

    def _dropped_paths(self, carried) -> list[str]:
        """The video files in something being dragged, from the library or from a folder."""
        if carried.hasFormat(VIDEO_MIME):
            raw = bytes(carried.data(VIDEO_MIME)).decode("utf-8", "replace")
            return [line for line in raw.splitlines() if line.strip()]
        found = []
        for url in carried.urls() if carried.hasUrls() else []:
            local = url.toLocalFile()
            if local and Path(local).suffix.lower() in VIDEO_SUFFIXES:
                found.append(local.replace("/", "\\"))
        return found

    def dragEnterEvent(self, event) -> None:
        if self._dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if self._dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths_in = self._dropped_paths(event.mimeData())
        if not paths_in:
            return
        added = sum(1 for path in paths_in if self._append_source(path))
        event.acceptProposedAction()
        self.status.setText(f"Added {added} dragged in."
                            if added else "Those are already on the list.")
        self._refresh()

    def add_videos(self, videos: list[dict]) -> None:
        """Put more videos on the list, from the library window."""
        added = sum(1 for video in videos if self._append_source(video["path"]))
        if added:
            self.status.setText(f"Added {added}.")
        self._refresh()

    def _add_from_library(self) -> None:
        picker = PickDialog(self.db, set(self._paths()), self,
                            title="Choose videos to make passthrough copies of")
        if picker.exec() != QDialog.Accepted:
            return
        added = sum(1 for path in picker.chosen() if self._append_source(path))
        self.status.setText(f"Added {added} from the library.")
        self._refresh()

    def _add_from_disk(self) -> None:
        start = self.db.libraries()[0]["path"] if self.db.libraries() else ""
        chosen, _filter = QFileDialog.getOpenFileNames(self, "Pick the videos", start,
                                                       VIDEO_FILTER)
        for path in chosen:
            self._append_source(path.replace("/", "\\"))
        self._refresh()

    def _drop_source(self) -> None:
        for item in self.sources.selectedItems():
            self.sources.takeItem(self.sources.row(item))
        self._refresh()

    def _clear_sources(self) -> None:
        self.sources.clear()
        self._refresh()

    def _remove_path(self, path: str) -> None:
        for row in range(self.sources.count()):
            if self.sources.item(row).data(Qt.UserRole) == path:
                self.sources.takeItem(row)
                return

    def _browse_share(self, line: QLineEdit) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "The folder every computer can reach",
                                                  line.text())
        if chosen:
            line.setText(chosen.replace("/", "\\"))
            self._refresh()

    # ------------------------------------------------------------ tab two: helping out

    def _help_tab(self) -> QWidget:
        page = QWidget()
        note = QLabel(
            "Leave this running on a second computer and it will take shares of whatever is "
            "waiting in the folder below. Nothing needs to be set up on this end beyond "
            "BlueShelf and VRAutoMatte - the work is claimed by renaming a file, so two "
            "computers can never take the same share.<br><br>"
            "<b>Both computers have to reach the videos by the same path.</b> A drive letter "
            "mapped differently on each machine is the usual thing that goes wrong; the "
            "network path (\\\\server\\share\\…) always works.<br><br>"
            "Closing this is safe at any point. A share that was half done goes back in the "
            "queue on its own.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)

        self.help_share_line = QLineEdit()
        self.help_share_line.setPlaceholderText(r"\\192.168.1.6\Transmission\.blueshelf-farm")
        help_browse = QPushButton("Browse…")
        help_browse.clicked.connect(lambda: self._browse_share(self.help_share_line))
        row = QHBoxLayout()
        row.addWidget(self.help_share_line, 1)
        row.addWidget(help_browse)

        self.queue = QListWidget()
        self.queue.setMinimumHeight(70)
        self.help_btn = QPushButton("Start helping")
        self.help_btn.clicked.connect(self._toggle_help)
        self.help_bench_btn = QPushButton("Check this machine")
        self.help_bench_btn.setToolTip(
            "Time this machine's link to the shared folder and how fast it mattes, and tell "
            "the other machines. Add the video on the first tab first and it will matte a "
            "few seconds of it - which also proves this machine can reach the video by the "
            "same path the job uses, the commonest thing to go wrong.")
        self.help_bench_btn.clicked.connect(self._check_machine)
        self.help_log = QPlainTextEdit()
        self.help_log.setReadOnly(True)
        self.help_log.setMinimumHeight(90)
        self.help_log.setMaximumBlockCount(1000)
        self.help_bar = QProgressBar()
        self.help_bar.setRange(0, 1000)
        self.help_bar.setTextVisible(False)
        self.help_stage = QLabel()

        layout = QVBoxLayout(page)
        layout.addWidget(note)
        layout.addWidget(QLabel("Folder they all share:"))
        layout.addLayout(row)
        layout.addWidget(QLabel("Waiting in that folder:"))
        layout.addWidget(self.queue, 1)
        self.drop_btn = QPushButton("Take the chosen one out")
        self.drop_btn.setToolTip(
            "Remove a video from the shared queue. The chunks already matted are kept "
            "where they are, beside the video, so putting it back later carries on rather "
            "than starting again.")
        self.drop_btn.clicked.connect(self._drop_from_queue)
        self.drop_btn.setEnabled(False)
        self.queue.itemSelectionChanged.connect(self._queue_picked)
        # Here as well as on the first tab: when every machine is only helping, this is the
        # tab that is open, and a share that failed twice needs a person to say try again.
        self.help_retry_btn = QPushButton(RETRY_LABEL)
        self.help_retry_btn.setToolTip(
            "Put every chunk that failed back in the queue, for any machine to pick up. A chunk "
            "that fails once is tried again on its own after a few minutes; this is for one "
            "that has failed twice, once whatever stopped it has been dealt with.")
        self.help_retry_btn.clicked.connect(self._retry_failed_here)
        self.help_retry_btn.setEnabled(False)
        helping_row = QHBoxLayout()
        helping_row.addWidget(self.help_btn, 1)
        helping_row.addWidget(self.drop_btn)
        helping_row.addWidget(self.help_retry_btn)
        helping_row.addWidget(self.help_bench_btn)
        layout.addLayout(helping_row)
        layout.addWidget(self.help_stage)
        layout.addWidget(self.help_bar)
        layout.addWidget(self.help_log, 1)

        self.queue_timer = QTimer(self)
        self.queue_timer.setInterval(5000)
        self.queue_timer.timeout.connect(self._watch_farm)
        self.queue_timer.start()
        return page

    def _watch_farm(self) -> None:
        """Keep both views of the shared folder current, whichever tab is in front.

        Not only the tab you are looking at: the point of coordinating a job is to see what
        the other machine is doing, and that view used to stop updating the moment you left
        it, which made a working helper look like a dead one.
        """
        if not self.isVisible():
            return
        self._fill_queue(self.help_share_line.text().strip())
        if self.spread.isChecked():
            self._fill_workers(self.share_line.text().strip())

    def _read_share(self, root: str):
        # Asked every few seconds, and the NAS sometimes refuses for a moment ("security
        # policies block unauthenticated guest access"). That is a folder not there just now,
        # not something to throw out of a timer.
        try:
            if not root or not Path(root).is_dir():
                return None
        except OSError:
            return None
        return farm.Share(root)

    def _fill_queue(self, root: str) -> None:
        self.queue.clear()
        self.help_retry_btn.setEnabled(False)
        share = self._read_share(root)
        if share is None:
            return
        failed_any = False
        for job_id in share.jobs():
            try:
                state = share.state(job_id)
                job, _pieces = share.read_job(job_id)
            except (OSError, ValueError, TypeError):
                continue
            where = f" on {', '.join(state['machines'])}" if state["machines"] else ""
            row = QListWidgetItem(
                f"{Path(job.source).name} — {state['done']}/{state['total']} done, "
                f"{state['waiting']} waiting, {state['running']} being worked on{where}"
                + _failed_note(state))
            # The rows below are machines, not jobs; only a row carrying an id can be taken out.
            row.setData(Qt.UserRole, job_id)
            self.queue.addItem(row)
            if state["failed"]:
                failed_any = True
                self.queue.addItem(f"    why: {state['failures'][0]}")
        self.help_retry_btn.setEnabled(failed_any)
        for fact in share.machines():
            bits = [fact.get("machine", "a computer")]
            if fact.get("matte_fps"):
                bits.append(f"{fact['matte_fps']:g} fps matting")
            if fact.get("read_mbs"):
                bits.append(f"{fact['read_mbs']:g}/{fact.get('write_mbs', 0):g} MB/s")
            self.queue.addItem("    " + "  ".join(bits))

    def _check_machine(self) -> None:
        """Time this machine and tell the others."""
        helping = self.tabs.currentIndex() == 1
        first = (self.help_share_line if helping else self.share_line).text().strip()
        second = (self.share_line if helping else self.help_share_line).text().strip()
        share = self._read_share(first or second)
        if share is None:
            QMessageBox.information(self, "No shared folder",
                                    "Choose the folder the computers share first.")
            return
        if self.worker is not None and self.worker.isRunning():
            return
        source = self._current_source()
        job = self._job_for(source) if source else None
        if job is None:
            # On a helper there is no list of videos, but there is work in the shared
            # folder - and testing against the very file it is about to matte is a better
            # check than testing against nothing, since reaching that file by the path the
            # job records is the thing most likely to be wrong.
            job = self._job_from_the_queue(share)
        self.bench_btn.setEnabled(False)
        self.help_bench_btn.setEnabled(False)
        self.worker = _Bench(self.install, share, job, self)
        self.worker.line.connect(self.log.appendPlainText)
        self.worker.line.connect(self.help_log.appendPlainText)
        self.worker.step.connect(self._made_step)
        self.worker.ended.connect(self._checked)
        self.worker.start()

    def _job_from_the_queue(self, share) -> Job | None:
        """A job waiting in the shared folder, to time this machine against."""
        for job_id in share.jobs():
            try:
                job, _pieces = share.read_job(job_id)
            except (OSError, ValueError, TypeError):
                continue
            if Path(job.source).exists():
                return job
        return None

    def _checked(self, trouble: str) -> None:
        self.bench_btn.setEnabled(True)
        self.help_bench_btn.setEnabled(True)
        self.stage.setText("")
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.worker = None
        if trouble:
            self.log.appendPlainText(trouble)
        self._fill_workers(self.share_line.text().strip())
        self._refresh()

    def _retry_failed(self) -> None:
        """Offer every failed chunk in the shared folder again."""
        share = self._read_share(self.share_line.text().strip())
        if share is None:
            return
        again = sum(share.retry_failed(job_id) for job_id in share.jobs())
        self.status.setText(f"Put {again} failed chunk{'s' if again != 1 else ''} back in the "
                            f"queue." if again else "Nothing has failed.")
        self._fill_workers(self.share_line.text().strip())

    def _retry_failed_here(self) -> None:
        """The same, from the helping tab and its own folder."""
        share = self._read_share(self.help_share_line.text().strip())
        if share is None:
            return
        again = sum(share.retry_failed(job_id) for job_id in share.jobs())
        self.help_log.appendPlainText(
            f"Put {again} failed chunk{'s' if again != 1 else ''} back in the queue."
            if again else "Nothing has failed.")
        self._fill_queue(self.help_share_line.text().strip())

    def _fill_workers(self, root: str) -> None:
        self.farm_view.clear()
        self.retry_btn.setEnabled(False)
        share = self._read_share(root)
        if share is None:
            self.farm_view.addItem("Choose a folder the computers share.")
            return
        known = share.machines()
        for fact in known:
            bits = [fact.get("machine", "a computer")]
            if fact.get("gpu"):
                bits.append(fact["gpu"])
            if fact.get("matte_fps"):
                bits.append(f"{fact['matte_fps']:g} fps matting")
            if fact.get("read_mbs"):
                bits.append(f"{fact['read_mbs']:g}/{fact.get('write_mbs', 0):g} MB/s "
                            f"in/out")
            self.farm_view.addItem("  ".join(bits))
        best = share.best_for_the_tail()
        if best and len(known) > 1:
            self.farm_view.addItem(
                f"→ {best['machine']} has the best link, so the joining at the end is left "
                f"to it - start wherever you like, it makes no difference.")

        jobs = share.jobs()
        if not jobs:
            if not known:
                self.farm_view.addItem("Nothing in that folder yet.")
            return
        busy = 0
        for job_id in jobs:
            try:
                name = Path(share.read_job(job_id)[0].source).name
                state = share.state(job_id)
            except (OSError, ValueError, TypeError):
                continue
            for worker in share.workers(job_id):
                busy += 1
                seen = _ago(worker["age"])
                trouble = "  — not responding, its share will go back" if worker["stale"] else ""
                self.farm_view.addItem(
                    f"{worker['machine']}: chunk {worker['index'] + 1} of {name}"
                    f"  (heard from {seen}){trouble}")
            if state["failed"]:
                self.retry_btn.setEnabled(True)
                self.farm_view.addItem(
                    f"{name}{_failed_note(state)}. Why: "
                    f"{state['failures'][0] if state['failures'] else 'no reason recorded'}")
            # Only when every one is done. With a failed chunk and nothing else left this
            # said "waiting to be joined" too, which it was not - nothing joins until it is
            # tried again.
            if state["complete"]:
                self.farm_view.addItem(f"{name}: every chunk done, waiting to be joined")
        if not busy:
            self.farm_view.addItem("No machine is working on anything just now.")

    def _queue_picked(self) -> None:
        row = self.queue.currentItem()
        self.drop_btn.setEnabled(bool(row and row.data(Qt.UserRole)))

    def _drop_from_queue(self) -> None:
        """Take a video out of the shared queue, leaving its finished chunks alone."""
        row = self.queue.currentItem()
        job_id = row.data(Qt.UserRole) if row else ""
        if not job_id:
            return
        share = self._read_share(self.help_share_line.text().strip())
        if share is None:
            return
        try:
            state = share.state(job_id)
            job, _pieces = share.read_job(job_id)
        except (OSError, ValueError, TypeError):
            return
        name = Path(job.source).name
        busy = (f"\n\n{state['running']} chunk(s) are being worked on right now. Those "
                f"machines will finish what is in hand and then find the job gone."
                if state["running"] else "")
        answer = QMessageBox.question(
            self, "Take it out of the queue?",
            f"{name}\n\n{state['done']} of {state['total']} chunks are done. They are kept "
            f"beside the video, so adding it again later carries on from here rather than "
            f"starting over.{busy}\n\nTake it out?",
            QMessageBox.Cancel | QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        share.forget(job_id)
        self.help_log.appendPlainText(f"Took {name} out of the queue. Its "
                                      f"{state['done']} finished chunks are kept.")
        self._fill_queue(self.help_share_line.text().strip())

    def _toggle_help(self) -> None:
        if isinstance(self.worker, _Helping) and self.worker.isRunning():
            # It used to drop the chunk in hand while the button claimed it was stopping
            # after it - so a share minutes from done was thrown away without being asked.
            how = self.ask_how_to_stop(self.help_stage.text())
            if not how:
                return
            self.worker.stop(after_this=how == "after")
            self.help_btn.setEnabled(False)
            self.help_btn.setText("Stopping after this share…" if how == "after"
                                  else "Stopping…")
            return
        if self.install is None:
            QMessageBox.information(self, "Nothing to run it with",
                                    "Point this at VRAutoMatte first.")
            return
        root = self.help_share_line.text().strip()
        share = farm.Share(root)
        trouble = share.usable() if root else "Choose the folder the computers share."
        if trouble:
            QMessageBox.warning(self, "That folder will not work", trouble)
            return
        self.db.set_setting("farm_share_path", root)
        self.worker = _Helping(self.install, share, self)
        self.worker.line.connect(self.help_log.appendPlainText)
        self.worker.step.connect(self._help_step)
        self.worker.ended.connect(self._help_ended)
        self.worker.start()
        self.help_btn.setText("Stop helping")
        self._refresh()

    def _help_step(self, stage: str, share: float, detail: str) -> None:
        self.help_stage.setText(f"{stage} — {detail}" if detail else stage)
        self.help_bar.setValue(int(share * 1000))

    def _help_ended(self, trouble: str) -> None:
        self.help_btn.setEnabled(True)
        self.help_btn.setText("Start helping")
        self.help_stage.setText("")
        self.help_bar.setValue(0)
        self.worker = None
        if trouble:
            self.help_log.appendPlainText(trouble)
        self._refresh()

    # ------------------------------------------------------------ what would happen

    def _load_settings(self) -> None:
        saved = self.db.get_setting("farm_share_path", "")
        self.share_line.setText(saved)
        self.help_share_line.setText(saved)
        names = [name for name, _why in MODELS]
        remembered = self.db.get_setting("matte_model", names[0])
        if remembered in names:
            self.model_box.setCurrentIndex(names.index(remembered))
        self.roi_box.setChecked(self.db.get_setting("matte_roi", "1") != "0")
        made = self.make_box.findData(self.db.get_setting("matte_make", "deovr"))
        if made >= 0:
            self.make_box.setCurrentIndex(made)
        self.keep_box.setChecked(self.db.get_setting("matte_keep", "1") != "0")
        self.keep_box.setEnabled(self.make_box.currentData() == "deovr")
        detail = self.db.get_setting("matte_detail", "fine")
        found = self.detail_box.findData(detail)
        if found >= 0:
            self.detail_box.setCurrentIndex(found)

    # ------------------------------------------------------------ finding the action

    def _action_mode_changed(self, on: bool) -> None:
        self.scan_box.setVisible(on)
        self._show_reading()
        self._refresh()

    def _forget(self) -> None:
        source = self._current_source()
        if not source:
            return
        self.readings.pop(source, None)
        self.stretches.pop(source, None)
        self.recalled.discard(source)
        self.db.forget_scan(source)
        self._show_reading()
        self._refresh()

    def _scan(self) -> None:
        """Read the chosen video through and find where the action is."""
        source = self._current_source()
        if not source or self.install is None:
            return
        if self.worker is not None and self.worker.isRunning():
            return
        self.scan_btn.setEnabled(False)
        self.scan_note.setText(
            "Reading the video. Only a frame here and there is decoded, but the whole file "
            "still has to be read, so a long one over the network takes a few minutes. Stop "
            "cancels it.")
        self.worker = _Look(self.install, source, self)
        self.worker.reading.connect(self._scanned)
        self.worker.step.connect(self._made_step)
        self.worker.ended.connect(self._scan_ended)
        self.worker.start()
        self._refresh()

    def _series(self, reading: dict) -> list[float]:
        """The numbers the stretches are worked out from, by whichever measure is chosen."""
        if self.judge.currentData() == "exposure" and reading.get("exposure"):
            return reading["exposure"]
        return reading.get("values") or []

    def _bounds(self) -> tuple[float, float]:
        """Sensible limits for the line, which differ by measure.

        Exposure is a detector's confidence, so a useful line sits somewhere in the middle.
        Skin is a fraction of the frame and is always a small number."""
        return (0.15, 0.70) if self.judge.currentData() == "exposure" else (0.04, 0.45)

    def _judge_changed(self) -> None:
        source = self._current_source()
        if source in self.readings:
            self._suggest(self.readings[source])   # the same scan, read another way
            self._retime()
        else:
            self._refresh()

    def _suggest(self, reading: dict) -> None:
        values = self._series(reading)
        if not values:
            return
        lowest, highest = self._bounds()
        chosen = passthrough.suggest_threshold(values, lowest, highest)
        self.threshold.blockSignals(True)
        self.threshold.setValue(int(round(chosen * 100)))
        self.threshold.blockSignals(False)

    def _scanned(self, source: str, reading: dict) -> None:
        self.readings[source] = reading
        self.stretches.pop(source, None)
        if not reading.get("exposure"):
            # No detector on this machine: the choice would be a lie.
            self.judge.blockSignals(True)
            self.judge.setCurrentIndex(1)
            self.judge.blockSignals(False)
        # Start from a line drawn out of this video's own spread rather than a fixed one.
        self.recalled.discard(source)
        self._suggest(reading)
        self._retime()
        self._remember(source)

    def _scan_ended(self, trouble: str) -> None:
        self.scan_btn.setEnabled(True)
        self.stage.setText("")
        self.bar.setValue(0)
        self.worker = None
        if trouble:
            self.scan_note.setText(trouble)
        self._refresh()

    def _retime(self) -> None:
        """Work the stretches out again from the reading, with the settings as they stand."""
        self.line_up.threshold = self.threshold.value() / 100.0
        source = self._current_source()
        reading = self.readings.get(source)
        if not reading:
            self._show_reading()
            return
        job = self._job_for(source)
        frames = reading.get("frames") or (job.frames if job else 0)
        self.stretches[source] = passthrough.stretches_from(
            reading.get("times") or [], self._series(reading),
            float(reading.get("fps") or (job.fps if job else 0.0)), int(frames),
            threshold=self.threshold.value() / 100.0,
            pad=float(self.pad_box.value()), shortest=float(self.shortest_box.value()))
        self._show_reading()
        self._refresh()
        self._remember(source)

    def _stretches_edited(self) -> None:
        source = self._current_source()
        if source:
            self.stretches[source] = list(self.line_up.stretches)
            self._remember(source)
        self.scan_note.setText(self.line_up.summary())
        self._refresh()

    def _show_reading(self) -> None:
        source = self._current_source()
        reading = self.readings.get(source)
        self.line_up.threshold = self.threshold.value() / 100.0
        if reading:
            drawn = dict(reading)
            drawn["values"] = self._series(reading)
            self.line_up.show_reading(drawn, self.stretches.get(source, []))
            if not reading.get("detector"):
                missing = ("  <b>The exposure detector is not installed</b>, so this is the "
                           "skin measure, which is a poor guide in VR. To fit it, run this "
                           "in the VRAutoMatte folder:<br><tt>uv pip install --no-deps "
                           "nudenet opencv-python-headless</tt>")
            else:
                missing = ""
            how = ("what is exposed" if self.judge.currentData() == "exposure"
                   and reading.get("exposure") else "how much skin is on screen")
            self.scan_note.setTextFormat(Qt.RichText)
            again = ("  <b>Kept from an earlier scan</b>, including anything you marked by "
                     "hand." if source in self.recalled else "")
            self.scan_note.setText(
                self.line_up.summary()
                + f"  Judged by {how} - check it against the scene and drag over anything it "
                  f"got wrong.{again}{missing}")
        else:
            self.line_up.show_reading({}, [])
            if self.only_action.isChecked():
                self.scan_note.setText(
                    "Scan each video you want trimmed. Anything left unscanned is matted whole.")

    def _plan_for(self, job: Job) -> list:
        """The run of pieces for one video: what gets matted, and what is left blank."""
        parts = self.parts_box.value() if self.spread.isChecked() else 1
        lead_in = max(30, int(round(job.fps * 2))) if job.fps else 60
        wanted = self.stretches.get(job.source) if self.only_action.isChecked() else None
        return passthrough.plan_slices(job.frames, wanted or None, parts, lead_in,
                                       self.chunk_box.value())

    def _fov_changed(self, _value: int) -> None:
        # Once it has been set by hand, stop following what each file name says.
        self.fov_touched = True
        self._reinspect()

    def _reinspect(self) -> None:
        self.jobs.clear()
        self._refresh()

    def _facts_for(self, source: str) -> dict | None:
        """How big the file is. Kept, because measuring it means running ffprobe."""
        if source in self.facts:
            return self.facts[source]
        if source in self.unreadable:
            return None
        video_id = self.db.video_id_for_path(source)
        if video_id:
            rows = self.db.list_videos(video_id=video_id, show_hidden=True)
            known = passthrough.facts_from_library(rows[0]) if rows else None
            if known:
                self.facts[source] = known
                return known
        try:
            self.facts[source] = passthrough.measure(source)
        except Unavailable:
            self.unreadable.add(source)
            return None
        return self.facts[source]

    def _job_for(self, source: str) -> Job | None:
        """What would be made from one file, given the settings as they stand."""
        if source in self.jobs:
            return self.jobs[source]
        facts = self._facts_for(source)
        if facts is None:
            return None
        job = Job(source=source, fov=self.fov_box.value(), model=self.model_box.currentData(),
                  crf=self.quality_box.value(), stride=self.stride_box.value(),
                  detail=self.detail_box.currentData(),
                  roi=self.roi_box.isChecked(),
                  pack_chunk=self.chunk_box.value())
        forced = self.proj_box.currentData()
        if forced:
            job.projection = forced
            job.extras["projection_locked"] = True
        if self.fov_touched:
            job.extras["fov_locked"] = True
        passthrough.inspect(job, facts)
        job.extras.clear()
        job.make = self.make_box.currentData()
        job.keep_mask = job.make == "deovr" and self.keep_box.isChecked()
        passthrough.settle_mask_size(job)
        self.jobs[source] = job
        return job

    def _make_changed(self) -> None:
        # Keeping the cut-out only means something when the copy is the thing being made.
        self.keep_box.setEnabled(self.make_box.currentData() == "deovr")
        self._reinspect()

    def _all_jobs(self) -> list[Job]:
        made = []
        for path in self._paths():
            job = self._job_for(path)
            if job is not None:
                job.parts = self.parts_box.value() if self.spread.isChecked() else 1
                made.append(job)
        return made

    def share_has_work(self, job: Job, pieces: list) -> bool:
        """Whether anything has already been matted for this video."""
        folder = passthrough.work_folder(job)
        return any(passthrough.finished(passthrough.part_file(folder, p), p.kept)
                   for p in pieces if not p.blank)

    def _matted_share(self, job: Job) -> float:
        """How much of one video will be matted, between 0 and 1."""
        if not self.only_action.isChecked() or not job.frames:
            return 1.0
        wanted = self.stretches.get(job.source)
        if not wanted:
            return 1.0        # never scanned, so it gets matted whole
        return min(1.0, sum(s.length for s in wanted) / float(job.frames))

    def _mark_models(self) -> None:
        """Grey out any model this install cannot actually run, and say why.

        Offering it and failing three hours in with an import error would be the worst of
        both worlds, so it is offered only where the extra is present.
        """
        here = self.install is not None and passthrough.has_matanyone2(self.install)
        for at, (name, why) in enumerate(MODELS):
            ready = here or name not in NEEDS_EXTRA
            item = self.model_box.model().item(at)
            if item is not None:
                item.setEnabled(ready)
            self.model_box.setItemText(
                at, f"{name} — {why}" if ready
                else f"{name} — not installed. Run the setup for it first")
        if not here and self.model_box.currentData() in NEEDS_EXTRA:
            self.model_box.setCurrentIndex(0)

    def _refresh(self) -> None:
        self._mark_models()
        self.spread_box.setVisible(self.spread.isChecked())
        # With a chunk size set, chunking decides how the work is cut up and this spinner
        # changes nothing - so it is not offered rather than quietly ignored.
        self.parts_row.setVisible(self.spread.isChecked() and not self.chunk_box.value())
        self.scan_box.setVisible(self.only_action.isChecked())
        busy = self.worker is not None and self.worker.isRunning()
        # Setting up counts as busy for everything else: there is no install to use yet.
        setting = self.setter is not None and self.setter.isRunning()
        busy = busy or setting
        # Once there is a working install the button has nothing to offer, so it goes away
        # rather than sitting there inviting a five gigabyte download nobody needs.
        self.setup_btn.setVisible(self.install is None or setting)
        self.check_btn.setEnabled(self.install is not None and not setting)
        helping = isinstance(self.worker, _Helping)
        looking = isinstance(self.worker, _Look)
        count = self.sources.count()
        self.scan_btn.setText(
            "Scanning…" if looking
            else "Scan again" if self._current_source() in self.readings
            else "Scan this video")
        self.scan_btn.setEnabled(bool(self._current_source()) and self.install is not None
                                 and not busy)
        self.start_btn.setText("Make the copy" if count == 1
                               else f"Make {count} copies" if count else "Make the copies")
        self.start_btn.setEnabled(count > 0 and self.install is not None and not busy)
        found = None if busy else self._so_far_state()
        self.sofar_btn.setEnabled(found is not None and self.install is not None and not busy)
        if found is not None:
            _job, _pieces, ready, _frames = found
            self.sofar_btn.setText(f"Make one from the {ready} that are done")
        else:
            self.sofar_btn.setText("Make one from what's done")
        self.stop_btn.setEnabled(busy and not helping)
        if not count:
            self.estimate.setText("Add videos from your library, or browse for files.")
            return
        jobs = self._all_jobs()
        if not jobs:
            self.estimate.setText("<b>None of those files could be read.</b>")
            return
        minutes, skipped_minutes = 0.0, 0.0
        for job in jobs:
            share = self._matted_share(job)
            minutes += passthrough.estimate_minutes(job) * share
            if job.fps:
                skipped_minutes += job.frames * (1.0 - share) / job.fps / 60
        unreadable = count - len(jobs)
        current = self.sources.currentItem()
        detail = ""
        if current is not None:
            job = self._job_for(current.data(Qt.UserRole))
            if job is not None:
                length = job.frames / job.fps / 60 if job.fps else 0
                detail = (f"<br><b>{Path(job.source).name}</b>: {job.width}×{job.height}, "
                          f"{length:.0f} minutes, read as {job.projection} at {job.fov}°, "
                          f"saved as {Path(job.output).name}")
        where = (" shared between whatever computers are helping" if self.spread.isChecked()
                 else " on this computer")
        chunks = sum(1 for job in jobs for p in self._plan_for(job) if not p.blank)
        in_bits = (f" In {chunks} chunks, so a failure costs one of them rather than the lot."
                   if self.chunk_box.value() and chunks > 1 else "")
        done_already = 0
        for job in jobs:
            folder = passthrough.work_folder(job)
            if folder.is_dir():
                done_already += farm.Share(folder).ready_made(self._plan_for(job), folder)
        if done_already:
            in_bits += (f" <b>{done_already} of them are already matted</b> from an earlier "
                        f"run and will be kept.")
        one = "video" if len(jobs) == 1 else "videos"
        saved = ""
        if skipped_minutes >= 1:
            saved = (f" Skipping {_hours(skipped_minutes)} of talking, which is where the "
                     f"saving comes from.")
        self.estimate.setText(
            f"{len(jobs)} {one}, roughly <b>{_hours(minutes)}</b> altogether{where}.{saved}"
            + in_bits
            + (f" {unreadable} could not be read and will be skipped." if unreadable else "")
            + detail)

    # ------------------------------------------------------------ doing it

    def _start(self) -> None:
        jobs = self._all_jobs()
        if not jobs or self.install is None:
            return
        for job in jobs:
            # Chunks left by an older version, or by a run whose lens angle differed, come
            # back before anything is planned - otherwise they would be matted again.
            passthrough.gather_older_work(job, self._plan_for(job))
        # What each job would leave behind: the packed copy, or the cut-out on its own.
        existing = [job for job in jobs if passthrough.result_of(job).exists()]
        if existing:
            names = "\n".join(
                (Path(job.source).name + "  (cut-out)") if job.make == "matte"
                else Path(job.output).name for job in existing[:8])
            answer = QMessageBox.question(
                self, "Some copies are already there",
                f"{len(existing)} of these already have a copy:\n\n{names}\n\nMake them again?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.No:
                jobs = [job for job in jobs if not passthrough.result_of(job).exists()]
                if not jobs:
                    return
        # If chunks already exist for a video, the settings that produced them are on disk
        # beside them. Anything that would make those chunks useless is worth stopping for.
        for job in jobs:
            saved = passthrough.read_settings(job)
            if not saved:
                continue
            plan = self._plan_for(job)
            if not self.share_has_work(job, plan):
                continue
            differs = passthrough.settings_differences(job, plan, saved)
            if not differs:
                continue
            answer = QMessageBox.question(
                self, "These settings do not match the work already done",
                f"{Path(job.source).name} already has chunks matted with different "
                f"settings:\n\n  " + "\n  ".join(differs)
                + "\n\nCarrying on will matte all of them again. Change the "
                  "settings back to match, or carry on and redo the work?",
                QMessageBox.Cancel | QMessageBox.Yes)
            if answer != QMessageBox.Yes:
                return

        # Asked before hours are spent, because the damage does not show until the end.
        uneven = []
        for job in jobs:
            kind, why = passthrough.frame_timing(job.source)
            if kind == "vfr":
                uneven.append((job, why))
        if uneven:
            told = "\n".join(f"{Path(j.source).name} - {w}" for j, w in uneven[:6])
            answer = QMessageBox.question(
                self, "These do not have evenly spaced frames",
                f"{len(uneven)} of these have a variable frame rate:\n\n{told}\n\n"
                f"The mask is made at one steady rate and laid over the picture by time, so "
                f"on these it drifts further out of step the longer it plays - a ghost "
                f"trailing the performer, which only shows once the whole thing is "
                f"finished. Converting the video to a constant frame rate first avoids "
                f"it.\n\nCarry on anyway?",
                QMessageBox.Cancel | QMessageBox.Yes)
            if answer != QMessageBox.Yes:
                return

        self.db.set_setting("matte_model", jobs[0].model)
        self.db.set_setting("matte_detail", jobs[0].detail)
        self.db.set_setting("matte_roi", "1" if jobs[0].roi else "0")
        self.db.set_setting("matte_make", self.make_box.currentData())
        self.db.set_setting("matte_keep", "1" if self.keep_box.isChecked() else "0")
        wanted = {job.model for job in jobs} & NEEDS_EXTRA
        if wanted and not passthrough.has_matanyone2(self.install):
            QMessageBox.warning(
                self, "That model is not installed",
                f"{', '.join(sorted(wanted))} is an extra VRAutoMatte does not install by "
                f"default, and this copy does not have it. Nothing has been started - it "
                f"would have failed once the matting began.")
            return
        work = [(job, self._plan_for(job)) for job in jobs]
        unscanned = [job for job in jobs
                     if self.only_action.isChecked() and not self.stretches.get(job.source)]
        if unscanned:
            names = "\n".join(Path(job.source).name for job in unscanned[:8])
            if QMessageBox.question(
                    self, "Some were never scanned",
                    f"{len(unscanned)} of these have not been scanned for action, so they will "
                    f"be matted from end to end:\n\n{names}\n\nCarry on?",
                    QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
                return
        self.log.clear()
        self.overall_bar.setRange(0, len(jobs))
        self.overall_bar.setValue(0)
        self.overall_bar.setVisible(len(jobs) > 1)

        if self.spread.isChecked():
            root = self.share_line.text().strip()
            share = farm.Share(root)
            trouble = share.usable() if root else "Choose the folder the computers share."
            if trouble:
                QMessageBox.warning(self, "That folder will not work", trouble)
                return
            self.db.set_setting("farm_share_path", root)
            self.worker = _Shared(self.install, share, work, parent=self)
            self.worker.counts.connect(self._shared_counts)
        else:
            self.worker = _Batch(self.install, work, self)
        self.worker.line.connect(self.log.appendPlainText)
        self.worker.step.connect(self._made_step)
        self.worker.overall.connect(self._made_overall)
        self.worker.one_done.connect(self._one_done)
        self.worker.ended.connect(self._made_ended)
        self.worker.start()
        self._refresh()

    def _so_far_state(self) -> tuple[Job, list, int, int] | None:
        """The chosen video, its plan, and how many chunks of it are already matted."""
        source = self._current_source()
        job = self._job_for(source) if source else None
        if job is None:
            return None
        pieces = self._plan_for(job)
        passthrough.gather_older_work(job, pieces)
        _plan, ready, frames = passthrough.so_far(job, pieces)
        return (job, pieces, ready, frames) if ready else None

    def _make_so_far(self) -> None:
        found = self._so_far_state()
        if found is None or self.install is None:
            return
        job, pieces, ready, frames = found
        covered = frames / job.fps / 60 if job.fps else 0
        if QMessageBox.question(
                self, "Make a copy from what is done?",
                f"{ready} chunks are matted, covering about {covered:.0f} minutes."
                f"\n\nA copy will be made of those parts only - about "
                f"{covered:.0f} minutes long, all of it with the cut-out. The stretches "
                f"not matted yet are left out rather than filled in. Nothing is "
                f"re-matted and nothing is lost: the job can carry on afterwards."
                f"\n\nIt will be saved as "
                f"{Path(passthrough.partial_output(job)).name}",
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self.log.clear()
        self.worker = _SoFar(self.install, job, pieces, self)
        self.worker.line.connect(self.log.appendPlainText)
        self.worker.step.connect(self._made_step)
        self.worker.one_done.connect(self._one_done)
        self.worker.ended.connect(self._made_ended)
        self.worker.start()
        self._refresh()

    def _shared_counts(self, name: str, state: dict) -> None:
        self.status.setText(
            f"{name}: {state['done']} of {state['total']} shares done, {state['running']} being "
            f"worked on, {state['waiting']} waiting" + _failed_note(state))

    def _made_step(self, stage: str, share: float, detail: str) -> None:
        if self.stopping:
            # Otherwise the next progress message wipes the notice out and pressing Stop
            # looks like it did nothing.
            said = ("Stopping once this chunk is done" if self.stopping == "after"
                    else "Stopping")
            stage = f"{said} — {stage}"
        self.stage.setText(f"{stage} — {detail}" if detail else stage)
        self.bar.setValue(int(share * 1000))

    def _made_overall(self, done: int, total: int, name: str) -> None:
        self.overall_bar.setValue(done)
        if name:
            self.status.setText(f"Working on <b>{name}</b> — {done} of {total} done.")

    def _one_done(self, path: str) -> None:
        """Hand the new file to the library, and take its source off the list.

        Not tagged here: the scan that picks it up runs on its own thread, so the video does
        not exist to tag yet. The window behind marks it as Passthrough once the scan has
        actually found it.
        """
        self.made.emit(path)
        for source, job in list(self.jobs.items()):
            if job.output == path:
                self._remove_path(source)
                del self.jobs[source]
                break

    def _made_ended(self, trouble: str) -> None:
        self.stopping = ""
        self.stage.setText("")
        self.bar.setValue(0)
        if trouble:
            self.log.appendPlainText(trouble)
            QMessageBox.warning(self, "Not everything finished", trouble[:1500])
        elif not self.sources.count():
            self.status.setText("All done, and added to your library marked as Passthrough.")
        self.worker = None
        self.overall_bar.setVisible(False)
        self._refresh()

    def ask_how_to_stop(self, where: str = "") -> str:
        """"after" to finish the chunk in hand, "now" to drop it, "" to carry on.

        The same question wherever the work is being done. Which answer is right depends
        entirely on how far in it is - a chunk abandoned at frame 4,900 of 5,000 has thrown
        away nine minutes - so it is asked rather than decided.
        """
        question = QMessageBox(self)
        question.setWindowTitle("Stop")
        question.setText("Stop after the chunk being worked on, or drop it?")
        question.setInformativeText(
            (f"Where it has got to: {where}\n\n" if where else "")
            + "Finishing it keeps the work; dropping it throws away however far that "
              "one chunk has got. Everything already finished is kept either way.")
        after = question.addButton("Finish this chunk first", QMessageBox.AcceptRole)
        now = question.addButton("Drop it and stop now", QMessageBox.DestructiveRole)
        question.addButton("Carry on", QMessageBox.RejectRole)
        question.exec()
        if question.clickedButton() is after:
            return "after"
        if question.clickedButton() is now:
            return "now"
        return ""

    def _stop(self) -> None:
        """Ask how to stop, unless there is nothing in hand worth finishing."""
        if self.worker is None or not self.worker.isRunning():
            return
        how = self.ask_how_to_stop(self.stage.text())
        if not how:
            return
        self.stopping = how
        self.worker.stop(after_this=how == "after")
        self.stage.setText("Stopping once this chunk is done…" if how == "after"
                           else "Stopping…")
        self.stop_btn.setEnabled(False)

    def closeEvent(self, event) -> None:
        """Closing puts the window away; it does not throw anything out.

        The list of videos, the scans and any job under way all stay as they are for as long
        as BlueShelf is running. Reopening from the Tools menu brings the same window back.
        A job that is running keeps running, which is the point - there is no reason to sit
        and watch a four-hour matte.
        """
        if self.worker is not None and self.worker.isRunning():
            self.status.setText("Still working. This window can be closed; it carries on.")
        event.ignore()
        self.hide()
        self.shown.emit(False)

    def _scene_clip_ready(self, clip: str) -> None:
        """Scenes cut into a clip: onto the list, to be made like any other video."""
        self._append_source(clip)
        self.tabs.setCurrentIndex(0)
        self.status.setText(f"Added {Path(clip).name}, cut from the scenes you chose. Choose how to make "
                            f"it and press Start.")
        self._refresh()

    def shut_down(self) -> None:
        """Called when BlueShelf itself is closing: stop everything and mean it."""
        self.queue_timer.stop()
        self.scenes_tab.shut_down()
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()          # closing means now, not after this chunk
            self.worker.wait(30000)


def _scrolling(page: QWidget) -> QScrollArea:
    """Put a page in a scroller so the window can be made smaller than its contents."""
    holder = QScrollArea()
    holder.setWidget(page)
    holder.setWidgetResizable(True)
    holder.setFrameShape(QFrame.NoFrame)
    holder.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return holder


def _screen_room() -> int:
    """How tall the window may be without running off the screen it opens on."""
    screen = QApplication.primaryScreen()
    if screen is None:
        return 760
    return max(420, screen.availableGeometry().height() - 80)


RETRY_LABEL = "Try the failed chunks again"


def _failed_note(state: dict) -> str:
    """What the failed shares of a job mean for it, in a few words - or "" when none have.

    A job with a failed share and nothing else left used to read as idle - every chunk but
    one done, both machines waiting, nothing moving and nothing saying why.
    """
    failed = state.get("failed", 0)
    if not failed:
        return ""
    twice = failed - state.get("retrying", 0)
    if twice <= 0:
        return f", {failed} failed - will be tried again on its own in a few minutes"
    which = "twice" if twice == failed else f"- {twice} of them twice"
    return f", {failed} failed {which} - press “{RETRY_LABEL}” to try again"


def _ago(seconds: float) -> str:
    if seconds < 0:
        return "unknown"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    return f"{seconds / 3600:.1f} hours ago"


def _hours(minutes: float) -> str:
    if not minutes:
        return "unknown"
    if minutes < 90:
        return f"{minutes:.0f} minutes"
    return f"{minutes / 60:.1f} hours"
