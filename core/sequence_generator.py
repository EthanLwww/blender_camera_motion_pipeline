"""Per-(camera, motion, character) sequence generation.

For every combination the generator:

1. rebuilds a clean scene context (original camera state + geometry);
2. builds the candidate animation from the motion template;
3. validates it -- including the spherical camera search when validation fails;
4. bakes the winner into the scene as per-frame keyframes on a *copy* of the
   camera data block, so the artist's original camera animation survives;
5. writes ``sequence.blend``, ``sequence_config.json``, the JSON/TXT camera
   artifacts, a validation report and a generation log;
6. restores the scene so the next combination starts from a pristine state.

A failure in any step is recorded and the loop continues: one bad template must
never cost the rest of the batch.
"""

from __future__ import annotations

import copy
import math
import os
import time
import traceback
from dataclasses import dataclass, field
from typing import Sequence

from ..camera.camera_export import (
    SequenceMetadata,
    build_trajectory_rows,
    camera_intrinsics,
    write_trajectory_txt,
)
from ..camera.camera_search import CameraSearch, SearchResult, apply_candidate
from ..camera.camera_validator import CameraValidator, ValidationReport, validate_camera_static
from ..camera.motion_templates import (
    MotionAnimation,
    MotionTemplate,
    MotionTemplateGenerator,
    TemplateUnitScale,
    matrix_to_quaternion,
    quaternion_to_matrix,
)
from ..camera.scene_context import CameraSnapshot, CharacterBox
from ..character.base_provider import (
    CharacterDescriptor,
    CharacterPlacement,
    CharacterProvider,
)
from ..config.models import (
    BatchConfig,
)
from ..io.json_io import save_json_file
from ..io.manifest import ManifestWriter, utc_now_iso
from ..io.path_utils import ensure_dir, normalize_path, relative_to, sanitize_relpath, to_forward_slashes
from ..utils.logging_utils import RunLogger, get_logger
from ..utils.animation import set_interpolation
from ..utils.task_control import TaskController
from ..utils.version import GENERATOR_VERSION, generator_stamp
from . import blender_context as bctx
from .camera_animation import PAYLOAD_KEY, build_payload, payload_summary, sample_to_dict
from .scene_loader import SceneEntry, scene_name_for


# --------------------------------------------------------------------------
# small row-major 4x4 helpers (pure python: usable without ``bpy``, testable)
# --------------------------------------------------------------------------
def _identity_4x4() -> "list[list[float]]":
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> "list[list[float]]":
    return [
        [sum(float(a[i][k]) * float(b[k][j]) for k in range(4)) for j in range(4)]
        for i in range(4)
    ]


def _invert_4x4(matrix: Sequence[Sequence[float]]):
    """Gauss-Jordan inverse of a row-major 4x4; ``None`` when singular."""
    rows = [[float(value) for value in row] for row in matrix]
    inverse = _identity_4x4()
    for column in range(4):
        pivot_row = max(range(column, 4), key=lambda r: abs(rows[r][column]))
        if abs(rows[pivot_row][column]) < 1e-12:
            return None
        if pivot_row != column:
            rows[column], rows[pivot_row] = rows[pivot_row], rows[column]
            inverse[column], inverse[pivot_row] = inverse[pivot_row], inverse[column]
        pivot = rows[column][column]
        rows[column] = [value / pivot for value in rows[column]]
        inverse[column] = [value / pivot for value in inverse[column]]
        for row in range(4):
            if row == column:
                continue
            factor = rows[row][column]
            if factor == 0.0:
                continue
            rows[row] = [value - factor * other for value, other in zip(rows[row], rows[column])]
            inverse[row] = [
                value - factor * other for value, other in zip(inverse[row], inverse[column])
            ]
    return inverse


def _matrix_from_pose(position: Sequence[float], quaternion: Sequence[float]) -> "list[list[float]]":
    """World-space homogeneous transform from a position and a (w, x, y, z) quaternion."""
    rotation = quaternion_to_matrix(quaternion)
    return [
        [float(rotation[0][0]), float(rotation[0][1]), float(rotation[0][2]), float(position[0])],
        [float(rotation[1][0]), float(rotation[1][1]), float(rotation[1][2]), float(position[1])],
        [float(rotation[2][0]), float(rotation[2][1]), float(rotation[2][2]), float(position[2])],
        [0.0, 0.0, 0.0, 1.0],
    ]


def assert_object_parenting(camera_obj) -> None:
    """Reject parenting modes the world-to-local bake cannot reproduce.

    ``local_basis = inverse(matrix_parent_inverse) @ inverse(parent_world) @
    world`` holds for object parenting.  Bone and vertex parenting insert an extra
    space matrix (the bone's pose matrix, the vertex weights) that this bake does
    not model, so they fail loudly rather than producing a path that ends up in
    the video, the trajectory and a validator PASS while being wrong.
    """
    parent = camera_obj.parent
    if parent is None or str(camera_obj.parent_type) == "OBJECT":
        return
    raise RuntimeError(
        f"camera {camera_obj.name!r} is {camera_obj.parent_type}-parented to "
        f"{parent.name!r}; sequence generation supports object parenting only. "
        "Clear the parent, or parent the camera to an empty that the rig drives."
    )


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
@dataclass
class SequenceRequest:
    """Everything that identifies one output sequence."""

    scene_entry: SceneEntry
    scene_name: str
    motion_name: str
    template: MotionTemplate
    camera_name: str
    has_character: bool
    character: "CharacterDescriptor | None" = None
    animation: object | None = None
    character_note: str = ""
    index: int = 0

    @property
    def motion_folder(self) -> str:
        return sanitize_relpath(self.motion_name) or "motion"

    @property
    def sequence_folder(self) -> str:
        return f"sequence_{self.index:06d}"

    def output_dir(self, output_root: str) -> str:
        return os.path.join(
            normalize_path(output_root), self.scene_name, self.motion_folder, self.sequence_folder
        )

    def plain_id(self) -> str:
        return self.sequence_folder

    def key(self) -> str:
        return "|".join([
            self.scene_name, self.motion_name, self.camera_name,
            (self.character.id if self.character else "-"),
            (getattr(self.animation, "id", "") or "-"),
            "char" if self.has_character else "nochar",
        ])


