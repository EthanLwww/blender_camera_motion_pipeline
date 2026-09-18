"""Incremental, cancellable generation state used by the panel operator.

The sidebar must never freeze, so generation is driven from ``bpy.app.timers``
one *sequence* at a time.  This module owns the small amount of state that
requires:

* the template library and the character provider are built once, at ``begin``;
* scenes are processed one at a time -- a scene is opened, its sequence requests
  are expanded against the cameras that actually exist in it, and every request
  is generated before the next scene is opened;
* the total request count is accumulated as scenes are expanded, so the progress
  bar is monotonic and honest;
* ``failures`` collects structured reasons for the error report.

The state machine is plain Python; only the work it calls into needs ``bpy``.
"""

from __future__ import annotations

import fnmatch
import os
import time
import traceback

from ..config.models import BatchConfig, ConfigError
from ..utils.logging_utils import get_logger
from ..utils.task_control import TaskController
from .scene_loader import SceneEntry, open_scene_for_generation, scene_name_for
from .sequence_generator import SequenceGenerator, SequenceRequest, SequenceResult

LOGGER = get_logger("ui_task")


class UITaskState:
    """Holder for the in-flight panel generation."""

    def __init__(self) -> None:
        self.controller: "TaskController | None" = None
        self.config: "BatchConfig | None" = None
        self.generator: "SequenceGenerator | None" = None
        #: Loaded motion template library (built once, at ``begin``).
        self.library = None
        #: Character adapter in use.
        self.provider = None
        #: Expanded character variants (has_character, character, animation, note).
        self.variants: "list[tuple]" = []
        #: ``all``, camera names, or camera indices.
        self.camera_selection: str = "all"
        self.scenes: "list[SceneEntry]" = []
        self.scene_index: int = 0
        self.queue: "list[SequenceRequest]" = []
        self.queue_index: int = 0
        self.total_requests: int = 0
        self.started_at: float = 0.0
        self.finished_at: float = 0.0
        self.state: str = "idle"
        self.stage: str = ""
        self.generated: int = 0
        self.failed: int = 0
        self.skipped: int = 0
        self.failures: "list[dict]" = []
        self.setup: dict = {}
        self._opened_scene: str = ""

    # -- lifecycle -------------------------------------------------------
    def begin(
        self,
        config: BatchConfig,
        entries,
        *,
        camera_selection: str = "all",
        log_callback=None,
    ) -> dict:
        """Validate the setup, build the shared objects and queue the scenes."""
        from ..camera.motion_templates import MotionTemplateLibrary
        from ..character import build_character_provider
        from ..character.base_provider import character_variants, summarise_variants
        from ..io.path_utils import parse_path_mappings

        if self.is_running():
            raise RuntimeError("a generation task is already running")

        self.__init__()
        self.config = config
        self.controller = TaskController(name="panel")
        self.state = "preparing"
        self.stage = "loading motion templates"
        self.started_at = time.time()
        self.camera_selection = camera_selection or "all"

        library = MotionTemplateLibrary.from_config(config.motion, logger=LOGGER)
        self._apply_motion_filter(library, config.motion.template_names)
        self.library = library

        mappings = parse_path_mappings(config.batch.path_mappings)
        provider = build_character_provider(
            config.batch.character_provider,
            asset_root=config.batch.character_asset_root,
            animation_root=config.batch.animation_asset_root,
            config=config.batch,
            mappings=mappings,
            logger=LOGGER,
        )
        self.provider = provider
        self.variants = character_variants(config.batch.mode, provider, logger=LOGGER)
        self.generator = SequenceGenerator(
            config,
            output_root=config.batch.output_root,
            character_provider=provider,
            logger=LOGGER,
            task=self.controller,
        )
        # Timer-driven runs must not call save_as_mainfile (it crashes Blender),
        # so sequence .blend writes are queued for a later flush.
        self.generator.defer_blend_save = True
        self.scenes = [entry for entry in entries if entry.enabled]
        self.controller.set_total(0, stage="queued")
        self.state = "running"
        self.stage = "queued"
        self.setup = {
            "template_count": len(library),
            "template_source": library.source,
            "provider": provider.availability(),
            "variants": summarise_variants(self.variants),
            "scene_count": len(self.scenes),
            "request_count": 0,
            "camera_selection": self.camera_selection,
            "warnings": list(library.warnings),
            "scenes": {},
        }
        LOGGER.info(
            "panel generation queued: %d scene(s), %d template(s), %s",
            len(self.scenes), len(library), self.setup["variants"],
        )
        del log_callback  # the panel reads last_report directly
        return self.setup

    def _apply_motion_filter(self, library, names) -> None:
        patterns = [str(name) for name in (names or []) if name]
        if not patterns:
            return
        if any(character in pattern for pattern in patterns for character in "*?["):
            matched = [
                template.name for template in library
                if any(fnmatch.fnmatch(template.name, pattern) for pattern in patterns)
            ]
            if not matched:
                raise ConfigError(
                    f"the motion filter {patterns} matched none of the {len(library)} template(s)"
                )
            library.restrict_to(matched)
        else:
            library.restrict_to(patterns)

    @staticmethod
    def _camera_spec(spec: str) -> "list[str]":
        spec = (spec or "all").strip()
        if not spec or spec.lower() == "all":
            return []
        return [part.strip() for part in spec.split(",") if part.strip()]

    def is_running(self) -> bool:
        return self.state in ("preparing", "running", "cancelling")

    def request_cancel(self, reason: str = "") -> None:
        if self.controller is not None:
            self.controller.cancel(reason)
        if self.state in ("preparing", "running"):
            self.state = "cancelling"
        self.stage = "cancelling"

    def abort(self, reason: str) -> None:
        self.state = "failed"
        self.stage = reason
        self.failures.append({"sequence_id": "", "error": reason, "stage": "task"})
        self.finished_at = time.time()

    def finish(self, *, state: str = "done") -> None:
        self.state = state
        self.stage = {
            "done": "done",
            "failed": "finished with failures",
            "cancelled": "cancelled",
        }.get(state, state)
        self.finished_at = time.time()
        LOGGER.info(
            "panel generation %s: %d generated, %d failed, %d skipped in %.1fs",
            state, self.generated, self.failed, self.skipped, self.elapsed,
        )

    # -- stepping --------------------------------------------------------
    def step(self) -> bool:
        """Process the next unit of work.  Returns True when everything is done.

        One call either expands the next scene into requests, or generates one
        sequence.  Keeping the unit small is what makes cancellation responsive.
        """
        if self.controller is None or self.generator is None:
            raise RuntimeError("step() called before begin()")
        if self.controller.cancelled:
            self.state = "cancelling"
            return True

        # Drain the current scene's queue.
        while self.queue_index < len(self.queue):
            request = self.queue[self.queue_index]
            self._generate(request)
            self.queue_index += 1
            self.controller.completed = self.generated + self.failed + self.skipped
            if self.controller.cancelled:
                self.state = "cancelling"
                return True
            return False

        # Move to the next scene.
        if self.scene_index >= len(self.scenes):
            self.controller.completed = self.controller.total
            return True
        self._expand_next_scene()
        if not self.queue and self.scene_index >= len(self.scenes):
            return True
        return False

    def _expand_next_scene(self) -> None:
        """Open the next scene and build its sequence requests."""
        from . import blender_context as bctx
        from .scene_loader import current_blend_path

        entry = self.scenes[self.scene_index]
        self.scene_index += 1
        self.stage = f"opening {os.path.basename(entry.path)}"
        self.controller.set_stage(self.stage)

        load = open_scene_for_generation(entry)
        self.setup["scenes"][entry.path] = load.to_dict()
        if not load.ok:
            LOGGER.error("cannot open %s: %s", entry.path, load.error)
            self.failures.append({
                "sequence_id": "",
                "scene_name": scene_name_for(entry),
                "source_blend": entry.path,
                "error": load.error,
                "stage": "load",
            })
            self.failed += 1
            self.queue = []
            self.queue_index = 0
            return
        self._opened_scene = entry.path

        try:
            if os.path.normcase(current_blend_path()) != os.path.normcase(entry.path):
                # open_mainfile did not take effect (headless edge case): do not
                # silently generate against the wrong scene.
                raise RuntimeError("the requested scene is not the one currently loaded")
            camera_names = bctx.scene_camera_names()
        except Exception as exc:
            LOGGER.error("cannot inspect %s: %s", entry.path, exc, exc_info=True)
            self.failures.append({
                "sequence_id": "",
                "scene_name": scene_name_for(entry),
                "source_blend": entry.path,
                "error": f"could not inspect the scene: {exc}",
                "stage": "inspect",
            })
            self.failed += 1
            self.queue = []
            self.queue_index = 0
            return

        wanted = self._camera_spec(self.camera_selection)
        if wanted:
            if all(item.lstrip("-").isdigit() for item in wanted):
                selected = [camera_names[int(item)] for item in wanted
                            if 0 <= int(item) < len(camera_names)]
            else:
                selected = [name for name in wanted if name in camera_names]
            missing = [name for name in wanted if name not in selected]
            for name in missing:
                LOGGER.warning("camera %r is not present in %s", name, entry.path)
        else:
            selected = list(camera_names)

        if not selected:
            message = (
                f"scene {scene_name_for(entry)!r} has no usable camera"
                if not camera_names else
                f"camera selection {self.camera_selection!r} matched no camera"
            )
            LOGGER.error("%s: %s", entry.path, message)
            self.failures.append({
                "sequence_id": "",
                "scene_name": scene_name_for(entry),
                "source_blend": entry.path,
                "error": message,
                "stage": "no_camera",
            })
            self.failed += 1
            self.queue = []
            self.queue_index = 0
            return

        requests = self.generator.build_requests(
            entry,
            library=self.library,
            cameras=selected,
            character_variants=self.variants,
            start_index=self.total_requests + 1,
        )
        self.queue = requests
        self.queue_index = 0
        self.total_requests += len(requests)
        self.setup["request_count"] = self.total_requests
        self.controller.set_total(self.total_requests, stage=f"scene {scene_name_for(entry)}")
        LOGGER.info(
            "expanded scene %s: %d camera(s) x %d template(s) x %d variant(s) = %d sequence(s)",
            scene_name_for(entry), len(selected), len(self.library),
            len(self.variants), len(requests),
        )

    def _generate(self, request: SequenceRequest) -> None:
        self.stage = f"{request.motion_name} / {request.camera_name}"
        self.controller.set_stage(self.stage)
        try:
            result = self.generator.generate(request)
        except Exception as exc:
            LOGGER.error("sequence %s failed: %s", request.sequence_folder, exc, exc_info=True)
            result = SequenceResult(request=request, ok=False, error=f"{type(exc).__name__}: {exc}")
            self.failures.append({
                "sequence_id": request.sequence_folder,
                "scene_name": request.scene_name,
                "motion_name": request.motion_name,
                "camera_name": request.camera_name,
                "error": result.error,
                "traceback": traceback.format_exc(),
            })
            self.failed += 1
            return

        if result.ok and result.skipped:
            self.skipped += 1
        elif result.ok:
            self.generated += 1
        else:
            self.failures.append({
                "sequence_id": request.sequence_folder,
                "scene_name": request.scene_name,
                "motion_name": request.motion_name,
                "camera_name": request.camera_name,
                "error": result.error,
                "output_dir": result.output_dir,
            })
            self.failed += 1

    # -- reporting -------------------------------------------------------
    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    @property
    def fraction(self) -> float:
        total = max(self.total_requests, 1)
        done = self.generated + self.failed + self.skipped
        if not self.is_running() and self.state != "idle":
            return 1.0
        return min(1.0, done / float(total))

    # -- deferred sequence blends ----------------------------------------
    @property
    def pending_blends(self) -> "list[str]":
        """Sequence ``.blend`` files still waiting to be written."""
        if self.generator is None:
            return []
        return list(self.generator.pending_blend_saves)

    def flush_pending_blends(self, *, limit: int = 0) -> "list[str]":
        """Write queued sequence blends.  Safe outside a timer callback.

        Reopens nothing: each pending path is written from the scene as it
        stands at flush time, so a flush that happens after the batch moved on
        produces a blend for the *last* processed sequence only.  The batch
        runner (non-timer path) never defers, and the panel flushes whenever it
        gets a chance, which keeps this a best-effort convenience rather than a
        correctness-critical path.
        """
        if self.generator is None:
            return []
        written: "list[str]" = []
        pending = self.generator.pending_blend_saves
        budget = pending if not limit else pending[:limit]
        remaining = [path for path in pending if path not in budget]
        self.generator.pending_blend_saves = []
        from .sequence_generator import write_sequence_blend

        for path in budget:
            try:
                written.append(write_sequence_blend(path))
            except Exception as exc:
                LOGGER.warning("could not write %s: %s", path, exc)
                remaining.append(path)
        self.generator.pending_blend_saves = remaining
        return written

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "stage": self.stage,
            "total": self.total_requests,
            "completed": self.generated + self.failed + self.skipped,
            "fraction": self.fraction,
            "generated": self.generated,
            "failed": self.failed,
            "skipped": self.skipped,
            "elapsed_seconds": round(self.elapsed, 3),
            "failure_count": len(self.failures),
            "setup": dict(self.setup),
        }

    def failure_list(self) -> "list[dict]":
        return list(self.failures)

    def reset(self) -> None:
        self.__init__()


