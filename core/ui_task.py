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
        #: The project folder this run writes into (``None`` until ``begin``).
        self.project_layout = None
        #: ``sequence/`` inside the project folder.
        self.output_root: str = ""
        #: What packing embedded and what placing the focus models did, per scene.
        self.asset_packs: "list[dict]" = []
        self.focus_placements: "list[dict]" = []
        #: ``{base scene copy: [(model, variant entry), ...]}`` -- one copy per subject.
        self.focus_variants: "dict" = {}
        self.focus_models_list: "list" = []

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
        from .project import ProjectError, create_project

        if self.is_running():
            raise RuntimeError("a generation task is already running")

        self.__init__()
        self.config = config
        self.controller = TaskController(name="panel")
        self.state = "preparing"
        self.stage = "loading motion templates"
        self.started_at = time.time()
        self.camera_selection = camera_selection or "all"

        # One project folder per run: the sequence tree plus a copy of every scene it
        # was generated from.  Data only -- the renderer comes from the render image,
        # so the folder can be zipped/uploaded as it is.
        self.project_layout = create_project(config.batch.output_root, logger=LOGGER)
        self.output_root = self.project_layout.sequence_root
        self.stage = "copying scenes into the project"
        self.scenes = [entry for entry in entries if entry.enabled]
        for entry in self.scenes:
            if getattr(entry, "original_path", ""):
                continue
            try:
                copy = self.project_layout.stage_scene(entry.path, logger=LOGGER)
            except Exception as exc:  # OSError / ProjectError
                raise ProjectError(f"scene could not be copied into the project: {entry.path} ({exc})")
            entry.original_path = entry.path
            entry.path = copy
            self._pack_and_place_focus(copy)

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
            output_root=self.output_root,
            character_provider=provider,
            logger=LOGGER,
            task=self.controller,
            project_layout=self.project_layout,
        )
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
            "project_root": self.project_layout.project_root,
            "project_folder": self.project_layout.root,
            "output_root": self.output_root,
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
        self.finalise_project()
        LOGGER.info(
            "panel generation %s: %d generated, %d failed, %d skipped in %.1fs",
            state, self.generated, self.failed, self.skipped, self.elapsed,
        )

    def finalise_project(self) -> None:
        """Write the project README and manifest once the run is over.

        Best-effort: a generation that produced sequences must not be reported as
        failed because a documentation file could not be written.
        """
        layout = self.project_layout
        if layout is None:
            return
        try:
            from .project import blender_version

            version = blender_version()
            scenes = [
                layout.relative_scene(target) or target
                for _, target in layout.scene_copies
            ]
            layout.write_readme(scenes=[name for name in scenes if name], blender_version=version)
            layout.write_manifest(
                metadata={
                    "character_mode": self.config.batch.mode if self.config else "",
                    "camera_selection": self.camera_selection,
                    "generated": int(self.generated),
                    "failed": int(self.failed),
                    "skipped": int(self.skipped),
                    "output_root": self.output_root,
                },
                blender_version=version,
            )
            self.setup["project"] = layout.to_dict()
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("could not finalise the project folder: %s", exc)

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

        focus_variants = self._focus_variants(entry)
        requests = self.generator.build_requests(
            entry,
            library=self.library,
            cameras=selected,
            character_variants=self.variants,
            start_index=self.total_requests + 1,
            focus_variants=focus_variants,
        )
        self.queue = requests
        self.queue_index = 0
        self.total_requests += len(requests)
        self.setup["request_count"] = self.total_requests
        self.controller.set_total(self.total_requests, stage=f"scene {scene_name_for(entry)}")
        LOGGER.info(
            "expanded scene %s: %d camera(s) x %d template(s) x %d variant(s) x %d focus "
            "object(s) = %d sequence(s)",
            scene_name_for(entry), len(selected), len(self.library),
            len(self.variants), len(focus_variants) or 1, len(requests),
        )
        self._opened_scene = entry.path

    def _pack_and_place_focus(self, copy: str) -> None:
        """Embed the base copy's assets, then give every focus model its own copy.

        Runs in throwaway Blender processes (``pack_textures.py`` and
        ``place_focus_objects.py``): the artist's file is open in this session, so the
        staged copies have to be finished out of process.  Problems are recorded as
        warnings -- a scene with a missing asset or a model that will not load can still
        be generated, it just renders with less in it.

        One copy per model, each holding **exactly one** focus object: a shot has one
        subject, and the file the render node opens then says which.
        """
        from . import focus as focus_objects
        from .project import FOCUS_REPORT, PACK_REPORT, pack_scene, place_focus_models
        from ..io.path_utils import parse_path_mappings
        from .scene_loader import SceneEntry

        layout = self.project_layout
        try:
            record = pack_scene(copy, logger=LOGGER,
                                report=os.path.join(layout.root, PACK_REPORT))
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            record = {"missing": [], "error": str(exc)}
        self.asset_packs.append(record)
        for item in (record.get("missing") or [])[:5]:
            LOGGER.warning("scene %s points at a file that no longer exists: %s",
                           os.path.basename(copy), item.get("path") or item)

        self.focus_models_list = focus_objects.models_from_section(
            getattr(self.config, "focus", None),
            mappings=parse_path_mappings(self.config.batch.path_mappings),
            logger=LOGGER,
        )
        if not self.focus_models_list:
            return
        variants = []
        for model in self.focus_models_list:
            try:
                variant_path = self.project_layout.stage_scene(copy, logger=LOGGER,
                                                               label=model.id)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                LOGGER.warning("the scene copy for focus object %r could not be staged: %s",
                               model.id, exc)
                continue
            placed = place_focus_models(
                variant_path, [model],
                anchor=focus_objects.anchor_from_section(self.config.focus),
                logger=LOGGER,
                report=os.path.join(layout.root, FOCUS_REPORT),
            )
            self.focus_placements.append(placed)
            if not placed.get("ok"):
                LOGGER.warning("focus object %r could not be placed in %s: %s", model.id,
                               os.path.basename(variant_path),
                               placed.get("error") or "unknown reason")
                continue
            variant = SceneEntry(path=variant_path)
            variant.original_path = copy
            variants.append((model, variant))
            LOGGER.info("focus object %r: %s", model.id, os.path.basename(variant_path))
        self.focus_variants[copy] = variants

    def _focus_variants(self, entry) -> list:
        """The ``(model, scene copy)`` pairs staged for this scene."""
        return list(self.focus_variants.get(entry.path, []))

    def _generate(self, request: SequenceRequest) -> None:
        self.stage = f"{request.motion_name} / {request.camera_name}"
        self.controller.set_stage(self.stage)
        # Each focus object has its own scene copy, so the file has to be switched when
        # the queue moves on to the next subject.  Opening a file wipes Blender's Python
        # timer registry, which is why the panel tick re-registers itself after every
        # step (see registration._generation_tick).
        if (request.scene_entry.path
                and os.path.normcase(request.scene_entry.path)
                != os.path.normcase(self._opened_scene or "")):
            switch = open_scene_for_generation(
                request.scene_entry,
                safe_copy=False,
                scratch_dir=os.path.join(self.project_layout.root, "_scratch")
                if self.project_layout else "",
            )
            if not switch.ok:
                result = SequenceResult(request=request, ok=False,
                                        error=f"cannot open the scene copy: {switch.error}")
                self.failures.append({
                    "sequence_id": request.sequence_folder,
                    "scene_name": request.scene_name,
                    "motion_name": request.motion_name,
                    "camera_name": request.camera_name,
                    "error": result.error,
                })
                self.failed += 1
                return
            self._opened_scene = request.scene_entry.path
            LOGGER.info("focus object %r: generating from %s", request.focus,
                        os.path.basename(request.scene_entry.path))
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

    # -- (sequence blends are not written any more) ----------------------
    # Sequences store the camera animation and the renderer replays it onto the
    # scene shipped beside them, so there is nothing to defer out of a timer: the
    # old "queue the .blend writes until the run ends" machinery went with the
    # scene-copy mode.

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
            "project_folder": self.project_layout.root if self.project_layout else "",
            "output_root": self.output_root,
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
    "is_running",
    "request_cancel",
    "reset",
    "snapshot",
    "state",
    "step",
    "task_controller",
]
