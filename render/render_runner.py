"""Local video rendering driven from the sidebar panel.

Rendering a sequence means *loading another ``.blend``*, which would destroy the
user's current session if it happened in-process.  So the panel spawns the
standalone renderer (`render/render_sequences.py`) as a background Blender
process instead:

* the user's open file is never touched;
* the renderer runs the exact same code path a render farm would, so "it works
  in the panel" implies "it works headlessly";
* ``bpy.app.timers`` pumps the child process, so the UI stays responsive and
  **Stop** can terminate it.

One sequence is rendered per child process, mirroring the renderer's own
per-sequence semantics and keeping progress/cancel granular.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from itertools import count
from typing import Iterable

from ..io.json_io import load_json_file
from ..io.path_utils import ensure_dir, normalize_path, to_forward_slashes
from ..utils.logging_utils import get_logger

LOGGER = get_logger("render_runner")

#: Monotonic run counter, used to give every render its own scratch report path.
#: The process id alone is not enough: re-running inside the same Blender session
#: would reuse the file and a stale summary could be read as the new result.
_RUN_COUNTER = count(1)


def _allocate_report_path(output_root: str) -> str:
    """A unique, hidden scratch report path inside ``output_root``."""
    return os.path.join(
        normalize_path(output_root) or ".",
        f".panel_render_{os.getpid()}_{next(_RUN_COUNTER):04d}.json",
    )


#: Renderer script, resolved relative to this package.
_RENDER_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "render", "render_sequences.py",
)

#: Where the renderer writes its per-run summary.
REPORT_NAME = "render_report.json"


@dataclass
class RenderJob:
    """One sequence queued for local rendering."""

    sequence_dir: str
    sequence_id: str = ""
    scene_name: str = ""
    motion_name: str = ""
    blend: str = ""
    storage_mode: str = "blend"
    frame_start: int | None = None
    frame_end: int | None = None
    state: str = "pending"          # pending | running | done | failed | skipped
    output_dir: str = ""
    video: str = ""
    error: str = ""
    elapsed: float = 0.0

    @property
    def label(self) -> str:
        parts = [self.scene_name, self.motion_name, self.sequence_id]
        text = " / ".join(part for part in parts if part)
        return text or os.path.basename(self.sequence_dir)

    def to_dict(self) -> dict:
        return {
            "sequence_id": self.sequence_id,
            "sequence_dir": to_forward_slashes(self.sequence_dir),
            "scene_name": self.scene_name,
            "motion_name": self.motion_name,
            "blend": to_forward_slashes(self.blend),
            "storage_mode": self.storage_mode,
            "state": self.state,
            "output_dir": to_forward_slashes(self.output_dir),
            "video": to_forward_slashes(self.video),
            "error": self.error,
            "elapsed_seconds": round(self.elapsed, 3),
        }


def blender_executable() -> str:
    """The Blender binary to spawn (not the bundled Python interpreter)."""
    override = os.environ.get("MP_BLENDER", "").strip()
    if override and os.path.isfile(override):
        return override
    try:
        import bpy

        if bpy.app.binary_path and os.path.isfile(bpy.app.binary_path):
            return bpy.app.binary_path
    except Exception:
        pass
    return sys.executable


def render_script_path() -> str:
    return _RENDER_SCRIPT


def discover_sequences(root: str, *, recursive: bool = True) -> "list[dict]":
    """Sequences under ``root``, newest-last, using the shared sequence manager."""
    from ..core.sequence_manager import SequenceManager

    manager = SequenceManager(root)
    found = manager.find_sequences()
    if not recursive:
        base = normalize_path(root)
        found = [info for info in found if os.path.dirname(info.sequence_dir) == base]
    return [
        {
            "sequence_dir": info.sequence_dir,
            "sequence_id": info.sequence_id or os.path.basename(info.sequence_dir),
            "scene_name": info.scene_name,
            "motion_name": info.motion_name,
            "blend": (info.files.get("blend") or [""])[0],
            "storage_mode": info.storage_mode,
            "source_blend": str(((info.config or {}).get("sequence") or {}).get("source_blend") or ""),
            "frame_start": info.frame_start,
            "frame_end": info.frame_end,
            "has_video": info.has_video(),
            "has_partial_video": info.has_partial_video(),
            "problems": list(info.problems),
        }
        for info in found
    ]


class RenderRunner:
    """Sequentially render a list of sequences in child Blender processes."""

    def __init__(
        self,
        *,
        output_root: str,
        options: "dict | None" = None,
        logger=None,
    ):
        self.output_root = normalize_path(output_root)
        self.options = dict(options or {})
        self.logger = logger or LOGGER
        self.jobs: "list[RenderJob]" = []
        self.index = 0
        self.state = "idle"          # idle | running | cancelling | done | failed | cancelled
        self.process: "subprocess.Popen | None" = None
        self.current_started = 0.0
        self.started_at = 0.0
        self.finished_at = 0.0
        self.log_path = ""
        self._log_handle = None
        self.last_report: dict = {}
        # Allocate now, not in ``start()``: ``_command`` may be built first, and a
        # blank ``--report`` would let the renderer fall back to the shared name.
        self.report_path = _allocate_report_path(self.output_root)
        self._previous_report_path = ""

    # -- lifecycle -------------------------------------------------------
    def start(self, jobs: Iterable[RenderJob], *, log_path: str = "") -> "list[RenderJob]":
        self.jobs = list(jobs)
        for job in self.jobs:
            job.state = "pending"
            job.error = ""
            job.video = ""
            job.output_dir = ""
        self.index = 0
        self.state = "running" if self.jobs else "done"
        self.started_at = time.time()
        self.finished_at = 0.0
        self.last_report = {}
        ensure_dir(self.output_root)
        # Every run gets its own scratch report so a previous run's summary can
        # never be read as this one's result.  Re-allocate defensively in case
        # another runner claimed the path in the meantime.
        if not self.report_path or os.path.exists(self.report_path):
            self.report_path = _allocate_report_path(self.output_root)
        self.log_path = log_path or os.path.join(self.output_root, "panel_render.log")
        try:
            self._log_handle = open(self.log_path, "a", encoding="utf-8", newline="\n")
            self._log_handle.write(
                f"\n=== panel render started {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"({len(self.jobs)} sequence(s)) ===\n"
            )
            self._log_handle.flush()
        except OSError as exc:
            self.logger.warning("cannot write %s: %s", self.log_path, exc)
            self._log_handle = None
        return self.jobs

    def is_running(self) -> bool:
        return self.state in ("running", "cancelling")

    def cancel(self, reason: str = "") -> None:
        if not self.is_running():
            return
        self.state = "cancelling"
        self._note(f"cancel requested: {reason or 'stopped from the panel'}")
        self._kill_process()

    def _kill_process(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=10)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def close(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    def cleanup(self) -> None:
        """Remove every scratch report this run produced.

        The report path is unique per panel render (``.panel_render_<pid>.json``)
        so a previous run's summary can never be mistaken for this one's.  Both
        the current and the previous path are swept, because ``configure()``
        replaces the runner without the old one ever reaching ``_finish``.
        """
        targets = {self.report_path, self._previous_report_path}
        for path in targets:
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    # -- stepping --------------------------------------------------------
    def step(self) -> bool:
        """Advance the render by at most one unit.  Returns True when finished."""
        if self.state in ("done", "failed", "cancelled"):
            return True
        if self.state == "cancelling":
            self._kill_process()
            self._finish("cancelled")
            return True

        if self.process is None:
            job = self._next_pending()
            if job is None:
                self._finish("failed" if self._failed_jobs() else "done")
                return True
            self._launch(job)
            return False

        code = self.process.poll()
        if code is None:
            return False                    # still rendering this sequence
        elapsed = time.time() - self.current_started
        job = self.jobs[self.index - 1]
        job.elapsed = elapsed
        self._collect(job, code)
        self.process = None
        return False

    def _next_pending(self) -> "RenderJob | None":
        while self.index < len(self.jobs):
            job = self.jobs[self.index]
            self.index += 1
            if job.state == "skipped":
                continue
            return job
        return None

    def _launch(self, job: RenderJob) -> None:
        job.state = "running"
        job.output_dir = self._output_dir(job)
        self.current_started = time.time()
        command = self._command(job)
        self._note("$ " + " ".join(f'"{part}"' if " " in part else part for part in command))
        try:
            self.process = subprocess.Popen(
                command,
                stdout=self._log_handle or subprocess.DEVNULL,
                stderr=self._log_handle or subprocess.DEVNULL,
                cwd=self.output_root or None,
            )
        except Exception as exc:
            self.process = None
            job.state = "failed"
            job.error = f"could not start Blender: {exc}"
            self._note(f"FAILED {job.sequence_id}: {job.error}")
        self._flush()

    def _command(self, job: RenderJob) -> "list[str]":
        options = self.options
        command = [
            blender_executable(), "-b",
            "-P", render_script_path(), "--",
            "--input", job.sequence_dir,
            "--output-root", self.output_root,
            "--report", self.report_path,
            "--log-level", str(options.get("log_level", "INFO")),
        ]
        # ``--input`` names one sequence folder, so ``--recursive`` is irrelevant;
        # only ``--flat`` changes where the artifacts land.
        if options.get("flat"):
            command.append("--flat")
        if options.get("overwrite"):
            command.append("--overwrite")
        if options.get("dry_run"):
            command.append("--dry-run")
        if options.get("skip_existing") is False:
            command.append("--no-skip-existing")
        for flag, key in (
            ("--resolution-x", "resolution_x"),
            ("--resolution-y", "resolution_y"),
            ("--resolution-percentage", "resolution_percentage"),
            ("--samples", "samples"),
            ("--frame-start", "frame_start"),
            ("--frame-end", "frame_end"),
        ):
            value = options.get(key)
            if value:
                command += [flag, str(value)]
        for flag, key in (
            ("--fps", "fps"),
        ):
            value = options.get(key)
            if value:
                command += [flag, f"{float(value):g}"]
        for flag, key in (
            ("--engine", "engine"),
            ("--device", "device"),
            ("--video-format", "video_format"),
            ("--codec", "codec"),
            ("--crf", "crf"),
            ("--trajectory-mode", "trajectory_mode"),
        ):
            value = options.get(key)
            if value:
                command += [flag, str(value)]
        step = options.get("trajectory_step")
        if step and int(step) > 1:
            command += ["--trajectory-step", str(int(step))]
        for pair in options.get("path_maps") or []:
            command += ["--path-map", str(pair)]
        if options.get("keep_frames"):
            command += ["--frames-output", "--keep-frames"]
        return command

    def _output_dir(self, job: RenderJob) -> str:
        if self.options.get("flat"):
            return os.path.join(self.output_root, job.sequence_id or "sequence")
        return os.path.join(
            self.output_root,
            job.scene_name or "scene",
            job.motion_name or "motion",
            job.sequence_id or "sequence",
        )

    def _collect(self, job: RenderJob, exit_code: int) -> None:
        """Read this run's report for the sequence and set the job state."""
        entry = None
        payload = load_json_file(self.report_path, default=None, required=False)
        if isinstance(payload, dict):
            self.last_report = payload
            for item in (payload.get("results") or []):
                if os.path.normcase(item.get("sequence_dir", "")) == os.path.normcase(job.sequence_dir):
                    entry = item
                    break
            if entry is None:
                for item in (payload.get("rendered") or []):
                    if item.get("sequence_id") == job.sequence_id:
                        entry = item
                        break
                for item in (payload.get("failed") or []):
                    if item.get("sequence_id") == job.sequence_id:
                        entry = item
                        break

        if entry:
            files = entry.get("files") or {}
            job.video = files.get("video", "")
            job.output_dir = entry.get("output_dir") or job.output_dir
            if entry.get("skipped"):
                job.state = "skipped"
                self._note(f"SKIPPED {job.sequence_id}: {entry.get('skip_reason', 'already rendered')}")
                return

        if exit_code == 0:
            job.state = "done"
            if not job.video:
                job.video = self._find_video(job)
            self._note(f"DONE {job.sequence_id} -> {to_forward_slashes(job.video or job.output_dir)}")
        else:
            job.state = "failed"
            job.error = (entry or {}).get("error") or f"Blender exited with code {exit_code}"
            self._note(f"FAILED {job.sequence_id}: {job.error}")

    def _find_video(self, job: RenderJob) -> str:
        directory = job.output_dir
        if not os.path.isdir(directory):
            return ""
        for name in sorted(os.listdir(directory)):
            if name.lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov")):
                return os.path.join(directory, name)
        return ""

    def _failed_jobs(self) -> "list[RenderJob]":
        return [job for job in self.jobs if job.state == "failed"]

    def _finish(self, state: str) -> None:
        self.state = state
        self.finished_at = time.time()
        self._note(
            f"=== finished: {self.done_count} rendered, {self.failed_count} failed, "
            f"{self.skipped_count} skipped in {self.elapsed:.1f}s ==="
        )
        self._flush()
        self.close()
        self.cleanup()

    # -- reporting -------------------------------------------------------
    @property
    def done_count(self) -> int:
        return sum(1 for job in self.jobs if job.state == "done")

    @property
    def failed_count(self) -> int:
        return sum(1 for job in self.jobs if job.state == "failed")

    @property
    def skipped_count(self) -> int:
        return sum(1 for job in self.jobs if job.state == "skipped")

    @property
    def finished_count(self) -> int:
        return sum(
            1 for job in self.jobs if job.state in ("done", "failed", "skipped")
        )

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    @property
    def fraction(self) -> float:
        if not self.jobs:
            return 1.0
        if not self.is_running():
            return 1.0
        return min(1.0, self.finished_count / float(len(self.jobs)))

    def current_job(self) -> "RenderJob | None":
        if not self.is_running():
            return None
        if 0 < self.index <= len(self.jobs):
            candidate = self.jobs[self.index - 1]
            if candidate.state in ("running", "pending"):
                return candidate
        return None

    def snapshot(self) -> dict:
        current = self.current_job()
        return {
            "state": self.state,
            "total": len(self.jobs),
            "completed": self.finished_count,
            "fraction": self.fraction,
            "done": self.done_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "elapsed_seconds": round(self.elapsed, 2),
            "current": current.label if current else "",
            "current_sequence": current.sequence_id if current else "",
            "output_root": to_forward_slashes(self.output_root),
            "log_path": to_forward_slashes(self.log_path),
        }

    def failures(self) -> "list[dict]":
        return [
            {
                "sequence_id": job.sequence_id,
                "sequence_dir": to_forward_slashes(job.sequence_dir),
                "error": job.error,
            }
            for job in self.jobs
            if job.state == "failed"
        ]

    # -- log -------------------------------------------------------------
    def _note(self, message: str) -> None:
        self.logger.info("%s", message)
        if self._log_handle is not None:
            try:
                self._log_handle.write(message + "\n")
            except Exception:
                pass

    def _flush(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.flush()
            except Exception:
                pass


#: Module-level singleton, mirroring ``core.ui_task``.
_runner = RenderRunner(output_root="")


def runner() -> RenderRunner:
    return _runner


def configure(output_root: str, options: "dict | None" = None) -> RenderRunner:
    """Point the shared runner at a new output root / option set.

    The report path is allocated here (not only in :meth:`RenderRunner.start`)
    so the child command always carries a real ``--report`` argument even if a
    caller builds the command before starting.
    """
    global _runner
    if _runner.is_running():
        # Never leave the panel stuck on a wedged child process: a user who
        # presses Render again means "stop that and do this".
        LOGGER.warning("a render was still running; terminating it before the new run")
        _runner.cancel("superseded by a new render")
        _runner._kill_process()
        _runner._finish("cancelled")
    old_path = _runner.report_path
    _runner.close()
    _runner = RenderRunner(output_root=output_root, options=options)
    _runner.report_path = _allocate_report_path(output_root)
    _runner._previous_report_path = old_path
    _runner.cleanup()
    return _runner



def is_running() -> bool:
    return _runner.is_running()


def start(jobs, *, output_root: str, options: "dict | None" = None, log_path: str = ""):
    configure(output_root, options)
    return _runner.start(jobs, log_path=log_path)


def step() -> bool:
    return _runner.step()


def snapshot() -> dict:
    return _runner.snapshot()


def cancel(reason: str = "") -> None:
    _runner.cancel(reason)


def failures() -> "list[dict]":
    return _runner.failures()


def summary_text() -> str:
    if not _runner.jobs:
        return "no render has been started yet"
    snapshot_value = _runner.snapshot()
    return (
        f"{snapshot_value['done']} rendered, {snapshot_value['failed']} failed, "
        f"{snapshot_value['skipped']} skipped of {snapshot_value['total']} "
        f"({snapshot_value['state']})"
    )


__all__ = [
    "REPORT_NAME",
    "RenderJob",
    "RenderRunner",
    "blender_executable",
    "cancel",
    "configure",
    "discover_sequences",
    "failures",
    "is_running",
    "render_script_path",
    "runner",
    "snapshot",
    "start",
    "step",
    "summary_text",
]