@dataclass
class SequenceResult:
    """Outcome of generating one sequence."""

    request: SequenceRequest
    ok: bool = False
    sequence_id: str = ""
    output_dir: str = ""
    files: "dict[str, str]" = field(default_factory=dict)
    validation: "ValidationReport | None" = None
    search: "SearchResult | None" = None
    animation: "MotionAnimation | None" = None
    error: str = ""
    messages: "list[str]" = field(default_factory=list)
    skipped: bool = False
    elapsed_seconds: float = 0.0
    camera_used: str = ""
    character_status: str = ""

    def to_manifest_entry(self) -> dict:
        from ..io.manifest import ManifestWriter  # noqa: F401  (typing only)

        entry = {
            "sequence_id": self.sequence_id,
            "motion_name": self.request.motion_name,
            "scene_name": self.request.scene_name,
            "camera_name": self.camera_used or self.request.camera_name,
            "has_character": bool(self.request.has_character),
            "character_name": self.request.character.id if self.request.character else "",
            "character_animation": getattr(self.request.animation, "id", "") or "",
            "character_status": self.character_status,
            "source_blend": to_forward_slashes(self.request.scene_entry.path),
            "status": "ok" if self.ok else ("skipped" if self.skipped else "failed"),
            "error": self.error,
            "validation_passed": bool(self.validation.passed) if self.validation else False,
            "validation_score": round(float(self.validation.score), 6) if self.validation else 0.0,
            "search_attempts": self.search.attempts if self.search else 0,
            "search_used": bool(self.search and self.search.passed),
            "frame_start": self.animation.frame_start if self.animation else None,
            "frame_end": self.animation.frame_end if self.animation else None,
            "frame_count": self.animation.frame_count if self.animation else 0,
            "elapsed_seconds": round(float(self.elapsed_seconds), 4),
            "files": {key: to_forward_slashes(value) for key, value in self.files.items()},
        }
        if self.validation:
            entry["validation_reasons"] = self.validation.failures
        return entry


