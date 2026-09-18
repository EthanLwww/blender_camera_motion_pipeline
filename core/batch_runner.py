"""Batch orchestration: scenes x motions x cameras x characters.

The runner never raises for a per-scene or per-sequence problem.  It records the
failure, keeps the batch alive, and writes a roll-up so a remote job can inspect
exactly what was produced and what was not.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..camera.motion_templates import MotionTemplateLibrary
from ..character import build_character_provider
from ..character.base_provider import (
    CharacterProvider,
    character_variants,
    summarise_variants,
)
from ..config.models import (
    CHARACTER_MODE_NONE,
    BatchConfig,
    validate_batch_config,
)
from ..io.json_io import save_json_file
from ..io.manifest import ManifestWriter, utc_now_iso
from ..io.path_utils import ensure_dir, normalize_path, parse_path_mappings, to_forward_slashes
from ..utils.logging_utils import get_logger
from ..utils.task_control import TaskCancelled, TaskController
from ..utils.version import GENERATOR_VERSION, generator_stamp
from . import blender_context as bctx
from .scene_loader import (
    SceneEntry,
    missing_scene_entries,
    open_scene_for_generation,
    scene_name_for,
)
from .sequence_generator import SequenceGenerator, SequenceResult


@dataclass
class SceneOutcome:
    """Everything that happened for one source ``.blend``."""

    entry: SceneEntry
    ok: bool = False
    skipped: bool = False
    cameras: "list[str]" = field(default_factory=list)
    motion_count: int = 0
    request_count: int = 0
    generated: int = 0
    failed: int = 0
    skipped_sequences: int = 0
    error: str = ""
    warnings: "list[str]" = field(default_factory=list)
    elapsed_seconds: float = 0.0
    load: dict = field(default_factory=dict)
    character_status: str = ""
    requests: "list[SequenceResult]" = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scene_name": scene_name_for(self.entry),
            "source_blend": to_forward_slashes(self.entry.path),
            "ok": bool(self.ok),
            "skipped": bool(self.skipped),
            "cameras": list(self.cameras),
            "camera_count": len(self.cameras),
            "motion_count": int(self.motion_count),
            "request_count": int(self.request_count),
            "generated": int(self.generated),
            "failed": int(self.failed),
            "skipped_sequences": int(self.skipped_sequences),
            "error": self.error,
            "warnings": list(self.warnings),
            "character_status": self.character_status,
            "elapsed_seconds": round(float(self.elapsed_seconds), 4),
            "load": dict(self.load),
            "sequences": [r.to_manifest_entry() for r in self.requests],
        }


@dataclass
class BatchReport:
    """Roll-up for the whole run."""

    started_utc: str = ""
    finished_utc: str = ""
    output_root: str = ""
    character_mode: str = CHARACTER_MODE_NONE
    template_source: str = ""
    template_names: "list[str]" = field(default_factory=list)
    provider: dict = field(default_factory=dict)
    camera_selection: str = "all"
    scenes: "list[SceneOutcome]" = field(default_factory=list)
    total_requests: int = 0
    generated: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: bool = False
    cancel_reason: str = ""
    elapsed_seconds: float = 0.0
    warnings: "list[str]" = field(default_factory=list)
    errors: "list[str]" = field(default_factory=list)
    report_path: str = ""
    config_path: str = ""

    @property
    def ok(self) -> bool:
        """True only when every scene and every sequence succeeded.

        ``errors`` is consulted too: :meth:`BatchRunner._write_outputs` appends a
        summarising error for a failed or cancelled run, so checking only
        ``failed``/``cancelled`` would let a run that lost whole scenes report
        success.
        """
        if self.failed or self.cancelled:
            return False
        if any(not scene.ok and not scene.skipped for scene in self.scenes):
            return False
        # Ignore the trailing summary entry this class adds itself.
        return not [e for e in self.errors if not e.startswith(("batch:", "run cancelled:"))]

    def to_dict(self) -> dict:
        return {
            "generator_version": GENERATOR_VERSION,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "output_root": to_forward_slashes(self.output_root),
            "character_mode": self.character_mode,
            "character_provider": dict(self.provider),
            "template_source": to_forward_slashes(self.template_source),
            "template_count": len(self.template_names),
            "template_names": list(self.template_names),
            "camera_selection": self.camera_selection,
            "totals": {
                "scenes": len(self.scenes),
                "requested": int(self.total_requests),
                "generated": int(self.generated),
                "failed": int(self.failed),
                "skipped": int(self.skipped),
            },
            "cancelled": bool(self.cancelled),
            "cancel_reason": self.cancel_reason,
            "elapsed_seconds": round(float(self.elapsed_seconds), 4),
            "ok": bool(self.ok),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "report_path": to_forward_slashes(self.report_path),
            "config_path": to_forward_slashes(self.config_path),
            "scenes": [s.to_dict() for s in self.scenes],
        }

    def summary_text(self) -> str:
        lines = [
            f"batch {'OK' if self.ok else 'WITH PROBLEMS'} "
            f"in {self.elapsed_seconds:.1f}s",
            f"  output        : {self.output_root}",
            f"  character mode: {self.character_mode} ({self.provider.get('provider', '?')}"
            f"/{self.provider.get('status', '?')})",
            f"  templates     : {len(self.template_names)} from {self.template_source or '(embedded)'}",
            f"  scenes        : {len(self.scenes)}",
            f"  sequences     : {self.generated} generated, {self.failed} failed, {self.skipped} skipped",
        ]
        for scene in self.scenes:
            flag = "ok  " if scene.ok else ("skip" if scene.skipped else "FAIL")
            lines.append(
                f"  [{flag}] {os.path.basename(scene.entry.path)}: "
                f"{scene.generated} ok / {scene.failed} failed / {scene.skipped_sequences} skipped"
                + (f" -- {scene.error}" if scene.error else "")
            )
        if self.cancelled:
            lines.append(f"  CANCELLED: {self.cancel_reason}")
        for warning in self.warnings:
            lines.append(f"  WARNING: {warning}")
        for error in self.errors:
            lines.append(f"  ERROR: {error}")
        return "\n".join(lines)


class BatchRunner:
    """Drive the whole generation pipeline."""

    def __init__(
        self,
        config: BatchConfig,
        *,
        output_root: str = "",
        scene_entries=None,
        logger=None,
        task: TaskController | None = None,
        provider: CharacterProvider | None = None,
        camera_selection: str = "all",
    ):
        self.config = config
        self.output_root = normalize_path(output_root or config.batch.output_root)
        self.logger = logger or get_logger("batch_runner")
        self.task = task or TaskController(name="batch")
        self.entries: "list[SceneEntry]" = list(scene_entries or [])
        self.camera_selection = camera_selection
        self._provider = provider

    # -- setup -----------------------------------------------------------
    def preflight(self) -> "list[str]":
        """Return configuration problems that would make the run meaningless."""
        problems = validate_batch_config(self.config, require_output=True)
        if not self.entries:
            problems.append("no scenes are queued")
        missing = missing_scene_entries(self.entries)
        for entry in missing:
            problems.append(f"scene file does not exist: {entry.path}")
        try:
            ensure_dir(self.output_root)
        except OSError as exc:
            problems.append(f"output folder is not writable: {self.output_root} ({exc})")
        return problems

    def build_provider(self) -> CharacterProvider:
        if self._provider is not None:
            return self._provider
        mappings = parse_path_mappings(self.config.batch.path_mappings)
        self._provider = build_character_provider(
            self.config.batch.character_provider,
            asset_root=self.config.batch.character_asset_root,
            animation_root=self.config.batch.animation_asset_root,
            config=self.config.batch,
            mappings=mappings,
            logger=self.logger,
        )
        return self._provider

    def load_library(self) -> MotionTemplateLibrary:
        library = MotionTemplateLibrary.from_config(self.config.motion, logger=self.logger)
        self.logger.info(
            "motion templates: %d loaded from %s", len(library), library.source
        )
        return library

    # -- run -------------------------------------------------------------
    def run(self, *, library: MotionTemplateLibrary | None = None) -> BatchReport:
        started = time.time()
        report = BatchReport(
            started_utc=utc_now_iso(),
            output_root=self.output_root,
            character_mode=self.config.batch.mode,
            camera_selection=self.camera_selection,
        )
        try:
            library = library or self.load_library()
        except Exception as exc:
            report.errors.append(f"motion templates could not be loaded: {exc}")
            report.finished_utc = utc_now_iso()
            report.elapsed_seconds = time.time() - started
            self.logger.error("motion templates could not be loaded: %s", exc)
            return report

        report.template_source = library.source
        report.template_names = library.names
        report.warnings.extend(library.warnings)

        provider = self.build_provider()
        report.provider = provider.availability()
        self.logger.info(provider.describe_plan(self.config.batch.mode))

        variants = character_variants(self.config.batch.mode, provider, logger=self.logger)
        self.logger.info("character variants: %s", summarise_variants(variants))

        self.task.set_total(len(self.entries), stage="scenes")
        for entry in self.entries:
            if self.task.cancelled:
                report.cancelled = True
                report.cancel_reason = self.task.reason
                break
            outcome = self._run_scene(entry, library, provider, variants)
            report.scenes.append(outcome)
            report.total_requests += outcome.request_count
            report.generated += outcome.generated
            report.failed += outcome.failed
            report.skipped += outcome.skipped_sequences
            self.task.tick(1, stage=f"scene {os.path.basename(entry.path)}")

        report.cancelled = report.cancelled or self.task.cancelled
        report.cancel_reason = report.cancel_reason or (self.task.reason if report.cancelled else "")
        report.finished_utc = utc_now_iso()
        report.elapsed_seconds = time.time() - started
        self._write_outputs(report)
        return report

    # -- one scene -------------------------------------------------------
    def _run_scene(self, entry: SceneEntry, library, provider, variants) -> SceneOutcome:
        import bpy

        started = time.time()
        outcome = SceneOutcome(entry=entry)
        scene_name = scene_name_for(entry, self.config.batch.scene_name_mode)
        self.logger.info("=== scene %s (%s) ===", scene_name, entry.path)

        if not entry.enabled:
            outcome.ok = True
            outcome.skipped = True
            outcome.error = "scene is disabled in the list"
            outcome.elapsed_seconds = time.time() - started
            return outcome

        # Scene output already complete?  Handled per sequence below, but bail
        # out early when nothing at all is left to do.
        load = open_scene_for_generation(
            entry,
            safe_copy=False,           # generation works on copies of datablocks, never the file
            scratch_dir=os.path.join(self.output_root, "_scratch"),
        )
        outcome.load = load.to_dict()
        outcome.warnings.extend(load.warnings)
        if not load.ok:
            outcome.error = load.error
            self.logger.error("cannot open %s: %s", entry.path, load.error)
            self._record_scene_failure(entry, scene_name, load.error)
            outcome.elapsed_seconds = time.time() - started
            return outcome

        scene = bpy.context.scene
        camera_objects = bctx.list_camera_objects(scene)
        outcome.cameras = [obj.name for obj in camera_objects]
        outcome.motion_count = len(library)
        if not camera_objects:
            outcome.error = (
                f"scene {scene.name!r} contains no camera; nothing can be generated. "
                "Add a camera or remove this scene from the list."
            )
            self.logger.error("%s", outcome.error)
            self._record_scene_failure(entry, scene_name, outcome.error)
            outcome.elapsed_seconds = time.time() - started
            return outcome

        selected = self._select_cameras(camera_objects)
        if not selected:
            outcome.error = f"camera selection {self.camera_selection!r} matched no camera"
            self._record_scene_failure(entry, scene_name, outcome.error)
            outcome.elapsed_seconds = time.time() - started
            return outcome

        generator = SequenceGenerator(
            self.config,
            output_root=self.output_root,
            character_provider=provider,
            logger=self.logger,
            task=self.task,
        )
        requests = generator.build_requests(
            entry,
            library=library,
            cameras=[obj.name for obj in selected],
            character_variants=variants,
        )
        outcome.request_count = len(requests)
        self.logger.info(
            "scene %s: %d camera(s) x %d motion(s) x %d character variant(s) = %d sequence(s)",
            scene_name, len(selected), len(library), len(variants), len(requests),
        )

        outcome.character_status = provider.status()
        for request in requests:
            if self.task.cancelled:
                break
            try:
                result = generator.generate(request)
            except TaskCancelled:
                break
            except Exception as exc:  # pragma: no cover - generator already guards
                self.logger.error("sequence %s raised: %s", request.sequence_folder, exc, exc_info=True)
                outcome.failed += 1
                continue
            outcome.requests.append(result)
            if result.ok and result.skipped:
                outcome.skipped_sequences += 1
            elif result.ok:
                outcome.generated += 1
            else:
                outcome.failed += 1

        outcome.ok = outcome.failed == 0 and not self.task.cancelled
        outcome.elapsed_seconds = time.time() - started
        self.logger.info(
            "scene %s done: %d generated, %d failed, %d skipped in %.1fs",
            scene_name, outcome.generated, outcome.failed, outcome.skipped_sequences,
            outcome.elapsed_seconds,
        )
        return outcome

    def _select_cameras(self, camera_objects):
        spec = (self.camera_selection or "all").strip()
        if not spec or spec.lower() == "all":
            return list(camera_objects)
        wanted = [name.strip() for name in spec.split(",") if name.strip()]
        if not wanted:
            return list(camera_objects)
        index_spec = all(item.lstrip("-").isdigit() for item in wanted)
        if index_spec:
            selected = []
            for item in wanted:
                index = int(item)
                if 0 <= index < len(camera_objects):
                    selected.append(camera_objects[index])
                else:
                    self.logger.warning(
                        "camera index %d is out of range (scene has %d camera(s))",
                        index, len(camera_objects),
                    )
            return selected
        by_name = {obj.name: obj for obj in camera_objects}
        selected = []
        for name in wanted:
            if name in by_name:
                selected.append(by_name[name])
            else:
                self.logger.warning("camera %r is not present in the loaded scene", name)
        return selected

    # -- reporting -------------------------------------------------------
    def _record_scene_failure(self, entry: SceneEntry, scene_name: str, error: str) -> None:
        if self.task is not None:
            self.task.note_error()
        try:
            writer = ManifestWriter(
                os.path.join(self.output_root, scene_name, "manifest.json"),
                kind="scene",
                scope={"scene_name": scene_name, "source_blend": to_forward_slashes(entry.path)},
            )
            writer.add_failure({
                "scene_name": scene_name,
                "source_blend": to_forward_slashes(entry.path),
                "error": error,
                "recorded_utc": utc_now_iso(),
            })
            writer.flush()
        except Exception as exc:
            self.logger.warning("could not record the scene failure in a manifest: %s", exc)

    def _write_outputs(self, report: BatchReport) -> None:
        try:
            ensure_dir(self.output_root)
        except OSError as exc:
            report.errors.append(f"cannot create the output folder: {exc}")
            return
        # Effective configuration, so a render node can reproduce the run.
        config_payload = self.config.to_dict()
        config_payload["_effective"] = {
            "output_root": to_forward_slashes(self.output_root),
            "camera_selection": self.camera_selection,
            "template_source": to_forward_slashes(report.template_source),
            "template_names": list(report.template_names),
            "character_provider": dict(report.provider),
            **generator_stamp(),
        }
        try:
            report.config_path = save_json_file(
                os.path.join(self.output_root, "batch_config.json"), config_payload
            )
        except Exception as exc:
            report.warnings.append(f"could not write batch_config.json: {exc}")

        # Root manifest: every generated sequence plus every failure.
        try:
            writer = ManifestWriter(
                os.path.join(self.output_root, "manifest.json"),
                kind="batch",
                scope={
                    "output_root": to_forward_slashes(self.output_root),
                    "character_mode": self.config.batch.mode,
                },
            )
            for scene in report.scenes:
                for result in scene.requests:
                    if result.ok and not result.skipped:
                        writer.add_sequence(result.to_manifest_entry())
                for result in scene.requests:
                    if not result.ok:
                        writer.add_failure(result.to_manifest_entry())
                if scene.error and not scene.requests:
                    writer.add_failure({
                        "scene_name": scene_name_for(scene.entry),
                        "source_blend": to_forward_slashes(scene.entry.path),
                        "error": scene.error,
                    })
            writer.flush(extra={"totals": report.to_dict()["totals"], "cancelled": report.cancelled})
        except Exception as exc:
            report.warnings.append(f"could not write the root manifest: {exc}")

        try:
            report.report_path = save_json_file(
                os.path.join(self.output_root, "batch_report.json"), report.to_dict()
            )
        except Exception as exc:
            report.warnings.append(f"could not write batch_report.json: {exc}")

        if not report.ok:
            failed_scenes = [s for s in report.scenes if not s.ok and not s.skipped]
            if report.failed or failed_scenes:
                report.errors.append(
                    f"{report.failed} sequence(s) and {len(failed_scenes)} scene(s) failed; "
                    "see the per-sequence failure_report.json files and batch_report.json"
                )
        if report.cancelled:
            report.errors.append(f"run cancelled: {report.cancel_reason}")

    # -- static helpers --------------------------------------------------
    @staticmethod
    def inspect_output_tree(output_root: str) -> dict:
        """Summarise what already exists under ``output_root``."""
        from .sequence_manager import SequenceManager

        return SequenceManager(output_root).summary()


def run_batch(
    config: BatchConfig,
    entries,
    *,
    output_root: str = "",
    logger=None,
    progress=None,
    camera_selection: str = "all",
) -> BatchReport:
    """Convenience wrapper used by the CLI and the add-on operator."""
    task = TaskController(name="batch", progress_callback=progress)
    runner = BatchRunner(
        config,
        output_root=output_root,
        scene_entries=entries,
        logger=logger,
        task=task,
        camera_selection=camera_selection,
    )
    return runner.run()


def describe_report(report: BatchReport) -> str:
    return report.summary_text()
