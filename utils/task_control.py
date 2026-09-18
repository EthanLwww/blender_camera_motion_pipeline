"""Cooperative cancellation and progress reporting.

A long generation run must never block the Blender UI, and the user must be
able to stop it.  Both the modal timer operator in the add-on and the headless
CLI therefore drive the same generators through this controller: the generators
poll :meth:`TaskController.check` at natural boundaries (per scene, per motion,
per sample frame) and raise :class:`TaskCancelled` to unwind cleanly.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class TaskCancelled(RuntimeError):
    """Raised when the user (or a signal handler) requested cancellation."""


class TaskController:
    """Thread-safe cancel flag plus throttled progress callbacks."""

    def __init__(
        self,
        *,
        name: str = "task",
        progress_callback: "Callable[[dict], None] | None" = None,
        min_interval: float = 0.05,
    ):
        self.name = name
        self._cancel = threading.Event()
        self._progress_callback = progress_callback
        self._min_interval = float(min_interval)
        self._last_emit = 0.0
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.total = 0
        self.completed = 0
        self.message = ""
        self.stage = ""
        self.error_count = 0

    # -- cancellation ----------------------------------------------------
    def cancel(self, reason: str = "") -> None:
        self.message = reason or "cancelled"
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def reason(self) -> str:
        return self.message

    def check(self) -> None:
        """Raise :class:`TaskCancelled` when cancellation was requested."""
        if self._cancel.is_set():
            raise TaskCancelled(self.message or "cancelled by user")

    def reset(self) -> None:
        self._cancel.clear()
        self.message = ""
        self.stage = ""
        self.total = 0
        self.completed = 0
        self.error_count = 0
        self.started_at = time.time()

    # -- progress --------------------------------------------------------
    def set_total(self, total: int, *, stage: str = "") -> None:
        self.total = int(total)
        if stage:
            self.stage = stage
        self.emit(force=True)

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self.emit(force=True)

    def tick(self, step: int = 1, *, stage: str | None = None, force: bool = False) -> None:
        self.completed += int(step)
        if stage:
            self.stage = stage
        self.emit(force=force)

    def note_error(self) -> None:
        self.error_count += 1

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.completed / float(self.total)))

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "cancelled": self.cancelled,
            "stage": self.stage,
            "total": self.total,
            "completed": self.completed,
            "fraction": self.fraction,
            "error_count": self.error_count,
            "elapsed_seconds": round(self.elapsed, 3),
            "message": self.message,
        }

    def emit(self, *, force: bool = False) -> None:
        if self._progress_callback is None:
            return
        now = time.time()
        with self._lock:
            if not force and (now - self._last_emit) < self._min_interval:
                return
            self._last_emit = now
        try:
            self._progress_callback(self.snapshot())
        except Exception:
            # A broken UI callback must never abort a generation run.
            pass