# --------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------
class SequenceGenerator:
    """Generate sequence artifacts for one loaded scene."""

    def __init__(
        self,
        config: BatchConfig,
        *,
        output_root: str,
        character_provider: "CharacterProvider | None" = None,
        logger=None,
        task: "TaskController | None" = None,
    ):
        self.config = config
        self.output_root = normalize_path(output_root)
        self.provider = character_provider
        self.logger = logger or get_logger("sequence_generator")
        self.task = task
        self.motion = config.motion
        self.validation_config = config.validation
        self.search_config = config.search
        self.unit_scale: TemplateUnitScale = config.motion.unit_scale
        self.results: "list[SequenceResult]" = []
        self.notes: "list[str]" = []
        #: When True, ``sequence.blend`` writes are queued instead of executed.
        #: Required for timer-driven (non-blocking) panel runs; see
        #: :meth:`save_sequence_blend`.
        self.defer_blend_save: bool = False
        #: Absolute paths of sequence blends waiting to be written.
        self.pending_blend_saves: "list[str]" = []

    # -- helpers ---------------------------------------------------------
    def _tick(self, *, stage: str = "", step: int = 1) -> None:
        if self.task is not None:
            if stage:
                self.task.set_stage(stage)
            self.task.tick(step)
            self.task.check()

    def _generator(self) -> MotionTemplateGenerator:
        return MotionTemplateGenerator(
            unit_scale=self.unit_scale,
            frame_start=self.motion.frame_start,
            frame_scale=self.motion.frame_scale,
            interpolation=self.motion.interpolation,
        )

    @staticmethod
    def camera_object(name: str):
        import bpy

        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "CAMERA":
            raise KeyError(f"camera object {name!r} is not present in the loaded file")
        return obj

    # -- public API ------------------------------------------------------
    def build_requests(
        self,
        scene_entry: SceneEntry,
        *,
        library,
        cameras: Sequence[str],
        character_variants: Sequence[tuple],
        start_index: int = 1,
    ) -> "list[SequenceRequest]":
        """Expand the scene x motion x camera x character matrix.

        Sequence numbers restart at 1 for **each motion folder**, so a
        ``scene/motion/`` directory is self-contained: its manifest, its
        sequence ids and its numbering all agree, and a partial re-run of one
        motion never renumbers another motion's sequences.
        """
        from ..io.path_utils import safe_filename

        scene_name = scene_name_for(scene_entry, self.config.batch.scene_name_mode)
        requests: "list[SequenceRequest]" = []
        del start_index  # numbering is per motion folder, not global
        for template in library:
            motion_name = safe_filename(template.name, fallback="motion")
            index = 1
            for camera_name in cameras:
                for has_character, character, animation, note in character_variants:
                    requests.append(SequenceRequest(
                        scene_entry=scene_entry,
                        scene_name=scene_name,
                        motion_name=motion_name,
                        template=template,
                        camera_name=camera_name,
                        has_character=bool(has_character),
                        character=character,
                        animation=animation,
                        character_note=note,
                        index=index,
                    ))
                    index += 1
        return requests

    def generate(self, request: SequenceRequest) -> SequenceResult:
        """Generate (or skip) one sequence."""
        started = time.time()
        result = SequenceResult(request=request)
        output_dir = request.output_dir(self.output_root)
        result.output_dir = output_dir
        result.sequence_id = request.plain_id()

        run_log = RunLogger(echo=self.logger)
        run_log.log(f"sequence {request.sequence_folder} | scene={request.scene_name} "
                    f"motion={request.motion_name} camera={request.camera_name} "
                    f"character={request.character.id if request.character else '-'}")

        try:
            self._generate_inner(request, result, output_dir, run_log)
        except Exception as exc:  # one bad combination must not stop the batch
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
            run_log.log(result.error, level="ERROR")
            run_log.log(traceback.format_exc(), level="ERROR")
            self.logger.error(
                "sequence %s failed: %s", request.sequence_folder, result.error, exc_info=True
            )
            if self.task is not None:
                self.task.note_error()
            try:
                self._write_log(run_log, output_dir)
            except Exception:
                pass
        finally:
            result.elapsed_seconds = time.time() - started
            result.messages = list(run_log.lines)
            self.results.append(result)
        return result

    # -- implementation --------------------------------------------------
    def _generate_inner(self, request, result, output_dir, run_log) -> None:
        import bpy

        # -------- incremental re-run support -----------------------------
        planned_files = self._planned_files(output_dir, request)
        if self._can_skip(planned_files, run_log):
            result.ok = True
            result.skipped = True
            result.files = planned_files
            result.camera_used = request.camera_name
            run_log.log("existing artifacts found and batch.overwrite/resume allow reuse: skipped", level="WARNING")
            self._write_log(run_log, output_dir)
            return

        ensure_dir(output_dir)
        scene = bpy.context.scene
        self._tick(stage=f"{request.motion_name}/{request.camera_name}")

        # -------- 0. deterministic anchor frame --------------------------
        # The template motion is applied to the camera's pose at the sequence's
        # *first* frame, so that frame has to be the same for every sequence in a
        # batch.  Left alone it is whatever the artist saved (frame 1 in the
        # reference scene) for the first sequence and the previous sequence's last
        # frame for the rest, which makes a template's result depend on batch
        # position -- measured on the reference scene as a 0.2 m anchor shift.
        anchor_frame = self.motion.frame_start
        if anchor_frame is None:
            anchor_frame = scene.frame_start
        try:
            scene.frame_set(int(anchor_frame))
        except Exception as exc:
            run_log.log(f"could not set the anchor frame to {anchor_frame}: {exc}", level="WARNING")

        # -------- 1. character (optional, isolated) ----------------------
        character_box: "CharacterBox | None" = None
        character_placement: "CharacterPlacement | None" = None
        if request.has_character and request.character is not None:
            character_box, character_placement, status = self._place_character(request, run_log)
            result.character_status = status
        elif request.has_character:
            result.character_status = "requested but no character descriptor was resolved"
            run_log.log(result.character_status, level="WARNING")
        else:
            result.character_status = "no character in this sequence"

        # -------- 2. camera + original state ----------------------------
        camera_obj = self.camera_object(request.camera_name)
        original = bctx.camera_snapshot(camera_obj, scene)
        static_problems = validate_camera_static(original, self.validation_config)
        for problem in static_problems:
            run_log.log(f"camera static check: {problem}", level="WARNING")

        exclude = list(character_placement.imported_objects) if character_placement else []
        context = bctx.build_scene_context(
            scene=scene,
            exclude_objects=exclude,
            characters=[character_box] if character_box else [],
            cameras=[original],
            blend_path=request.scene_entry.path,
            logger=self.logger,
        )

        # -------- 3. animation + validation + search --------------------
        generator = self._generator()
        base_matrix = original.matrix_world
        base_position = original.location
        from ..camera.motion_templates import matrix_to_quaternion

        base_quaternion = matrix_to_quaternion(base_matrix)

        def make_animation_for(position, quaternion=None):
            matrix = copy.deepcopy(base_matrix)
            matrix[0][3] = float(position[0])
            matrix[1][3] = float(position[1])
            matrix[2][3] = float(position[2])
            return generator.generate(
                request.template,
                base_matrix=matrix,
                base_focal=original.lens,
                base_quaternion=quaternion if quaternion is not None else base_quaternion,
                frame_start=self.motion.frame_start if self.motion.frame_end is not None else None,
                frame_end=self.motion.frame_end,
            )

        animation = make_animation_for(base_position)
        report: "ValidationReport | None" = None
        search_result: "SearchResult | None" = None

        if self.validation_config.enabled and context.ray_caster is not None:
            validator = CameraValidator(context, self.validation_config, logger=self.logger)
            report = validator.validate(
                original,
                animation,
                character=character_box,
                base_matrix=base_matrix,
                base_focal=original.lens,
            )
            run_log.log(f"validation (original camera): {report.summary_line()}")
            for message in report.messages:
                run_log.log(f"  {message}", level="WARNING")

            if not report.passed and self.search_config.enabled:
                run_log.log(
                    f"original camera failed; searching {self.search_config.candidate_count} "
                    f"candidate position(s) in radius "
                    f"[{self.search_config.min_radius}, {self.search_config.max_radius}]"
                )
                search = CameraSearch(context, self.search_config, validator, logger=self.logger)

                def make_animation(candidate):
                    return make_animation_for(candidate.position)

                search_result = search.search(
                    original,
                    make_animation,
                    base_position=base_position,
                    base_quaternion=base_quaternion,
                    base_focal=original.lens,
                    character=character_box,
                    base_matrix=base_matrix,
                    original_report=report,
                )
                for message in search_result.messages:
                    run_log.log(f"  search: {message}")
                if search_result.passed and search_result.accepted:
                    winner = search_result.accepted[0]
                    best = next(
                        (e for e in search_result.evaluations if e.candidate is winner), None
                    )
                    animation = apply_candidate(make_animation_for(winner.position), winner)
                    if best is not None:
                        report = best.report
                    run_log.log(f"camera search accepted: {winner.describe()}")
                else:
                    run_log.log(
                        "camera search found no acceptable candidate; recording the failure "
                        "instead of emitting a knowingly bad sequence",
                        level="ERROR",
                    )
            elif not report.passed:
                run_log.log(
                    "validation failed and the camera search is disabled "
                    "(search.enabled = false); recording the failure rather than emitting a "
                    "knowingly bad sequence",
                    level="ERROR",
                )

            # ---------------------------------------------------------------
            # Emission gate: a failing validation must never produce a
            # "successful" sequence.  This is deliberately *outside* the search
            # branch -- an earlier version nested it inside, which silently let
            # an unvalidated sequence through whenever the search was disabled.
            # ---------------------------------------------------------------
            if not report.passed:
                result.search = search_result
                result.validation = report
                result.animation = animation
                result.error = "validation_failed: " + ", ".join(report.failures)
                result.ok = False
                if self.task is not None:
                    self.task.note_error()
                self._write_failure_artifacts(
                    request, result, output_dir, run_log, report, animation, search_result
                )
                return
        elif self.validation_config.enabled:
            run_log.log(
                "geometry validation skipped: no ray caster is available for this scene",
                level="WARNING",
            )
        else:
            run_log.log("geometry validation disabled by configuration")

        result.validation = report
        result.search = search_result
        result.animation = animation

        # -------- 4. bake keyframes -------------------------------------
        camera_data_copy, animation_payload = self._apply_animation(camera_obj, animation, run_log)
        result.camera_used = camera_obj.name

        # -------- 5. persist artifacts ----------------------------------
        sequence_id = request.sequence_folder
        files = self._write_artifacts(
            request, result, output_dir, run_log,
            camera=original, animation=animation, report=report,
            search_result=search_result, scene=scene,
            character_placement=character_placement, character_status=result.character_status,
            animation_payload=animation_payload,
        )
        result.files = files

        # -------- 6. restore --------------------------------------------
        self._restore(camera_obj, original, camera_data_copy, run_log)
        result.ok = True
        run_log.log(f"sequence {sequence_id} completed in {result.elapsed_seconds:.2f}s")
        self._write_log(run_log, output_dir)

    # -- step helpers ----------------------------------------------------
    def _planned_files(self, output_dir: str, request: SequenceRequest) -> "dict[str, str]":
        sequence_id = request.sequence_folder
        files = {
            "sequence_blend": os.path.join(output_dir, f"{sequence_id}.blend"),
            "sequence_config": os.path.join(output_dir, "sequence_config.json"),
            "metadata": os.path.join(output_dir, f"{sequence_id}.json"),
            "camera_trajectory": os.path.join(output_dir, f"{sequence_id}_camera.txt"),
            "generation_log": os.path.join(output_dir, "generation_log.txt"),
        }
        if self.config.batch.save_validation_report:
            files["validation_report"] = os.path.join(output_dir, "validation_report.json")
        return files

    def _can_skip(self, planned: "dict[str, str]", run_log: RunLogger) -> bool:
        if self.config.batch.overwrite or not self.config.batch.resume:
            return False
        required = ["sequence_config", "metadata", "camera_trajectory"]
        if self.config.batch.save_sequence_blend:
            # The blend is the deliverable the renderer consumes, so its absence
            # means the sequence must be regenerated even when the rest exists.
            required.append("sequence_blend")
        return all(os.path.isfile(planned[key]) for key in required if key in planned)

    def _place_character(self, request: SequenceRequest, run_log: RunLogger):
        """Import/place/animate the character, returning a box when successful."""
        assert self.provider is not None
        provider = self.provider
        if not provider.is_usable():
            message = (
                f"character sequence requested but provider {provider.name!r} is "
                f"{provider.status()}; generating the character-free variant instead"
            )
            run_log.log(message, level="WARNING")
            return None, None, f"unavailable: {provider.status()}"

        scene_context = bctx.build_scene_context(
            scene=None, ray_caster="bpy", logger=self.logger
        )
        placement = provider.import_character(scene_context, request.character)
        if not placement.ok:
            run_log.log(
                f"character import failed: {'; '.join(placement.errors or placement.messages) or 'unknown reason'}",
                level="ERROR",
            )
            return None, placement, f"import_failed: {placement.status}"
        for message in placement.messages:
            run_log.log(f"character: {message}")
        placement = provider.place_character(placement, scene_context)
        if request.animation is not None:
            placement = provider.apply_animation(placement, request.animation)
            for message in placement.messages[-2:]:
                run_log.log(f"character animation: {message}")
        validation = provider.validate_character_placement(placement, scene_context)
        for message in validation.messages:
            run_log.log(
                f"character placement: {message}",
                level="WARNING" if validation.valid else "ERROR",
            )
        run_log.log(
            f"character {placement.descriptor_id} placed as {placement.object_name!r} "
            f"(status={placement.status}, method={placement.placement_method or 'n/a'})"
        )
        if not placement.ok:
            return None, placement, f"placement_failed: {placement.status}"

        # Rebuild the box from the *current* geometry so validation sees the
        # character where it actually ended up.
        box = placement.to_character_box()
        if box is not None:
            refreshed = self._measure_character(placement)
            if refreshed is not None:
                box = refreshed
        return box, placement, placement.status

    def _measure_character(self, placement: CharacterPlacement) -> "CharacterBox | None":
        import bpy

        root = bpy.data.objects.get(placement.object_name)
        if root is None:
            return None
        lo = [math.inf] * 3
        hi = [-math.inf] * 3
        found = False
        from ..character.blender_provider import _descendants

        for obj in [root, *_descendants(root)]:
            if obj.type != "MESH":
                continue
            snapshot = bctx.mesh_snapshot_safe(obj)
            if snapshot is None:
                continue
            for axis in range(3):
                lo[axis] = min(lo[axis], snapshot.bbox_min[axis])
                hi[axis] = max(hi[axis], snapshot.bbox_max[axis])
            found = True
        if not found:
            return placement.to_character_box()
        return CharacterBox(
            name=placement.descriptor_id or placement.object_name,
            bbox_min=tuple(lo),  # type: ignore[arg-type]
            bbox_max=tuple(hi),  # type: ignore[arg-type]
            object_name=placement.object_name,
            animation=placement.animation,
        )

    def _apply_animation(self, camera_obj, animation: MotionAnimation, run_log: RunLogger):
        """Key every validated frame onto the camera; returns the data block copy.

        The generator produces **world-space** poses (that is what the validator
        checked, and what the trajectory records).  ``obj.location`` however is
        expressed in the object's *parent* space, so a parented camera needs the
        pose converted before it is keyed.  Writing world coordinates straight
        into ``location`` double-counts the parent's transform -- on the reference
        scene (camera parented to an animated train) that displaced the rendered
        camera by 26 m at frame 0 and 110 m by frame 80, and it silently discarded
        the camera's own ride.

        Constraints with a non-zero influence are muted for the bake and left
        muted, because they would otherwise overwrite the keyed rotation at
        evaluation time and the rendered motion would not match the validated one.
        """
        import bpy

        assert_object_parenting(camera_obj)

        scene = bpy.context.scene
        # Keep the artist's original animation intact: work on a copy of the
        # camera *data* block and clear animation on the new one only.
        data_copy = camera_obj.data.copy()
        data_copy.name = f"{camera_obj.data.name}_seq{animation.template_name}"[:63]
        camera_obj.data = data_copy
        bctx.clear_camera_animation(camera_obj)

        run_log.log(
            f"applying {animation.template_name}: frames {animation.frame_start}..{animation.frame_end} "
            f"({animation.frame_count} samples), interpolation={animation.interpolation}"
        )
        run_log.log(f"camera data block replaced with independent copy {data_copy.name!r}")

        muted: "list[str]" = []
        for constraint in camera_obj.constraints:
            if getattr(constraint, "mute", False):
                continue
            try:
                influence = float(getattr(constraint, "influence", 0.0))
            except Exception:
                influence = 1.0
            if influence > 1e-6:
                constraint.mute = True
                muted.append(f"{constraint.name}({constraint.type})")
        if muted:
            run_log.log(
                "muted camera constraint(s) for the bake, because they would "
                "override the generated rotation: " + ", ".join(muted),
                level="WARNING",
            )

        parent = camera_obj.parent
        conversion = self._local_bake_helper(camera_obj, parent)
        run_log.log(
            "camera is unparented: baking world poses directly"
            if parent is None else
            f"camera is parented to {parent.name!r} (type={camera_obj.parent_type}): "
            "converting each world pose into the parent's space before keying"
        )

        # Every keyed value is recorded as it is written, so a sequence can be
        # replayed onto the source scene without re-deriving anything (see
        # ``core/camera_animation.py``).
        keyed: "list[dict]" = []
        for sample in animation.samples:
            location, quaternion = conversion(sample)
            camera_obj.location = location
            camera_obj.rotation_mode = "QUATERNION"
            camera_obj.rotation_quaternion = quaternion
            data_copy.lens = max(1.0, float(sample.focal))
            camera_obj.keyframe_insert(data_path="location", frame=sample.frame, group="motion_pipeline")
            camera_obj.keyframe_insert(data_path="rotation_quaternion", frame=sample.frame, group="motion_pipeline")
            camera_obj.keyframe_insert(data_path="scale", frame=sample.frame, group="motion_pipeline")
            data_copy.keyframe_insert(data_path="lens", frame=sample.frame, group="motion_pipeline")
            keyed.append(sample_to_dict(
                sample.frame,
                camera_obj.location,
                camera_obj.rotation_quaternion,
                camera_obj.scale,
                data_copy.lens,
            ))

        for owner in (camera_obj, data_copy):
            animation_data = getattr(owner, "animation_data", None)
            if animation_data is None or animation_data.action is None:
                continue
            action = animation_data.action
            keys = set_interpolation(action, self._blender_interpolation())
            run_log.log(f"set {keys} keyframe(s) to {self._blender_interpolation()} in {action.name!r}")
            try:
                action.name = f"MP_{animation.template_name}_{camera_obj.name}"[:63]
            except Exception:
                pass

        scene.frame_start = int(animation.frame_start)
        scene.frame_end = int(animation.frame_end)
        scene.render.fps = int(round(animation.fps)) or scene.render.fps
        scene.render.fps_base = 1.0
        scene.frame_set(int(animation.frame_start))
        bpy.context.view_layer.update()
        return data_copy, build_payload(
            object_name=camera_obj.name,
            data_name=data_copy.name,
            rotation_mode=str(camera_obj.rotation_mode),
            interpolation=self._blender_interpolation(),
            samples=keyed,
            scene_name=bpy.context.scene.name,
            parent_name=getattr(parent, "name", "") or "",
            parent_type=str(camera_obj.parent_type),
            muted_constraints=[entry.split("(")[0] for entry in muted],
        )

    def _local_bake_helper(self, camera_obj, parent):
        """Return ``sample -> (local_location, local_quaternion)`` for keying.

        Blender evaluates a parented object as::

            world = parent_world @ matrix_parent_inverse @ local_basis

        so the local basis that reproduces a desired world pose is::

            local_basis = inverse(matrix_parent_inverse)
                        @ inverse(parent_world)
                        @ world_pose

        The parent's evaluated world matrix is read fresh per frame, because the
        parent's own animation is exactly what carries the camera through the
        scene.  Without a parent the pose is keyed as-is.
        """
        import bpy

        scene = bpy.context.scene
        parent_inverse_inverse = None
        if parent is not None:
            stored = [[float(v) for v in row] for row in camera_obj.matrix_parent_inverse]
            parent_inverse_inverse = _invert_4x4(stored)

        def convert(sample):
            if parent is None:
                return tuple(sample.position), tuple(sample.quaternion)
            scene.frame_set(int(sample.frame))
            depsgraph = bpy.context.evaluated_depsgraph_get()
            parent_world = [
                [float(v) for v in row]
                for row in parent.evaluated_get(depsgraph).matrix_world
            ]
            parent_world_inverse = _invert_4x4(parent_world)
            if parent_world_inverse is None:
                raise RuntimeError(
                    f"parent {parent.name!r} has a singular world matrix at frame {sample.frame}"
                )
            prefix = (
                parent_world_inverse
                if parent_inverse_inverse is None
                else _matmul(parent_inverse_inverse, parent_world_inverse)
            )
            local_matrix = _matmul(prefix, _matrix_from_pose(sample.position, sample.quaternion))
            return (
                (local_matrix[0][3], local_matrix[1][3], local_matrix[2][3]),
                matrix_to_quaternion(local_matrix),
            )

        return convert

    def _sequence_resolution(self, camera: CameraSnapshot) -> "tuple[int, int]":
        """The size this sequence is meant to be rendered at.

        ``render.resolution_explicit`` decides: when the user set a sequence
        resolution before generating, that wins and the renderer obeys it; otherwise
        the sequence follows the source scene (the historical behaviour, where a
        2000x2000 scene produced 2000x2000 videos no matter what the panel said).
        """
        render = self.config.render
        if render.resolution_explicit:
            factor = max(1, int(render.resolution_percentage)) / 100.0
            return (
                int(round(int(render.resolution_x) * factor)),
                int(round(int(render.resolution_y) * factor)),
            )
        return camera.effective_resolution

    def _blender_interpolation(self) -> str:
        mapping = {"LINEAR": "LINEAR", "BEZIER": "BEZIER", "CONSTANT": "CONSTANT"}
        return mapping.get(str(self.motion.interpolation).upper(), "BEZIER")

    def _restore(self, camera_obj, original: CameraSnapshot, data_copy, run_log: RunLogger) -> None:
        """Put the scene back so the next combination starts clean."""
        import bpy

        previous_data = camera_obj.data
        try:
            bctx.clear_camera_animation(camera_obj)
            bctx.restore_camera(camera_obj, original)
            if previous_data is not None and previous_data.users == 0:
                bpy.data.cameras.remove(previous_data)
        except Exception as exc:
            run_log.log(f"camera restore warning: {exc}", level="WARNING")
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
        del data_copy

    def _write_failure_artifacts(
        self, request, result, output_dir, run_log, report, animation, search_result
    ) -> None:
        """Persist whatever we learned about a failing combination."""
        ensure_dir(output_dir)
        payload = {
            "sequence_id": request.sequence_folder,
            "status": "failed",
            "error": result.error,
            "scene_name": request.scene_name,
            "motion_name": request.motion_name,
            "camera_name": request.camera_name,
            "has_character": bool(request.has_character),
            "character_name": request.character.id if request.character else "",
            "character_animation": getattr(request.animation, "id", "") or "",
            "source_blend": to_forward_slashes(request.scene_entry.path),
            "generator_version": GENERATOR_VERSION,
            "created_utc": utc_now_iso(),
            "validation": report.to_dict(config=self.validation_config, include_frames=True) if report else None,
            "search": search_result.to_dict(include_all=True) if search_result else None,
            "motion": animation.to_dict(include_samples=False) if animation else None,
            "random_seed": int(self.search_config.random_seed),
        }
        result.files["failure_report"] = save_json_file(
            os.path.join(output_dir, "failure_report.json"), payload
        )
        result.files["generation_log"] = self._write_log(run_log, output_dir)

    def _write_log(self, run_log: RunLogger, output_dir: str) -> str:
        ensure_dir(output_dir)
        path = os.path.join(output_dir, "generation_log.txt")
        run_log.write(path)
        return path

    def _write_artifacts(
        self, request, result, output_dir, run_log, *, camera, animation, report,
        search_result, scene, character_placement, character_status,
        animation_payload=None,
    ) -> "dict[str, str]":
        import bpy

        ensure_dir(output_dir)
        sequence_id = request.sequence_folder
        files: "dict[str, str]" = {}
        reason = ", ".join(report.failures) if report else ""

        # -- sequence blend ------------------------------------------------
        blend_path = os.path.join(output_dir, f"{sequence_id}.blend")
        if self.config.batch.save_sequence_blend:
            files["sequence_blend"] = self.save_sequence_blend(blend_path, run_log)
        else:
            # No scene copy: the sidecar carries the animation and the renderer
            # replays it onto ``source_blend``.  See core/camera_animation.py.
            run_log.log(
                "sequence .blend saving disabled by configuration: writing an "
                "animation-only sequence (the renderer needs the source scene)"
            )

        # -- trajectory + metadata ----------------------------------------
        rows = build_trajectory_rows(
            animation.samples,
            mode=self.config.render.trajectory_mode,
            step=self.config.render.trajectory_step,
        )
        video_ext = f".{self.config.render.video_format}"
        video_path = os.path.join(output_dir, f"{sequence_id}{video_ext}")
        metadata = SequenceMetadata(
            sequence_id=sequence_id,
            scene_name=request.scene_name,
            motion_name=request.motion_name,
            camera_name=camera.name,
            source_blend=request.scene_entry.path,
            frame_start=animation.frame_start,
            frame_end=animation.frame_end,
            fps=animation.fps,
            video_path=video_path,
            camera=camera,
            camera_trajectory=rows,
            has_character=bool(request.has_character and character_placement is not None and character_placement.ok),
            character_name=request.character.id if request.character else "",
            character_animation=getattr(request.animation, "id", "") or "",
            character_status=character_status,
            sequence_dir=output_dir,
            sequence_blend=files.get("sequence_blend", ""),
            validation=(report.to_dict(config=self.validation_config, include_frames=False) if report else {"passed": None, "skipped": True}),
            motion=animation.to_dict(include_samples=True),
            search=(search_result.to_dict(include_all=False) if search_result else {"attempted": False}),
            camera_original=camera.to_dict(),
            random_seed=int(self.search_config.random_seed),
            render={
                "resolution": list(camera.effective_resolution),
                "engine": str(scene.render.engine),
                "fps": int(round(animation.fps)),
                "planned_video_path": to_forward_slashes(video_path),
                "video_format": self.config.render.video_format,
            },
            extra={
                "scene_frame_range": [int(scene.frame_start), int(scene.frame_end)],
                "camera_data_block": getattr(getattr(bpy.data.objects.get(camera.name), "data", None), "name", ""),
                "character_placement": character_placement.to_dict() if character_placement else None,
                **generator_stamp(),
            },
        )
        if not (report is None or report.passed):
            metadata.status = "validation_failed"
            metadata.error = reason
        metadata.extra["motion_pipeline"] = {
            "output_dir": to_forward_slashes(output_dir),
            "relative_sequence_dir": relative_to(output_dir, self.output_root),
            "template_source": request.template.source,
            "template_parameters": dict(request.template.parameters),
        }
        if animation_payload:
            # Recorded whether or not a blend was written: it is what makes the
            # sequence replayable, and it costs ~15 KB next to a 265 MB scene copy.
            metadata.extra[PAYLOAD_KEY] = animation_payload
        metadata_path = os.path.join(output_dir, f"{sequence_id}.json")
        files["metadata"] = metadata.write(metadata_path)

        trajectory_path = os.path.join(output_dir, f"{sequence_id}_camera.txt")
        files["camera_trajectory"] = write_trajectory_txt(
            trajectory_path, rows,
            extra_header=[
                f"sequence_id={sequence_id}",
                f"scene={request.scene_name} motion={request.motion_name} camera={camera.name}",
                f"frames={animation.frame_start}..{animation.frame_end} fps={animation.fps:g}",
                f"coordinate_system=opencv_world_to_camera units=blender_world_units "
                f"rotation=3x3_rotation_matrix",
                f"generator=blender_motion_pipeline {GENERATOR_VERSION}",
            ],
        )

        # -- sequence config -------------------------------------------------
        config_path = os.path.join(output_dir, "sequence_config.json")
        config_payload = self._sequence_config_payload(request, camera, animation, result, report)
        # The renderer discovers sequences by reading only this file, so it says
        # where the animation lives without touching the (larger) sidecar.
        config_payload["camera_animation"] = payload_summary(
            animation_payload or {}, filename=os.path.basename(metadata_path)
        )
        files["sequence_config"] = save_json_file(config_path, config_payload)

        # -- validation report ----------------------------------------------
        if self.config.batch.save_validation_report:
            report_path = os.path.join(output_dir, "validation_report.json")
            files["validation_report"] = save_json_file(report_path, {
                "sequence_id": sequence_id,
                "generated_utc": utc_now_iso(),
                "validation": report.to_dict(config=self.validation_config, include_frames=True) if report else None,
                "search": search_result.to_dict(include_all=True) if search_result else None,
                "configuration": self.validation_config.to_dict(),
                "search_configuration": self.search_config.to_dict(),
                "camera_original": camera.to_dict(),
                "camera_final": {
                    "location": [round(float(v), 6) for v in animation.samples[0].position],
                    "lens_mm": round(float(animation.samples[0].focal), 6),
                    "intrinsics": camera_intrinsics(camera, focal_length=animation.samples[0].focal),
                },
                "motion": animation.to_dict(include_samples=False),
                **generator_stamp(),
            })

        # -- motion-level manifest -------------------------------------------
        manifest_path = os.path.join(
            normalize_path(self.output_root), request.scene_name, request.motion_folder, "manifest.json"
        )
        writer = ManifestWriter(
            manifest_path,
            kind="motion",
            scope={"scene_name": request.scene_name, "motion_name": request.motion_name,
                   "source_blend": to_forward_slashes(request.scene_entry.path)},
        )
        writer.add_sequence(result.to_manifest_entry())
        writer.flush()
        files["manifest"] = writer.path

        # -- log -------------------------------------------------------------
        files["generation_log"] = self._write_log(run_log, output_dir)
        return files

    def save_sequence_blend(self, path: str, run_log: RunLogger) -> str:
        """Save the current session as an independent sequence file.

        ``bpy.ops.wm.save_as_mainfile`` cannot be called from inside a
        ``bpy.app.timers`` callback: Blender's file writer re-enters the main
        loop and crashes with an access violation.  The panel therefore runs
        with ``defer_blend_save`` enabled and hands the path back through
        ``record_pending_blend`` for a later, safe flush.
        """
        if self.defer_blend_save:
            self.record_pending_blend(path, run_log)
            return ""
        return write_sequence_blend(path, run_log)

    def record_pending_blend(self, path: str, run_log: RunLogger) -> None:
        """Queue a sequence ``.blend`` to be written outside the timer."""
        target = normalize_path(path)
        if target not in self.pending_blend_saves:
            self.pending_blend_saves.append(target)
            run_log.log(
                "sequence .blend deferred until generation finishes "
                "(saving from inside a Blender timer would crash)"
            )

    def _sequence_config_payload(self, request, camera, animation, result, report) -> dict:
        payload = {
            "schema_version": self.config.schema_version,
            "sequence": {
                "sequence_id": request.sequence_folder,
                "scene_name": request.scene_name,
                "motion_name": request.motion_name,
                "camera_name": camera.name,
                "has_character": bool(request.has_character),
                "character_name": request.character.id if request.character else "",
                "character_animation": getattr(request.animation, "id", "") or "",
                "character_status": result.character_status,
                "source_blend": to_forward_slashes(request.scene_entry.path),
                "output_dir": to_forward_slashes(result.output_dir),
                "random_seed": int(self.search_config.random_seed),
            },
            "frames": {
                "frame_start": int(animation.frame_start),
                "frame_end": int(animation.frame_end),
                "frame_count": int(animation.frame_count),
                "fps": float(animation.fps),
            },
            "camera": {
                "original": camera.to_dict(),
                "final_first_frame": {
                    "location": [round(float(v), 6) for v in animation.samples[0].position],
                    "focal_length_mm": round(float(animation.samples[0].focal), 6),
                },
            },
            "motion": {
                "template_name": animation.template_name,
                "template_source": request.template.source,
                "interpolation": animation.interpolation,
                "parameters": dict(animation.template_parameters),
                "keyframes": [kf.to_dict() for kf in request.template.keyframes],
                "unit_scale": dict(animation.unit_scale),
            },
            "render": {
                **self.config.render.to_dict(),
                # What the scene itself would have produced, so a mismatch between
                # the requested size and the source scene is visible in the file the
                # renderer reads (this is what the 2000x2000 report came from).
                "scene_resolution": list(camera.effective_resolution),
                "effective_resolution": list(
                    self._sequence_resolution(camera)
                ),
            },
            "validation": {
                "passed": bool(report.passed) if report else None,
                "score": round(float(report.score), 6) if report else None,
                "reasons": report.failures if report else [],
            },
            "generator_version": GENERATOR_VERSION,
            "created_utc": utc_now_iso(),
        }
        payload.update(generator_stamp())
        return payload


