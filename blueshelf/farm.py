"""Sharing passthrough work between machines, through a folder they can all reach.

Matting a full-length 8K video is the longest thing BlueShelf ever asks a computer to do. If
there is a second machine sitting idle, it can take half.

There is no server here and nothing to install on the other machine beyond BlueShelf itself.
Every machine watches the same folder - on the NAS, usually, beside the videos - and the work
is claimed by renaming a file. A rename either succeeds or it does not, and only one machine
can rename the same file, so two machines cannot take the same share. That is the whole
protocol, and it is deliberately the whole protocol: anything cleverer would need a process
that is always running somewhere, and this way a laptop can join in for an hour and leave.

A share left half-done by a machine that was shut down goes back in the queue on its own, so
nothing is lost by closing the lid.

    jobs/<id>/job.json          what to make, and the slices it was cut into
             /parts/N.waiting   a share nobody has taken
                   /N.running   taken - the name says by whom, the file is touched as it goes
                   /N.done      finished, the segment is in out/
                   /N.failed    it went wrong, and why
             /out/N.mp4         one machine's finished matte segment
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import socket
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from blueshelf import passthrough
from blueshelf.passthrough import Job, Slice

# A share whose file has not been touched for this long is taken to be abandoned. It wants to
# be comfortably longer than a slow chunk, or a machine that is merely busy gets robbed.
STALE_SECONDS = 20 * 60
# How often a working machine touches its file to say it is still there.
BEAT_SECONDS = 60
NAME = re.compile(r"^(\d+)\.(waiting|running|done|failed)(?:\.(.+))?$")
# How long a join waits for the machine best placed to do it before anyone may take it.
# Without a limit, a job would sit unfinished because one computer was switched off.
JOIN_WAIT = 10 * 60
# How long a finished job waits for the machine that posted it before a helper takes it over.
# Longer than any single share, because that machine only looks between shares.
ADOPT_WAIT = 25 * 60
# A measurement older than this says nothing about a machine that may not even be running.
MEASUREMENT_KEEPS = 24 * 60 * 60
# A failed share is put back on its own this many times before a person has to be asked. A
# share that failed once is most often a moment the network share went away - one packed
# piece came out 1,986 frames long of 5,000 that way, and the whole film then sat finished
# but for it, with every machine idle and nothing saying why.
AUTO_RETRIES = 1
# How long a failed share rests before it is put back, so that whatever cut it short - the
# NAS refusing connections for a minute, say - has passed, and the one retry is not spent
# failing the same way straight away.
RETRY_AFTER = 5 * 60


def this_machine() -> str:
    """Something a person would recognise in a list. Not used for anything but showing."""
    try:
        host = socket.gethostname()
    except OSError:
        host = "a computer"
    try:
        return f"{getpass.getuser()}@{host}"
    except Exception:  # noqa: BLE001 - a name is never worth an exception
        return host


def _tidy(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", name)[:60]


class Share:
    """The shared folder, seen from one machine."""

    def __init__(self, root: str | Path, machine: str | None = None):
        self.root = Path(root)
        self.machine = machine or this_machine()
        self._jobs: dict[str, Job] = {}
        self._records: dict[str, dict] = {}

    # ---------------------------------------------------------------- layout

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def parts_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "parts"

    def out_dir(self, job_id: str) -> Path:
        """Where this job's finished chunks go: the working folder beside the video.

        Not inside the job's own folder. Chunks are the expensive thing here - hours each -
        and they were being written in one place when a machine worked alone and another
        when the work was shared, so switching between the two meant copying them about and
        risked stranding them. One place, named after the video, reachable by every machine
        that can reach the video itself.
        """
        return passthrough.work_folder(self.job_of(job_id))

    def job_of(self, job_id: str) -> Job:
        """The job, read once and kept - segment paths are asked for constantly."""
        if job_id not in self._jobs:
            self._jobs[job_id] = self.read_job(job_id)[0]
        return self._jobs[job_id]

    def prepare(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def usable(self) -> str:
        """Empty if the folder can be used, otherwise the reason it cannot."""
        try:
            self.prepare()
            probe = self.jobs_dir / f".write-test-{uuid.uuid4().hex[:8]}"
            probe.write_text(self.machine, encoding="utf-8")
            probe.unlink()
        except (OSError, ValueError) as exc:
            # ValueError as well as OSError: a path with something in it Windows will not
            # take never reaches the filesystem to fail there.
            return f"{self.root} cannot be written to: {getattr(exc, 'strerror', None) or exc}"
        return ""

    # ---------------------------------------------------------------- what each machine can do

    @property
    def machines_dir(self) -> Path:
        return self.root / "machines"

    def publish_machine(self, facts: dict) -> None:
        """Leave this machine's measurements where the others can read them.

        Each writes its own file, so there is nothing to coordinate and nothing to lock.
        """
        self.machines_dir.mkdir(parents=True, exist_ok=True)
        facts = {**facts, "machine": self.machine, "at": time.time()}
        try:
            (self.machines_dir / f"{_tidy(self.machine)}.json").write_text(
                json.dumps(facts, indent=1), encoding="utf-8")
        except OSError:
            pass

    def machines(self) -> list[dict]:
        """What every machine has said about itself, newest measurement first."""
        if not self.machines_dir.is_dir():
            return []
        found = []
        for path in self.machines_dir.glob("*.json"):
            try:
                found.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(found, key=lambda m: m.get("at", 0), reverse=True)

    def my_turn_to_join(self, job_id: str) -> bool:
        """Whether this machine should do the joining, or leave it to a better-placed one.

        Joining is nothing but moving bytes - reading the pieces and writing one file - so
        it belongs on whichever machine has the best link, not on whoever happened to start
        the job. If that machine does not come and take it, anyone may after a while, since
        a job left unfinished because a computer was switched off would be worse.
        """
        best = self.best_for_the_tail()
        if best is None or best.get("machine") == self.machine:
            return True
        if time.time() - float(best.get("at", 0)) > MEASUREMENT_KEEPS:
            return True                      # nothing recent enough to defer to
        posted = float(self.details_of(job_id).get("posted_at", 0))
        return time.time() - posted > JOIN_WAIT

    def stranded(self, job_id: str) -> bool:
        """Every share matted, and nobody left who is going to put them together.

        Only the machine that posted a job assembles it. Close BlueShelf there - or have it
        closed for you - and the job is stranded: helpers take shares and never assemble, so
        a finished film sits with hours of matting behind it and nothing that will ever pick
        it up.

        The wait is longer than any one share, because the owner only looks between shares
        and may simply be part-way through one.
        """
        if self.stage_of(job_id) != "matte" or not self.state(job_id)["complete"]:
            return False
        settled = max((path.stat().st_mtime for _i, _k, _t, path in self._files(job_id)
                       if path.exists()), default=0.0)
        return settled > 0 and time.time() - settled > ADOPT_WAIT

    def take_over(self, job_id: str) -> bool:
        """Claim the right to assemble a stranded job. Only one machine can win.

        Assembling twice over would write the same enormous file from two directions, so the
        claim is a file that can only be created once - whoever creates it does the work.
        """
        flag = self.job_dir(job_id) / "assembling"
        try:
            with open(flag, "x", encoding="utf-8") as who:
                who.write(self.machine)
            return True
        except FileExistsError:
            try:    # unless whoever claimed it has gone quiet as well
                if time.time() - flag.stat().st_mtime > ADOPT_WAIT:
                    flag.unlink()
                    return self.take_over(job_id)
            except OSError:
                pass
            return False
        except OSError:
            return False

    def best_for_the_tail(self) -> dict | None:
        """Which machine should do the projecting, packing and joining.

        The one with the best link, not the best card. That stage moves tens of gigabytes
        and computes comparatively little, so a fast card on a slow line spends it waiting.
        """
        rated = [m for m in self.machines() if m.get("read_mbs")]
        if not rated:
            return None
        return max(rated, key=lambda m: min(m.get("read_mbs", 0), m.get("write_mbs", 0)))

    # ---------------------------------------------------------------- posting work

    def post(self, job: Job, pieces: list[Slice], stage: str = "matte",
             extra: dict | None = None) -> str:
        """Put a video in the queue, cut into shares. Returns the id it was filed under.

        Chunks already matted for this video - by this machine working alone, or by an
        attempt that was given up on - are counted as done where they lie. Nothing is moved
        and nothing is done twice, so a job half-finished alone can be handed to several
        machines without throwing away the morning's work.
        """
        self.retire_earlier_attempts(job)
        job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        # Remembered before anything asks where this job's chunks go, since that answer is
        # read out of the job itself.
        self._jobs[job_id] = job
        self._records.pop(job_id, None)
        self.parts_dir(job_id).mkdir(parents=True, exist_ok=True)
        (self.job_dir(job_id) / "job.json").write_text(json.dumps({
            "job": asdict(job),
            "pieces": [asdict(p) for p in pieces],
            "posted_by": self.machine,
            "posted_at": time.time(),
            "title": Path(job.source).name,
            # "matte" cuts the performer out; "pack" projects that and writes it into the
            # picture. The second has no state between frames, so its pieces can be any
            # size and any machine can take any of them.
            "stage": stage,
            **(extra or {}),
        }, indent=1), encoding="utf-8")
        self.out_dir(job_id).mkdir(parents=True, exist_ok=True)
        # The pieces exist to build the joined mask. If that mask is already made and the
        # right length, none of them are needed - not even the ones whose chunk has since
        # been lost. This was found the hard way: a finished mask sat there while hours were
        # spent matting the chunks it had been made from.
        done_already = (stage == "matte" and passthrough.finished(
            self.out_dir(job_id) / "matte.mp4", sum(p.kept for p in pieces)))
        for piece in pieces:
            if piece.blank:
                continue                      # nothing to matte
            if done_already:
                (self.parts_dir(job_id) / f"{piece.index}.done").write_text(
                    "", encoding="utf-8")
                continue
            if passthrough.finished(self.segment_of(job_id, piece), piece.kept):
                # Already matted, by this machine working alone or by an attempt before.
                # Nothing is moved or copied: it is already where this job expects it.
                (self.parts_dir(job_id) / f"{piece.index}.done").write_text("",
                                                                            encoding="utf-8")
                continue
            (self.parts_dir(job_id) / f"{piece.index}.waiting").write_text("", encoding="utf-8")
        return job_id

    def reclaim_old_layout(self, job: Job, pieces: list[Slice]) -> int:
        """Bring back chunks filed by an older version, which kept them inside the job.

        Chunks used to be written into the job's own folder, which meant they were stranded
        if the job was abandoned and invisible to everything that looks beside the video.
        They are hours of work each, so an upgrade must not quietly leave them behind.
        """
        into = passthrough.work_folder(job)
        want = os.path.normcase(str(Path(job.source)))
        brought = 0
        for job_id in self.jobs():
            old_out = self.job_dir(job_id) / "out"
            if not old_out.is_dir():
                continue
            try:
                if os.path.normcase(str(Path(self.read_job(job_id)[0].source))) != want:
                    continue
            except (OSError, ValueError, TypeError):
                continue
            into.mkdir(parents=True, exist_ok=True)
            for piece in pieces:
                if piece.blank:
                    continue
                here = passthrough.part_file(into, piece)
                if passthrough.finished(here, piece.kept):
                    continue
                stranded = old_out / f"{piece.index}.mp4"
                if not passthrough.finished(stranded, piece.kept):
                    continue
                try:
                    shutil.move(str(stranded), str(here))
                    brought += 1
                except OSError:
                    continue
        return brought

    def ready_made(self, pieces: list[Slice], folder: str | Path) -> int:
        """How many chunks are already matted in a folder."""
        folder = Path(folder)
        if not folder.is_dir():
            return 0
        return sum(1 for piece in pieces if not piece.blank
                   and passthrough.finished(passthrough.part_file(folder, piece), piece.kept))

    def retire_earlier_attempts(self, job: Job) -> int:
        """Take down any queue still open for this same video.

        Pressing start again used to leave the previous attempt's queue live, so machines
        worked through two lists for one video and did the same chunks twice over. Only one
        queue per video should ever be open.
        """
        want = os.path.normcase(str(Path(job.source)))
        retired = 0
        for job_id in self.jobs():
            try:
                theirs, _pieces = self.read_job(job_id)
            except (OSError, ValueError, TypeError):
                continue
            if os.path.normcase(str(Path(theirs.source))) != want:
                continue
            self.forget(job_id)
            retired += 1
        return retired

    def read_job(self, job_id: str) -> tuple[Job, list[Slice]]:
        raw = json.loads((self.job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
        return Job(**raw["job"]), [Slice(**p) for p in raw["pieces"]]

    def jobs(self) -> list[str]:
        if not self.jobs_dir.is_dir():
            return []
        return sorted(d.name for d in self.jobs_dir.iterdir()
                      if d.is_dir() and (d / "job.json").is_file())

    def by_urgency(self) -> list[str]:
        """The jobs in the order their work should be handed out: nearest to finished first.

        Oldest first on its own put a film's last stages behind every share of anything
        filed before them - a job adopted to be packed is filed now, so it sorted after a
        second film with thirty chunks still to matte, and a film that was one step from done
        waited most of a day. Joining and packing are what turn work already done into
        something you can watch, so they go ahead of matting; oldest first within each.
        """
        order = {"join": 0, "pack": 1}
        return sorted(self.jobs(), key=lambda job_id: (order.get(self.stage_of(job_id), 2),
                                                       job_id))

    def forget(self, job_id: str) -> None:
        """Take a job out of the queue.

        Only the queue. The chunks live beside the video and are not this folder's to throw
        away - the machine that finishes the job clears them once there is a finished copy
        to show for them.
        """
        self._jobs.pop(job_id, None)
        self._records.pop(job_id, None)
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    # ---------------------------------------------------------------- the state of a share

    def _files(self, job_id: str) -> list[tuple[int, str, str, Path]]:
        out = []
        folder = self.parts_dir(job_id)
        if not folder.is_dir():
            return out
        for path in folder.iterdir():
            found = NAME.match(path.name)
            if found:
                out.append((int(found.group(1)), found.group(2), found.group(3) or "", path))
        return sorted(out)

    def state(self, job_id: str) -> dict:
        """A plain account of where a job has got to, for showing in a list.

        ``retrying`` counts the failed shares that will still be put back on their own, so
        nothing gives up on a job - or asks a person to - while one is only resting.
        """
        counts = {"waiting": 0, "running": 0, "done": 0, "failed": 0}
        who, trouble = [], []
        retrying = 0
        for index, kind, tag, path in self._files(job_id):
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "running":
                who.append(tag)
            elif kind == "failed":
                trouble.append(f"share {index + 1}: {_first_line(path)}")
                if failures_of(path) <= AUTO_RETRIES:
                    retrying += 1
        total = sum(counts.values())
        return {"id": job_id, "total": total, "machines": sorted(set(who)),
                "failures": trouble, "retrying": retrying,
                "complete": total > 0 and counts["done"] == total, **counts}

    # ---------------------------------------------------------------- claiming

    def workers(self, job_id: str) -> list[dict]:
        """Who is working on what, and how long since they last said so.

        The heartbeat is the file's own modified time - a machine touches it while it works -
        so this answers the only question that matters from another room: is that computer
        actually doing something, or has it stopped and nobody noticed?
        """
        now = time.time()
        found = []
        for index, kind, tag, path in self._files(job_id):
            if kind != "running":
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                age = -1.0
            found.append({"index": index, "machine": tag or "someone", "age": age,
                          "stale": age > STALE_SECONDS})
        return found

    def claim(self, job_id: str | None = None) -> tuple[str, Slice, Job] | None:
        """Take the next share nobody is working on, or None if there is nothing to take.

        The rename is the claim. Two machines reaching for the same share is normal and
        costs nothing: one of them gets a missing file and moves to the next.
        """
        for candidate in ([job_id] if job_id else self.by_urgency()):
            try:
                job, pieces = self.read_job(candidate)
            except (OSError, ValueError, TypeError):
                continue
            for index, kind, _tag, path in self._files(candidate):
                if kind != "waiting":
                    continue
                piece = next((p for p in pieces if p.index == index), None)
                if piece is None:
                    self.fail(candidate, index, "the job file does not list this share")
                    continue
                # A queue written earlier still says "waiting" for work that has since been
                # done - by a later attempt at the same video, or by this machine alone.
                # Handing it out again wastes an hour and produces a file that already
                # exists, so the disk is asked before anybody is sent to do it.
                if (self.stage_of(candidate) == "join"
                        and not self.my_turn_to_join(candidate)):
                    continue                 # a better-connected machine should take this
                if passthrough.finished(self.segment_of(candidate, piece), piece.kept):
                    try:
                        os.replace(path, path.with_name(f"{index}.done"))
                    except OSError:
                        pass
                    continue
                taken = path.with_name(f"{index}.running.{_tidy(self.machine)}")
                try:
                    os.rename(path, taken)
                except OSError:
                    continue  # somebody else was quicker, or it went away
                self.beat(candidate, index)
                return candidate, piece, job
        return None

    def running_file(self, job_id: str, index: int) -> Path | None:
        for at, kind, _tag, path in self._files(job_id):
            if at == index and kind == "running":
                return path
        return None

    def beat(self, job_id: str, index: int) -> None:
        """Say this machine is still on it. Touching the file is the whole message."""
        path = self.running_file(job_id, index)
        if path is None:
            return
        try:
            os.utime(path, None)
        except OSError:
            pass

    def release(self, job_id: str, index: int) -> None:
        """Hand a share back without marking it failed - a machine leaving politely."""
        path = self.running_file(job_id, index)
        if path is not None:
            try:
                os.rename(path, path.with_name(f"{index}.waiting"))
            except OSError:
                pass

    def finish(self, job_id: str, index: int, segment: str | Path) -> None:
        path = self.running_file(job_id, index)
        piece = next((p for p in self.read_job(job_id)[1] if p.index == index), None)
        # segment_of and nothing else. This worked out the name itself for a while, and so
        # every stage landed on part-NNN.mp4 - the packing stage wrote its finished video
        # over the top of the matte chunk it had just been made from, and the join would
        # then go looking for packed-NNN.mp4 and find nothing.
        target = (self.segment_of(job_id, piece) if piece is not None
                  else self.out_dir(job_id) / f"part-{index:03d}.mp4")
        if Path(segment).resolve() != target.resolve():
            self.out_dir(job_id).mkdir(parents=True, exist_ok=True)
            # Not os.replace: a segment is built on local disk on purpose, so this move
            # almost always crosses from a drive to a network share, and a plain rename
            # cannot do that - "cannot move the file to a different disk drive". shutil.move
            # renames when it can and copies when it cannot.
            shutil.move(str(segment), str(target))
        if path is not None:
            os.replace(path, path.with_name(f"{index}.done"))
        else:  # claimed by a machine that has since been reaped - the work still counts
            (self.parts_dir(job_id) / f"{index}.done").write_text("", encoding="utf-8")

    def fail(self, job_id: str, index: int, reason: str) -> None:
        path = self.running_file(job_id, index) or (self.parts_dir(job_id) / f"{index}.waiting")
        target = self.parts_dir(job_id) / f"{index}.failed"
        # How many times it has failed so far travels inside the share's own file - it keeps
        # its contents through every rename, from failed to waiting to running - so the count
        # holds whichever machine takes it next, and across restarts.
        before = failures_of(path) if path.exists() else 0
        try:
            target.write_text(f"{self.machine}\n{TRIES}{before + 1}\n{reason}", encoding="utf-8")
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def retry(self, job_id: str, index: int) -> None:
        for at, kind, _tag, path in self._files(job_id):
            if at == index and kind in ("failed", "done"):
                try:
                    os.replace(path, path.with_name(f"{index}.waiting"))
                except OSError:
                    pass

    def retry_failed(self, job_id: str) -> int:
        """Offer the failed shares again. Returns how many went back in the queue.

        Worth one more attempt before giving up on a whole video: a share usually fails for
        a reason that is the same for all of them - somewhere it cannot write, a drive that
        filled - and once that is dealt with the rest of the job is still good.
        """
        again = 0
        for index, kind, _tag, path in self._files(job_id):
            if kind != "failed":
                continue
            try:
                os.replace(path, path.with_name(f"{index}.waiting"))
                again += 1
            except OSError:
                continue
        return again

    def retry_on_its_own(self, now: float | None = None) -> list[str]:
        """Put back failed shares that have failed only once and have rested long enough.

        Done by whichever machine is looking, since the machine that posted the job may be
        switched off - and until now nobody else ever tried again, so a single hiccup left a
        whole film one share short of joining. Returns what was put back, for the log.
        """
        now = time.time() if now is None else now
        told = []
        for job_id in self.jobs():
            for index, kind, _tag, path in self._files(job_id):
                if kind != "failed" or failures_of(path) > AUTO_RETRIES:
                    continue
                why = _first_line(path)
                try:
                    if now - path.stat().st_mtime < RETRY_AFTER:
                        continue
                    os.replace(path, path.with_name(f"{index}.waiting"))
                except OSError:
                    continue          # another machine put it back first
                try:
                    name = Path(self.read_job(job_id)[0].source).name
                except (OSError, ValueError, TypeError):
                    name = job_id
                told.append(f"Share {index + 1} of {name} failed ({why}) - trying it once more.")
        return told

    def release_mine(self) -> int:
        """Hand back anything this machine still appears to be holding.

        Called as a machine starts helping. Whatever it claimed in an earlier run it is
        certainly not working on now - it has only just started - and a claim left by a run
        that was killed rather than closed keeps that chunk away from every other machine
        until it falls silent for twenty minutes. On a machine restarted a few times, several
        chunks end up held by a computer doing none of them.
        """
        freed, mine = 0, _tidy(self.machine)
        for job_id in self.jobs():
            for index, kind, who, path in self._files(job_id):
                if kind != "running" or who != mine:
                    continue
                try:
                    os.rename(path, path.with_name(f"{index}.waiting"))
                    freed += 1
                except OSError:
                    continue
        return freed

    def recover(self, stale_after: float = STALE_SECONDS) -> int:
        """Put back shares whose machine stopped talking. Returns how many were freed."""
        freed = 0
        cutoff = time.time() - stale_after
        for job_id in self.jobs():
            for index, kind, _tag, path in self._files(job_id):
                if kind != "running":
                    continue
                try:
                    if path.stat().st_mtime > cutoff:
                        continue
                    os.rename(path, path.with_name(f"{index}.waiting"))
                    freed += 1
                except OSError:
                    continue
        return freed

    def segment_of(self, job_id: str, piece: Slice) -> Path:
        if self.stage_of(job_id) == "join":
            # The join writes the finished copy itself; there is nothing to file afterwards.
            return Path(self.details_of(job_id)["output"])
        if self.stage_of(job_id) == "pack":
            return self.out_dir(job_id) / f"packed-{piece.index:03d}.mp4"
        return passthrough.part_file(self.out_dir(job_id), piece)

    def details_of(self, job_id: str) -> dict:
        """The whole record, including what kind of work it is and what it needs."""
        if job_id not in self._records:
            try:
                self._records[job_id] = json.loads(
                    (self.job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._records[job_id] = {}
        return self._records[job_id]

    def stage_of(self, job_id: str) -> str:
        return self.details_of(job_id).get("stage", "matte")

    def segments(self, job_id: str) -> list[Path]:
        """The matted segments in order, or an empty list if any are still missing.

        Blank stretches are not in here. Nobody was asked to make them, and the machine doing
        the joining fills them in itself.
        """
        _job, pieces = self.read_job(job_id)
        found = []
        for piece in sorted(pieces, key=lambda p: p.index):
            if piece.blank:
                continue
            part = self.segment_of(job_id, piece)
            if not part.is_file():
                return []
            found.append(part)
        return found


def _first_line(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1][:200]
    except (OSError, IndexError):
        return "no reason recorded"


TRIES = "failures so far: "


def failures_of(path: Path) -> int:
    """How many times the share in this file has failed.

    A share that has never failed has an empty file. One that failed before the count was
    written down still carries the machine and the reason, and that is one failure.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith(TRIES):
            try:
                return int(line[len(TRIES):])
            except ValueError:
                break
    return 1 if text.strip() else 0