#: Module-level singleton shared by the panel operator and its timer.
_singleton = UITaskState()


def state() -> UITaskState:
    return _singleton


def is_running() -> bool:
    return _singleton.is_running()


def begin(config, entries, **kwargs) -> dict:
    return _singleton.begin(config, entries, **kwargs)


def step() -> bool:
    return _singleton.step()


def snapshot() -> dict:
    return _singleton.snapshot()


def request_cancel(reason: str = "") -> None:
    _singleton.request_cancel(reason)


def abort(reason: str) -> None:
    _singleton.abort(reason)


def finish(*, state: str = "done") -> None:
    _singleton.finish(state=state)


def failures() -> "list[dict]":
    return _singleton.failure_list()


def pending_blends() -> "list[str]":
    return _singleton.pending_blends


def flush_pending_blends(*, limit: int = 0) -> "list[str]":
    return _singleton.flush_pending_blends(limit=limit)


def reset() -> None:
    _singleton.reset()


def task_controller() -> "TaskController | None":
    """The controller of the running task (``None`` when idle)."""
    return _singleton.controller


__all__ = [
    "UITaskState",
    "abort",
    "begin",
    "failures",
    "finish",
    "flush_pending_blends",
    "is_running",
    "pending_blends",
    "request_cancel",
    "reset",
    "snapshot",
    "state",
    "step",
    "task_controller",
]