def load_sequence_config(sequence_dir: str) -> "dict | None":
    """Read ``sequence_config.json`` beside a sequence (used by the renderer)."""
    from ..io.json_io import load_json_file

    path = os.path.join(normalize_path(sequence_dir), "sequence_config.json")
    if not os.path.isfile(path):
        return None
    try:
        payload = load_json_file(path)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def find_sequence_files(root: str, *, recursive: bool = True) -> "list[dict]":
    """Discover generated sequences under ``root``.

    A "sequence" is a folder containing a ``sequence_config.json``; the scene and
    motion are taken from that config when available so the renderer can rebuild
    the documented ``scene/motion/sequence`` output layout.
    """
    root = normalize_path(root)
    found: "list[dict]" = []
    if not os.path.isdir(root):
        return found
    if recursive:
        for current, _dirnames, filenames in os.walk(root):
            if "sequence_config.json" in filenames:
                found.append(_describe_sequence_dir(current, root))
    else:
        for name in sorted(os.listdir(root)):
            candidate = os.path.join(root, name)
            if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "sequence_config.json")):
                found.append(_describe_sequence_dir(candidate, root))
    return sorted(found, key=lambda item: item["sequence_dir"])


def write_sequence_blend(path: str, run_log: "RunLogger | None" = None, *, compress: bool = True) -> str:
    """Save the current Blender session as an independent sequence file.

    Safe to call from an operator or the CLI; **never** call this from a
    ``bpy.app.timers`` callback (see :meth:`SequenceGenerator.save_sequence_blend`).

    The file is written **compressed** (zstd, the same thing Blender's *Compress
    File* does).  A sequence is a full copy of the scene, so leaving compression
    off inflated a 265.9 MB scene to 610.6 MB per sequence -- 10.1 GB for 17
    sequences instead of 4.4 GB -- for no benefit, since Blender reads compressed
    files transparently.  Measure a given scene with
    ``tests/probe_blend_size.py``.

    ``bpy.data.use_autopack`` is disabled for the duration of the write.  With it
    on -- Blender's default, and what production files arrive with -- saving tries
    to *pack* every unpacked external file, and one missing texture aborts the
    whole save::

        RuntimeError: 错误: 无法打包文件, 找不到源路径 '...WoodenSurface1_ambientocclusion.png'

    That is a packaging concern, not a generation failure, and it must not cost
    the sequence.  The flag is always restored, including on error.
    """
    import bpy

    target = normalize_path(path)
    ensure_dir(os.path.dirname(target))
    before_file = bpy.data.filepath
    autopack_before = bool(getattr(bpy.data, "use_autopack", False))
    try:
        if autopack_before:
            bpy.data.use_autopack = False
            if run_log is not None:
                run_log.log(
                    "disabled bpy.data.use_autopack for this save: packing every "
                    "external file would fail on any missing asset"
                )
        try:
            bpy.ops.wm.save_as_mainfile(
                filepath=target,
                check_existing=False,
                copy=True,          # keep the session's current file untouched
                compress=bool(compress),
            )
        except TypeError:
            bpy.ops.wm.save_as_mainfile(filepath=target, check_existing=False)
    finally:
        if autopack_before:
            try:
                bpy.data.use_autopack = True
            except Exception:
                pass
        # Blender may rename the active file; keep working in the original context.
        if before_file and bpy.data.filepath != before_file:
            try:
                bpy.data.filepath = before_file
            except Exception:
                pass
    if run_log is not None:
        try:
            written = os.path.getsize(target)
            run_log.log(
                f"saved sequence blend: {target} "
                f"({written / (1024 * 1024):.1f} MB, compress={bool(compress)})"
            )
        except OSError:
            run_log.log(f"saved sequence blend: {target}")
    return target


def _describe_sequence_dir(directory: str, root: str) -> dict:
    config = load_sequence_config(directory) or {}
    sequence = config.get("sequence") or {}
    frames = config.get("frames") or {}
    scene_name = sequence.get("scene_name") or os.path.basename(os.path.dirname(os.path.dirname(directory)))
    motion_name = sequence.get("motion_name") or os.path.basename(os.path.dirname(directory))
    sequence_id = sequence.get("sequence_id") or os.path.basename(directory)
    blends = sorted(
        name for name in os.listdir(directory) if name.lower().endswith(".blend")
    )
    return {
        "sequence_dir": normalize_path(directory),
        "relative_dir": relative_to(directory, root),
        "scene_name": scene_name,
        "motion_name": motion_name,
        "sequence_id": sequence_id,
        "has_character": bool(sequence.get("has_character")),
        "character_name": sequence.get("character_name", ""),
        "camera_name": sequence.get("camera_name", ""),
        "frame_start": frames.get("frame_start"),
        "frame_end": frames.get("frame_end"),
        "fps": frames.get("fps"),
        "blend_files": blends,
        "config": config,
    }