# --------------------------------------------------------------------------- lending a hand

class Helper:
    """A machine offering its graphics card to the queue.

    It claims a share, mattes it, drops the segment in the job's out folder and looks for the
    next one. It is meant to be left running and interrupted at any point: a share it was
    part-way through simply goes back in the queue.
    """

    def __init__(self, share: Share, install: str | Path,
                 report: Callable[[str], None] = lambda _m: None,
                 progress: passthrough.Progress = lambda _s, _f, _d: None,
                 work_dir: str | Path | None = None,
                 should_stop: Callable[[], bool] | None = None):
        self.share = share
        self.install = Path(install)
        self.report = report
        self.progress = progress
        self.work_dir = Path(work_dir) if work_dir else None
        self.stop_wanted = False
        # Asked continually, not copied once. Whoever owns this helper may decide to stop
        # in the middle of a chunk, and a chunk is ten minutes long - a flag read only
        # between them means Stop appears to do nothing at all.
        self._asked = should_stop or (lambda: self.stop_wanted)
        self.done_count = 0
        self.current: tuple[str, int] | None = None

    def stop(self) -> None:
        self.stop_wanted = True

    def stopping(self) -> bool:
        return self.stop_wanted or self._asked()

    def _staging(self, job_id: str, index: int) -> Path:
        """Where a segment is built. Locally by choice: matting writes a lot, and writing it
        all across the network would make the network the slow part instead of the card."""
        base = self.work_dir or Path(os.environ.get("TEMP") or ".") / "BlueShelf-matte"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{job_id}.{index}.mp4"

    def one(self) -> bool:
        """Take a share and do it. False when there was nothing waiting."""
        self.share.recover()
        for line in self.share.retry_on_its_own():
            self.report(line)
        taken = self.share.claim()
        if taken is None:
            return False
        job_id, piece, job = taken
        self.current = (job_id, piece.index)
        staging = self._staging(job_id, piece.index)
        last_beat = [time.monotonic()]
        stage = self.share.stage_of(job_id)
        doing = {"pack": "Packing", "join": "Joining"}.get(stage, "Share")
        self.report(f"{doing} {piece.index + 1} of {Path(job.source).name}: frames "
                    f"{piece.keep_from:,} to {piece.end:,}")
        try:
            if not Path(job.source).exists():
                raise passthrough.Unavailable(
                    f"This machine cannot see {job.source}. Both machines have to reach the "
                    f"video by the same path - a mapped drive letter that differs between "
                    f"them is the usual reason.")

            def on_progress(stage: str, share: float, detail: str) -> None:
                if time.monotonic() - last_beat[0] > BEAT_SECONDS:
                    last_beat[0] = time.monotonic()
                    self.share.beat(job_id, piece.index)
                self.progress(stage, share, detail)

            if stage == "join":
                told = self.share.details_of(job_id)
                inputs = [Path(f) for f in told["inputs"]]
                output = Path(told["output"])
                self.progress("Joining the finished pieces", 0.0,
                              f"{len(inputs)} of them")

                def along(share: float, frame: int, many=len(inputs)) -> None:
                    self.progress(f"Joining the finished pieces ({many} of them)",
                                  share, f"frame {frame:,}")

                passthrough.join(inputs, output, along)
                self.share.beat(job_id, piece.index)
                self.share.finish(job_id, piece.index, output)
            elif stage == "pack":
                # Projecting and packing a range: no warm-up, nothing to trim, and the
                # result is a finished piece of the copy rather than a mask.
                told = self.share.details_of(job_id)
                passthrough.pack_piece(self.install, job, told.get("picture", job.source),
                                       told["matte"], piece, staging, on_progress)
                # A piece that came out shorter than asked is never accepted as finished, so
                # without this it simply went back in the queue - and a piece that can never
                # reach its length went round for hours with nothing ever saying so.
                if not passthrough.finished(staging, piece.kept):
                    got = passthrough.frame_count(staging) if staging.is_file() else 0
                    raise passthrough.Unavailable(
                        f"came out {got:,} frames where {piece.kept:,} were asked for")
                self.share.beat(job_id, piece.index)
                self.share.finish(job_id, piece.index, staging)
            else:
                passthrough.matte_slice(self.install, job, piece, staging, on_progress,
                                        should_stop=self.stopping)
                self.progress("Trimming the warm-up frames off the share", 0.97, "")
                cut = passthrough.prepare_segment(
                    staging, piece, staging.with_name(staging.stem + ".cut.mp4"),
                    size=passthrough.mask_size_of(job) if job.small_mask else None)
                self.share.beat(job_id, piece.index)
                self.share.finish(job_id, piece.index, cut)
            self.done_count += 1
            self.report(f"Share {piece.index + 1} done.")
        except InterruptedError:
            self.share.release(job_id, piece.index)
            self.report("Stopped - the share went back in the queue.")
        except Exception as exc:  # noqa: BLE001 - a helper must never die on one bad share
            self.share.fail(job_id, piece.index, str(exc))
            self.report(f"Share {piece.index + 1} failed: {exc}")
        finally:
            self.current = None
            for leftover in (staging, staging.with_name(staging.stem + ".cut.mp4")):
                try:
                    leftover.unlink()
                except OSError:
                    pass
        return True

    def serve(self, idle_seconds: float = 20.0) -> None:
        """Keep taking shares until asked to stop."""
        while not self.stopping():
            if not self.one():
                self.report("Nothing waiting.")
                waited = 0.0
                while waited < idle_seconds and not self.stopping():
                    time.sleep(0.5)
                    waited += 0.5
